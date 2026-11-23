"""Capture real retrieval in ONE process per backend, then compare JSON offline.

Examples (run each backend as a separate external invocation):
  python tools/compare_vector_backends.py --backend pinecone --output pinecone-report.json --iterations 30
  python tools/compare_vector_backends.py --backend local --output local-report.json --iterations 30
  python tools/compare_vector_backends.py --compare pinecone-report.json local-report.json --output comparison.json

RERANKER_ENABLED=false is the existing reranker switch. The optimizer and chat
graph are never entered; there is no invented optimizer switch or LLM invocation.
STORE_BACKEND=file and AWS_EC2_METADATA_DISABLED=true are set before repo imports.
Upload chronology is explicitly omitted using retrieve_documents(uploads={});
recorded local source dates still apply. This is not an end-to-end chat benchmark.
"""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
QUERIES = [
    ("payment", "does samar college allow partial payment?"),
    ("drop", "what do i do if i got dropped?"),
    ("classroom", "classroom location"),
    ("leadership", "who are the principal of highschool, elementary and who is the dean of citas?"),
    ("programs", "what are the programs offered?"),
    ("deans", "who are the deans of each college?"),
    ("enrollment", "What documents are required for new college student enrollment?"),
    ("grading", "What is the college grading system and passing grade?"),
    ("family_discount", "What tuition discounts are available for siblings enrolled together?"),
    ("student_id", "How do students report and replace a lost RFID school ID?"),
]
# Explicit, contentful jobs: no automatic expansion can change count10.
PROBES = [
    "tuition payment installment deadlines penalties",
    "college subject dropping procedure deadline",
    "classroom student conduct rules discipline",
    "high school elementary principal CITAS dean names",
    "Samar College degree programs course offerings",
    "college department deans leadership names",
    "new college student enrollment required documents",
    "college grading system passing grade",
    "siblings family tuition discount rates",
    "lost RFID school student identification replacement",
]
ENVIRONMENT = {
    "RERANKER_ENABLED": "false",
    "STORE_BACKEND": "file",
    "AWS_EC2_METADATA_DISABLED": "true",
}


def json_value(value):
    """Normalize metadata without hiding non-JSON types or non-finite scores."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"non_finite": str(value)}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return {"type": type(value).__name__, "repr": str(value)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def documents(docs):
    result = []
    for rank, doc in enumerate(docs, 1):
        metadata = json_value(doc.metadata or {})
        text = doc.page_content
        chunk_id = metadata.get("chunk_id")
        result.append({
            "rank": rank, "chunk_id": chunk_id,
            "document_id": json_value(getattr(doc, "id", None)),
            "identity": str(chunk_id) if chunk_id is not None else digest({
                "source": metadata.get("source"), "page": metadata.get("page"), "text": text,
            }),
            "identity_basis": "chunk_id" if chunk_id is not None else "source_page_full_text_sha256",
            "text": text, "metadata": metadata,
            "scores": {key: value for key, value in metadata.items()
                       if "score" in key.lower()},
        })
    return result


def percentiles(samples):
    values = sorted(samples)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None}

    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {"count": len(values), "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95), "min_ms": values[0], "max_ms": values[-1]}


class ObservedRetriever:
    """Delegate unchanged real calls; keep candidates/errors even if chain swallows them."""

    def __init__(self, target, stage, observations, lock, embeddings=None):
        self.target, self.stage = target, stage
        self.observations, self.lock = observations, lock
        self.embeddings = embeddings

    def __getattr__(self, name):
        return getattr(self.target, name)

    def invoke(self, query, *args, **kwargs):
        observation = {"stage": self.stage, "query": query,
                       "filter": json_value(kwargs.get("filter"))}
        if self.embeddings is not None:
            key = hashlib.sha256(query.encode("utf-8")).digest()
            with self.embeddings._cache_lock:
                observation["query_vector_cached_at_entry"] = key in self.embeddings._cache
        with self.lock:
            self.observations.append(observation)
        try:
            docs = self.target.invoke(query, *args, **kwargs)
            # Detach metadata cheaply now; JSON conversion/hashing is outside the timer.
            observation.update(status="ok", _documents=[SimpleNamespace(
                page_content=doc.page_content, metadata=dict(doc.metadata or {}),
                id=getattr(doc, "id", None),
            ) for doc in docs], count=len(docs))
            return docs
        except Exception as exc:
            observation.update(status="error", error_type=type(exc).__name__, error=str(exc), count=0)
            raise


def measure(chain, operation, iterations, observations):
    """Cold clears ONLY the existing query-vector cache; no model replacement."""
    snapshots, samples = {}, {"cold": [], "warm": []}
    warmup = None
    for mode in ("cold", "warm"):
        if mode == "warm":
            # Explicit warmup for this operation, not an assumption about capacity.
            warmup = call_once(chain, operation, observations, snapshots)
        for _ in range(iterations):
            if mode == "cold":
                with chain.embeddings._cache_lock:
                    chain.embeddings._cache.clear()
            sample = call_once(chain, operation, observations, snapshots)
            with chain.embeddings._cache_lock:
                sample["query_embedding_cache_entries_after"] = len(chain.embeddings._cache)
            samples[mode].append(sample)
    return {
        "stage": operation["stage"], "query": operation.get("query"),
        "requested_probe_count": 10 if operation["stage"] == "multi_query_count10" else None,
        "requested_probes": PROBES if operation["stage"] == "multi_query_count10" else None,
        "warmup": warmup, "samples": samples, "snapshots": snapshots,
        "timings": {mode: {
            "successful": percentiles([sample["duration_ms"] for sample in runs if sample["status"] == "ok"]),
            "all_attempts": percentiles([sample["duration_ms"] for sample in runs]),
            "failed_count": sum(sample["status"] != "ok" for sample in runs),
        } for mode, runs in samples.items()},
        "first_measured_call_ms": samples["cold"][0]["duration_ms"],
    }


def call_once(chain, operation, observations, snapshots):
    observations.clear()
    log = io.StringIO()
    started = time.perf_counter()
    error, docs = None, []
    try:
        with redirect_stdout(log):
            stage, query = operation["stage"], operation.get("query")
            if stage in {"sparse", "dense", "ensemble"}:
                target = {"sparse": chain.sparse_retriever, "dense": chain.dense_retriever,
                          "ensemble": chain.retriever}[stage]
                docs = target.invoke(query)
            elif stage == "multi_query_count10":
                docs = chain.multi_query_retrieve(
                    PROBES[0], filtered_queries=[(probe, None) for probe in PROBES],
                    allow_spelling=False,
                )
            else:
                docs = chain.retrieve_documents(query, uploads={}, verbose=False)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    duration_ms = (time.perf_counter() - started) * 1000
    # A timed-out pool can still have running jobs. Drain it before attributing
    # subsequent observations or clearing the cache; this wait is NOT latency.
    drain_ms = 0
    if error:
        drain_start = time.perf_counter()
        pool = chain.retrieval_pool
        pool.shutdown()
        from rag.runtime import BoundedExecutor
        chain.retrieval_pool = BoundedExecutor(pool.workers, 24, pool.name)
        drain_ms = (time.perf_counter() - drain_start) * 1000
    logged = log.getvalue()
    for observation in observations:
        if "_documents" in observation:
            observation["documents"] = documents(observation.pop("_documents"))
    failures = [item for item in observations if item.get("status") != "ok"]
    observed_probes = [item["query"] for item in observations if item["stage"] == "ensemble"]
    count10_ok = (sorted(observed_probes) == sorted(PROBES)
                  if operation["stage"] == "multi_query_count10" else True)
    silent_failure = any(marker in logged for marker in (
        "Retrieval failed", "Skipping retrieval probe", "search is unavailable",
    ))
    status = "error" if error or failures or silent_failure or not count10_ok else "ok"
    snapshot = {"documents": documents(docs), "result_count": len(docs),
                "candidate_count": sum(item.get("count", 0) for item in observations),
                "candidate_count_basis": "sum of observed ensemble or direct/filtered leaf returns, before deduplication",
                "unique_candidate_count": len({doc["identity"] for item in observations
                                                for doc in item.get("documents", [])}),
                "probes": observations.copy(), "log": logged,
                "count10_verified": count10_ok if operation["stage"] == "multi_query_count10" else None}
    snapshot_id = digest(snapshot)
    snapshots.setdefault(snapshot_id, snapshot)
    return {"status": status, "error": error, "duration_ms": duration_ms,
            "timeout_drain_ms": drain_ms, "snapshot": snapshot_id,
            "result_count": len(docs), "observed_ensemble_probe_count": len(observed_probes),
            "failed_probe_count": len(failures), "logged_failure_or_skip": silent_failure}


def capture(backend, iterations):
    report = {
        "schema_version": 1, "kind": "backend_capture", "backend": backend,
        "created_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
        "iterations_per_cache_mode": iterations, "query_suite": QUERIES,
        "count10_probes": PROBES, "environment": dict(ENVIRONMENT, VECTOR_BACKEND=backend),
        "scope": {
            "real_chain_methods": True, "llm_calls": 0, "generation": "not invoked",
            "optimizer": "not invoked: retrieval-only entry points; no optimizer disable env exists",
            "reranker": "disabled via RERANKER_ENABLED=false",
            "total_request_timing": "unmeasured (no chat graph, optimizer, generation, or HTTP request)",
            "chronology": "AWS upload chronology omitted via uploads={}; local recorded dates unchanged",
            "cold": "query embedding cache cleared before each sample; model/index/OS caches NOT reset",
            "warm": "one explicit untimed warmup per operation; query cache retained, capacity may evict probes",
            "method_timing": "wall clock including real embeddings/retrieval/selection and candidate observation overhead",
            "vector_export": "not supported; corpus changes must be investigated, not called backend parity",
            "score_policy": "reported metadata scores are unchanged; ensemble may inherit leaf scores, not RRF scores",
        },
        "status": "error", "operations": {},
    }
    for key, value in report["environment"].items():
        os.environ[key] = value
    sys.path.insert(0, str(ROOT))
    # Repo paths are relative. CLI paths have already been resolved before this.
    os.chdir(ROOT)
    started = time.perf_counter()
    startup_log = io.StringIO()
    try:
        with redirect_stdout(startup_log):
            from rag import chain
        report["startup_ms"] = (time.perf_counter() - started) * 1000
        report["startup_log"] = startup_log.getvalue()
        import config
        selected_backend = getattr(config, "VECTOR_BACKEND", "pinecone")
        if selected_backend != backend:
            raise RuntimeError(f"Requested {backend}, but config selected {selected_backend}")
        if chain.sparse_retriever.top_k != 30 or chain.dense_retriever.top_k != 30:
            raise RuntimeError("Expected both real leaf retrievers to use top_k=30")
        if not hasattr(chain.embeddings, "_cache_lock") or not hasattr(chain.embeddings, "_cache"):
            raise RuntimeError("Cannot honestly distinguish cold/warm query embedding cache")
        report["retriever_settings"] = {
            "top_k": 30, "sparse_alpha": chain.sparse_retriever.alpha,
            "dense_alpha": chain.dense_retriever.alpha,
            "ensemble_weights": chain.retriever.weights, "rrf_c": chain.retriever.c,
            "final_top_k": chain.FINAL_TOP_K, "query_cache_size": chain.embeddings.cache_size,
            "index_type": type(chain.index).__name__,
        }
        report["embedding_settings"] = {
            "model_name": getattr(chain.embeddings.model, "model_name", None),
            "encode_kwargs": json_value(getattr(chain.embeddings.model, "encode_kwargs", {})),
        }
        # Public embedding/encoder APIs, unchanged vectors; untimed and followed
        # by cache clearing. Fingerprints expose query/model drift across runs.
        fingerprint_queries = list(dict.fromkeys([q for _, q in QUERIES] + PROBES))
        report["query_fingerprints"] = {query: {
            "dense": digest(chain.embeddings.embed_query(query)),
            "sparse": digest(json_value(chain.bm25.encode_queries(query))),
        } for query in fingerprint_queries}
        report["recorded_source_dates"] = json_value(chain.get_doc_dates())
        if backend == "local":
            report["local_manifest_start"] = json_value(chain.index.snapshot().manifest)
        report["input_artifacts"] = {}
        for name in ("bm25_values.json", "doc_freshness.json"):
            path = ROOT / name
            report["input_artifacts"][name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        observations, lock = [], threading.Lock()
        for name, stage in (("sparse_retriever", "sparse"), ("dense_retriever", "dense"), ("retriever", "ensemble")):
            setattr(chain, name, ObservedRetriever(getattr(chain, name), stage, observations, lock, chain.embeddings))
        operations = [(f"{name}/{stage}", {"stage": stage, "query": query})
                      for name, query in QUERIES
                      for stage in ("sparse", "dense", "ensemble", "retrieve_documents")]
        operations.append(("multi_query/count10", {"stage": "multi_query_count10"}))
        for name, operation in operations:
            print(f"[{backend}] {name}: {iterations} cold + {iterations} warm samples", flush=True)
            report["operations"][name] = measure(chain, operation, iterations, observations)
        successful = all(sample["status"] == "ok"
                         for operation in report["operations"].values()
                         for sample in [operation["warmup"], *operation["samples"]["cold"], *operation["samples"]["warm"]])
        report["status"] = "ok" if successful else "error"
        report["expected_operation_count"] = len(operations)
        report["nonempty_results"] = any(sample["result_count"] > 0
                                         for op in report["operations"].values()
                                         for runs in op["samples"].values() for sample in runs)
        if backend == "local":
            report["local_manifest_end"] = json.loads((chain.index.path / "manifest.json").read_text(encoding="utf-8"))
            if report["local_manifest_start"] != report["local_manifest_end"]:
                report["status"] = "error"
                report["error"] = {"type": "CorpusChanged", "message": "Local generation changed during capture"}
    except Exception as exc:
        report["status"] = "error"
        report["startup_log"] = startup_log.getvalue()
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def compare(left, right):
    """Pure stdlib comparison; importing this tool never imports rag.chain."""
    result = {"schema_version": 1, "kind": "backend_comparison", "status": "inconclusive",
              "parity": "not established", "reasons": [], "operations": {},
              "total_request_timing": "unmeasured"}
    if not isinstance(left, dict) or not isinstance(right, dict):
        result["reasons"].append("Capture reports must be JSON objects")
        return result
    result.update(left_backend=left.get("backend"), right_backend=right.get("backend"))
    for report in (left, right):
        if report.get("kind") != "backend_capture" or report.get("schema_version") != 1:
            result["reasons"].append("Unsupported capture report")
        has_documents = any(sample.get('result_count', 0) > 0
                            for operation in report.get('operations', {}).values()
                            for runs in operation.get('samples', {}).values() for sample in runs)
        if report.get("status") != "ok" or not has_documents:
            result["reasons"].append(f"{report.get('backend')}: incomplete, failed, or empty retrieval capture")
    if {left.get("backend"), right.get("backend")} != {"pinecone", "local"}:
        result["reasons"].append("Need one pinecone and one local capture")
    for key in ("query_suite", "count10_probes", "iterations_per_cache_mode", "scope", "input_artifacts",
                "embedding_settings", "query_fingerprints", "recorded_source_dates"):
        if left.get(key) != right.get(key):
            result["reasons"].append(f"Capture configuration differs: {key}")
    if set(left.get("operations", {})) != set(right.get("operations", {})):
        result["reasons"].append("Operation suites differ")
    for key in ("top_k", "sparse_alpha", "dense_alpha", "ensemble_weights", "rrf_c", "final_top_k", "query_cache_size"):
        if left.get("retriever_settings", {}).get(key) != right.get("retriever_settings", {}).get(key):
            result["reasons"].append(f"Retriever settings differ: {key}")
    expected_count = len(QUERIES) * 4 + 1
    for report in (left, right):
        if len(report.get("operations", {})) != expected_count:
            result["reasons"].append(f"{report.get('backend')}: expected {expected_count} operations")
        for name, operation in report.get("operations", {}).items():
            for mode in ("cold", "warm"):
                runs = operation.get("samples", {}).get(mode, [])
                if len(runs) != report.get("iterations_per_cache_mode"):
                    result["reasons"].append(f"{report.get('backend')}: incomplete {name}/{mode}")
                for sample in runs:
                    snapshot = operation.get("snapshots", {}).get(sample.get("snapshot"), {})
                    empty_unexpected = not snapshot.get("documents") and not name.endswith('/retrieve_documents')
                    if sample.get("status") != "ok" or empty_unexpected:
                        result["reasons"].append(f"{report.get('backend')}: failed/empty {name}/{mode}")
                    if name == "multi_query/count10" and not snapshot.get("count10_verified"):
                        result["reasons"].append(f"{report.get('backend')}: count10 not verified")
    if result["reasons"]:
        return result
    all_equal = True
    for name, a in left["operations"].items():
        b = right["operations"][name]
        rankings = []
        for mode in ("cold", "warm"):
            pairs = zip(a["samples"][mode], b["samples"][mode])
            for index, (sa, sb) in enumerate(pairs, 1):
                da = a["snapshots"][sa["snapshot"]]["documents"]
                db = b["snapshots"][sb["snapshot"]]["documents"]
                ia, ib = [doc["identity"] for doc in da], [doc["identity"] for doc in db]
                common = set(ia) & set(ib)
                text_equal = ([doc["text"] for doc in da] == [doc["text"] for doc in db])
                def metadata_without_scores(docs):
                    return [{key: value for key, value in doc["metadata"].items()
                             if "score" not in key.lower()} for doc in docs]
                metadata_equal = metadata_without_scores(da) == metadata_without_scores(db)
                equal = ia == ib and text_equal and metadata_equal
                all_equal = all_equal and equal
                rankings.append({"mode": mode, "iteration": index, "ordered_ids_equal": ia == ib,
                                 "text_equal": text_equal, "metadata_without_scores_equal": metadata_equal,
                                 "scores_equal": [doc["scores"] for doc in da] == [doc["scores"] for doc in db],
                                 "overlap_count": len(common), "left_count": len(ia), "right_count": len(ib),
                                 "jaccard": len(common) / len(set(ia) | set(ib)) if ia or ib else None,
                                 "left_only": sorted(set(ia) - set(ib)), "right_only": sorted(set(ib) - set(ia)),
                                 "rank_changes": [{"identity": item, "left": ia.index(item) + 1,
                                                   "right": ib.index(item) + 1} for item in ia
                                                  if item in common and ia.index(item) != ib.index(item)]})
        result["operations"][name] = {"rankings": rankings,
                                     "left_timings": a["timings"], "right_timings": b["timings"]}
    result.update(status="compared", parity="observed suite ranking/text/metadata parity" if all_equal else "differences observed",
                  score_policy="raw score differences reported separately; ensemble score metadata may be inherited, not an RRF score",
                  limitation="Evidence only for captured suite and omitted upload chronology; not global corpus or answer parity")
    return result


def main(argv=None):
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backend", choices=("local", "pinecone"))
    mode.add_argument("--compare", nargs=2, type=Path, metavar=("PINECONE_REPORT", "LOCAL_REPORT"))
    parser.add_argument("--output", type=Path, help="Required for captures; comparisons otherwise print JSON")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--repo-root", type=Path, help="Run against an untouched snapshot for the before measurement")
    args = parser.parse_args(argv)
    if args.repo_root is not None:
        ROOT = args.repo_root.resolve()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.backend and args.output is None:
        parser.error("--backend requires --output")
    output = args.output.resolve() if args.output is not None else None
    if output is not None and not output.parent.is_dir():
        parser.error("--output parent directory must already exist")
    if args.compare:
        paths = [path.resolve() for path in args.compare]
        if output in paths:
            parser.error("--output must not overwrite an input capture")
        try:
            reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            report = compare(*reports)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            report = {"kind": "backend_comparison", "status": "inconclusive",
                      "parity": "not established", "error": str(exc)}
    else:
        report = capture(args.backend, args.iterations)
    payload = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    if output is None:
        print(payload, end="")
    else:
        output.write_text(payload, encoding="utf-8")
        print(f"{report['status']}: {output}")
    if args.compare:
        return 0 if report.get("parity") == "observed suite ranking/text/metadata parity" else 1
    return 0 if report["status"] == "ok" and report.get("nonempty_results") else 1


if __name__ == "__main__":
    raise SystemExit(main())
