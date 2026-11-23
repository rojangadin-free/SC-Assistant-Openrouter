"""Local hybrid retrieval contracts with real BM25 and on-disk generations."""

import _bootstrap  # noqa: F401

import concurrent.futures
import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pinecone_text.sparse import BM25Encoder

from rag.local_index import LocalHybridSearchRetriever, LocalIndex


MODEL_NAME = "fake-test"
ROOT = Path(__file__).resolve().parents[1]


class FakeEmbeddings(Embeddings):
    """Deliberately unnormalized vectors expose accidental cosine scoring."""

    model_name = MODEL_NAME
    vectors = {
        "ZXQCODE": [1.0, 0.0],
        "ZXQCODE library": [1.0, 0.5],
        "ZXQCODE scholarship office": [0.0, 2.0],
        "library reading services": [4.0, 0.0],
        "library office assistance": [2.0, 1.0],
        "sports athletics facilities": [0.5, 3.0],
    }

    def embed_query(self, text):
        if text in self.vectors:
            return list(self.vectors[text])
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [0.25 + digest[0] / 255.0, 0.25 + digest[1] / 255.0]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


class WrongDimensionEmbeddings(FakeEmbeddings):
    def embed_query(self, text):
        return super().embed_query(text) + [0.0]


def make_document(text, chunk_id, source="handbook.pdf", **metadata):
    return Document(page_content=text, metadata={
        "chunk_id": chunk_id, "source": source, **metadata,
    })


def sparse_dot(left, right):
    coordinates = dict(zip(left["indices"], left["values"]))
    return sum(coordinates.get(i, 0.0) * v
               for i, v in zip(right["indices"], right["values"]))


def process_upsert(path, number, barrier):
    index = LocalIndex(path, FakeEmbeddings(), embedding_model=MODEL_NAME,
                       reload_interval=0)
    barrier.wait(timeout=45)
    index.upsert([make_document(f"parallel office record {number}", f"process-{number}")])


class LocalIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "index"
        self.embeddings = FakeEmbeddings()
        self.documents = [
            make_document("ZXQCODE scholarship office", "acronym", "scholarships.pdf",
                          page=2, department="aid", active=True, optional=None,
                          context="must not leak", extra={"label": "retained"}),
            make_document("library reading services", "dense", page=3,
                          department="library", active=False, optional="value"),
            make_document("library office assistance", "mixed", page=4,
                          department="library", active=True, optional=None),
            make_document("sports athletics facilities", "sports", "sports.pdf",
                          page=5, department="sports", active=False, optional="value"),
        ]
        self.encoder = BM25Encoder().fit([d.page_content for d in self.documents])
        self.index = self.build(self.documents, self.encoder)

    def build(self, documents, encoder=None):
        if encoder is None:
            encoder = BM25Encoder().fit([d.page_content for d in documents])
        return LocalIndex.build(self.path, documents, self.embeddings, encoder,
                                embedding_model=MODEL_NAME)

    def reopen(self, **kwargs):
        return LocalIndex(self.path, self.embeddings, embedding_model=MODEL_NAME,
                          **kwargs)

    @staticmethod
    def ids(documents):
        return [d.metadata["chunk_id"] for d in documents]

    def test_build_returns_loaded_snapshot_and_real_sparse_coordinates(self):
        self.assertIsInstance(self.index, LocalIndex)
        snapshot = self.index.snapshot()
        self.assertIsInstance(snapshot.documents, tuple)
        self.assertEqual(list(snapshot.documents), self.documents)
        self.assertEqual(snapshot.dense.shape, (4, 2))
        self.assertEqual(snapshot.sparse.shape[0], 4)
        self.assertEqual(snapshot.encoder.get_params(), self.encoder.get_params())
        np.testing.assert_allclose(snapshot.dense, self.embeddings.embed_documents(
            [d.page_content for d in self.documents]))
        for row, document in enumerate(self.documents):
            encoded = self.encoder.encode_documents(document.page_content)
            for coordinate, value in zip(encoded["indices"], encoded["values"]):
                self.assertIn(coordinate, snapshot.vocabulary)
                self.assertAlmostEqual(float(snapshot.sparse[row, snapshot.vocabulary[coordinate]]),
                                       value, places=6)

    def test_metadata_strips_context_preserves_every_other_field_and_adds_score(self):
        original = copy.deepcopy(self.documents[0].metadata)
        result, = self.index.search("ZXQCODE", filter={"chunk_id": {"$eq": "acronym"}})
        self.assertEqual(result.page_content, original["context"])
        expected = {key: value for key, value in original.items() if key != "context"}
        self.assertEqual({key: value for key, value in result.metadata.items() if key != "score"},
                         expected)
        self.assertIsInstance(result.metadata["score"], (int, float))
        self.assertEqual(self.documents[0].metadata, original)
        self.assertEqual(self.index.snapshot().documents[0].metadata, original)

    def test_returned_nested_metadata_cannot_mutate_the_shared_snapshot(self):
        result, = self.index.search("ZXQCODE", filter={"chunk_id": {"$eq": "acronym"}})
        result.metadata["extra"]["label"] = "changed"
        again, = self.index.search("ZXQCODE", filter={"chunk_id": {"$eq": "acronym"}})
        self.assertEqual(again.metadata["extra"], {"label": "retained"})

    def test_delete_preserves_stored_sparse_coordinates_without_reencoding(self):
        current = self.index.snapshot()
        encoded = LocalIndex._sparse_vectors(current)
        encoded[1]["values"] = [value * 3 for value in encoded[1]["values"]]
        LocalIndex._persist(self.path, self.documents, current.dense, current.encoder,
                            MODEL_NAME, 2, sparse_vectors=encoded)
        changed = self.reopen(reload_interval=0).snapshot()
        original = LocalIndex._sparse_vectors(changed)[1]
        self.index.delete_by_source("scholarships.pdf")
        self.assertEqual(LocalIndex._sparse_vectors(self.index.snapshot())[0], original)

    def test_existing_metadata_score_is_not_overwritten(self):
        documents = [make_document("library reading services", "scored", score=123.5)]
        index = self.build(documents)
        self.assertEqual(index.search("library")[0].metadata["score"], 123.5)

    def test_none_filter_and_top_k(self):
        self.assertEqual(self.ids(self.index.search("ZXQCODE library")),
                         self.ids(self.index.search("ZXQCODE library", filter=None)))
        for top_k in (0, 1, 2, 3, 4, 30):
            with self.subTest(top_k=top_k):
                results = self.index.search("ZXQCODE library", top_k=top_k, alpha=1)
                self.assertEqual(len(results), min(top_k, len(self.documents)))
                self.assertEqual(self.ids(results), ["dense", "mixed", "sports", "acronym"][:top_k])

    def test_eq_and_in_filters_for_arbitrary_metadata_keys_including_none(self):
        cases = {
            "source": "handbook.pdf", "chunk_id": "mixed", "page": 4,
            "department": "library", "active": True, "optional": None,
        }
        for key, value in cases.items():
            expected = {d.metadata["chunk_id"] for d in self.documents
                        if d.metadata.get(key) == value}
            for operation, operand in (("$eq", value), ("$in", [value])):
                with self.subTest(key=key, operation=operation, value=value):
                    results = self.index.search("ZXQCODE library", alpha=1,
                                                filter={key: {operation: operand}})
                    self.assertEqual(set(self.ids(results)), expected)
        self.assertEqual(set(self.ids(self.index.search("library", alpha=1,
                         filter={"page": {"$in": [2, 5]}}))), {"acronym", "sports"})
        self.assertEqual(self.index.search("library", filter={"source": {"$eq": "missing"}}), [])
        self.assertEqual(self.index.search("library", filter={"page": {"$in": []}}), [])

    def test_multiple_filter_keys_are_conjoined(self):
        results = self.index.search("library", alpha=1, filter={
            "department": {"$eq": "library"}, "active": {"$eq": True},
        })
        self.assertEqual(self.ids(results), ["mixed"])

    def test_filter_is_applied_before_top_k_not_after_candidate_truncation(self):
        documents = [make_document("library reading services", f"distractor-{i}",
                                   department="unrelated") for i in range(35)]
        documents.append(make_document("ZXQCODE scholarship office", "eligible",
                                       department="aid"))
        index = self.build(documents)
        self.assertNotIn("eligible", self.ids(index.search("ZXQCODE", top_k=30, alpha=1)))
        self.assertEqual(self.ids(index.search("ZXQCODE", top_k=1, alpha=1,
                         filter={"department": {"$eq": "aid"}})), ["eligible"])

    def test_alpha_endpoints_and_midpoint_match_raw_dense_and_sparse_dot_products(self):
        query = "ZXQCODE library"
        dense_query = np.asarray(self.embeddings.embed_query(query))
        sparse_query = self.encoder.encode_queries(query)
        dense_documents = self.embeddings.embed_documents([d.page_content for d in self.documents])
        sparse_documents = self.encoder.encode_documents([d.page_content for d in self.documents])
        for alpha in (0.0, 0.5, 1.0):
            expected = {
                document.metadata["chunk_id"]:
                alpha * float(np.dot(dense_query, dense)) +
                (1 - alpha) * sparse_dot(sparse_query, sparse)
                for document, dense, sparse in zip(self.documents, dense_documents, sparse_documents)
            }
            with self.subTest(alpha=alpha):
                results = self.index.search(query, top_k=30, alpha=alpha)
                self.assertEqual(set(self.ids(results)), set(expected))
                scores = [d.metadata["score"] for d in results]
                self.assertEqual(scores, sorted(scores, reverse=True))
                for result in results:
                    self.assertAlmostEqual(result.metadata["score"],
                                           expected[result.metadata["chunk_id"]], places=5)

    def test_exact_acronym_sparse_hit_is_a_dense_top_one_miss(self):
        self.assertEqual(self.ids(self.index.search("ZXQCODE", top_k=1, alpha=0)), ["acronym"])
        self.assertEqual(self.ids(self.index.search("ZXQCODE", top_k=1, alpha=1)), ["dense"])

    def test_retriever_is_real_base_retriever_and_invoke_forwards_filter(self):
        retriever = LocalHybridSearchRetriever(index=self.index, embeddings=self.embeddings,
                                              sparse_encoder=self.encoder, top_k=1, alpha=1)
        self.assertIsInstance(retriever, BaseRetriever)
        self.assertEqual(self.ids(retriever.invoke("ZXQCODE")), ["dense"])
        self.assertEqual(self.ids(retriever.invoke("ZXQCODE", filter={
            "department": {"$eq": "aid"},
        })), ["acronym"])
        self.assertEqual(retriever.top_k, 1)
        self.assertEqual(retriever.alpha, 1)

    def test_retriever_defaults_match_index_defaults(self):
        retriever = LocalHybridSearchRetriever(index=self.index, embeddings=self.embeddings,
                                              sparse_encoder=self.encoder)
        self.assertEqual(retriever.top_k, 30)
        self.assertEqual(retriever.alpha, 0.5)
        self.assertEqual(retriever.invoke("ZXQCODE library"),
                         self.index.search("ZXQCODE library"))

    def assert_survivors_preserved(self, before, removed):
        after = self.index.snapshot()
        survivors = [i for i, d in enumerate(before.documents)
                     if d.metadata["chunk_id"] not in removed]
        self.assertEqual(self.ids(after.documents), [before.documents[i].metadata["chunk_id"]
                                                   for i in survivors])
        np.testing.assert_array_equal(after.dense, before.dense[survivors])
        self.assertEqual(after.encoder.get_params(), before.encoder.get_params())
        for new_row, old_row in enumerate(survivors):
            for coordinate, old_column in before.vocabulary.items():
                expected = float(before.sparse[old_row, old_column])
                column = after.vocabulary.get(coordinate)
                actual = float(after.sparse[new_row, column]) if column is not None else 0.0
                self.assertAlmostEqual(actual, expected, places=7)

    def test_delete_by_source_preserves_survivor_vectors_and_bm25_statistics(self):
        before = self.index.snapshot()
        with patch.object(self.embeddings, "embed_documents", side_effect=AssertionError("reembedding")), \
                patch.object(BM25Encoder, "fit", side_effect=AssertionError("refitting")):
            self.index.delete_by_source("handbook.pdf")
        self.assert_survivors_preserved(before, {"dense", "mixed"})
        self.assertEqual(set(self.ids(self.reopen().snapshot().documents)), {"acronym", "sports"})
        self.assertEqual(set(self.ids(before.documents)), {"acronym", "dense", "mixed", "sports"})

    def test_delete_filter_and_delete_all_are_persistent(self):
        before = self.index.snapshot()
        self.index.delete(filter={"department": {"$in": ["library"]}})
        self.assert_survivors_preserved(before, {"dense", "mixed"})
        self.index.delete(filter=None)
        self.assertEqual(self.index.search("library"), [])
        self.assertEqual(self.reopen().snapshot().documents, ())

    def test_upsert_adds_and_replaces_chunk_ids_without_duplicates(self):
        replacement = make_document("updated library office hours", "dense", page=99)
        addition = make_document("new library archive services", "new")
        self.index.upsert([replacement, addition])
        documents = self.reopen().snapshot().documents
        self.assertEqual(len(documents), 5)
        by_id = {d.metadata["chunk_id"]: d for d in documents}
        self.assertEqual(by_id["dense"], replacement)
        self.assertEqual(by_id["new"], addition)
        self.assertEqual(self.ids(documents).count("dense"), 1)

    def test_upsert_reuses_bm25_statistics_when_encoder_is_omitted(self):
        parameters = copy.deepcopy(self.index.snapshot().encoder.get_params())
        with patch.object(BM25Encoder, "fit", side_effect=AssertionError("refitting")):
            self.index.upsert([make_document("library archive office", "new")])
        self.assertEqual(self.reopen().snapshot().encoder.get_params(), parameters)

    def test_upsert_accepts_explicit_sparse_encoder(self):
        self.index.upsert([make_document("ZXQCODE office library", "new")],
                          sparse_encoder=self.encoder)
        self.assertIn("new", self.ids(self.reopen().snapshot().documents))

    def test_upsert_rejects_changed_sparse_statistics_without_publishing(self):
        before = self.index.snapshot().manifest
        encoder = BM25Encoder().fit(["entirely different corpus statistics"])
        with self.assertRaisesRegex(ValueError, "full.*build"):
            self.index.upsert([make_document("new office record", "new")], sparse_encoder=encoder)
        self.assertEqual(self.reopen().snapshot().manifest, before)

    def test_transaction_staging_does_not_block_cached_searches(self):
        from rag.store import _file_transaction
        with _file_transaction(str(self.path / "manifest.json.refresh")):
            # A separate worker can hold the mutation lock while staging files.
            # Search uses only the short, independent publication lock.
            result = self.reopen(reload_interval=0).search("library", alpha=1)
        self.assertTrue(result)

    def test_persistence_round_trip_and_immutable_generation_files(self):
        manifest = self.path / "manifest.json"
        first_manifest = manifest.read_bytes()
        manifest_data = json.loads(first_manifest)
        old_files = {p: p.read_bytes() for p in (self.path / "generations").rglob("*")
                     if p.is_file()}
        self.assertEqual({p.name for p in old_files},
                         {"vectors.npy", "chunks.jsonl", "sparse.npz", "bm25.json"})
        generation = self.path / "generations" / manifest_data["generation"]
        for path, contents in old_files.items():
            self.assertEqual(path.parent, generation)
            self.assertEqual(manifest_data["files"][path.name],
                             hashlib.sha256(contents).hexdigest())
        loaded = self.reopen()
        for alpha in (0, 0.5, 1):
            self.assertEqual(loaded.search("ZXQCODE library", alpha=alpha),
                             self.index.search("ZXQCODE library", alpha=alpha))
        self.build([make_document("replacement library catalog", "replacement")])
        self.assertNotEqual(manifest.read_bytes(), first_manifest)
        for path, contents in old_files.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(self.ids(self.reopen().snapshot().documents), ["replacement"])

    def test_missing_manifest_raises_clear_runtime_error(self):
        missing = Path(self.temporary.name) / "not-built"
        with self.assertRaisesRegex(RuntimeError, r"(?i)(manifest|build|index)"):
            LocalIndex(missing, self.embeddings, embedding_model=MODEL_NAME)

    def test_startup_rejects_embedding_model_mismatch(self):
        with self.assertRaisesRegex(ValueError, r"(?i)(model|embedding)"):
            LocalIndex(self.path, self.embeddings, embedding_model="different-model")

    def test_startup_rejects_embedding_dimension_mismatch(self):
        with self.assertRaisesRegex(ValueError, r"(?i)(dimension|shape|size)"):
            LocalIndex(self.path, WrongDimensionEmbeddings(), embedding_model=MODEL_NAME)

    def test_manifest_embedding_model_and_dimension_mismatches_are_rejected(self):
        path = self.path / "manifest.json"
        original = path.read_bytes()
        manifest = json.loads(original)
        self.assertEqual(manifest["embedding_model"], MODEL_NAME)
        self.assertEqual(manifest["dimension"], 2)
        for key, value, message in (("embedding_model", "wrong-model", "model"),
                                    ("dimension", 3, "dimension")):
            with self.subTest(key=key):
                changed = dict(manifest, **{key: value})
                try:
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, rf"(?i){message}"):
                        self.reopen()
                finally:
                    path.write_bytes(original)

    def test_corrupt_file_hash_is_rejected_before_loading(self):
        for filename in ("vectors.npy", "chunks.jsonl", "sparse.npz", "bm25.json"):
            with self.subTest(filename=filename):
                artifact, = (self.path / "generations").glob(f"*/{filename}")
                original = artifact.read_bytes()
                try:
                    artifact.write_bytes(original + b"corruption")
                    with self.assertRaisesRegex(ValueError, r"(?i)(hash|checksum|integrity|corrupt)"):
                        self.reopen()
                finally:
                    artifact.write_bytes(original)

    def test_failed_atomic_manifest_publication_leaves_old_version_readable(self):
        manifest = self.path / "manifest.json"
        original_manifest = manifest.read_bytes()
        expected = self.index.search("ZXQCODE library")
        replace = os.replace
        attempted = []

        def fail_manifest(source, destination, *args, **kwargs):
            if Path(destination).resolve() == manifest.resolve():
                attempted.append(destination)
                raise OSError("injected manifest publication failure")
            return replace(source, destination, *args, **kwargs)

        with patch("rag.local_index.os.replace", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.build([make_document("replacement office catalog", "replacement")])
        self.assertTrue(attempted, "The failure must occur at the manifest commit point")
        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assertEqual(self.reopen().search("ZXQCODE library"), expected)
        self.assertEqual(self.index.search("ZXQCODE library"), expected)

    def test_concurrent_search_reload_observes_only_complete_generations(self):
        reader = self.reopen(reload_interval=0)
        old_ids = frozenset(self.ids(self.documents))
        new_documents = [make_document("library replacement office", "new-one"),
                         make_document("replacement library sports", "new-two")]
        new_ids = frozenset(self.ids(new_documents))
        new_encoder = BM25Encoder().fit([d.page_content for d in new_documents])
        expected_scores = {}
        query_vector = self.embeddings.embed_query("library")
        for documents, encoder in ((self.documents, self.encoder), (new_documents, new_encoder)):
            sparse_query = encoder.encode_queries("library")
            for document in documents:
                expected_scores[document.metadata["chunk_id"]] = (
                    0.5 * float(np.dot(query_vector, self.embeddings.embed_query(document.page_content)))
                    + 0.5 * sparse_dot(sparse_query, encoder.encode_documents(document.page_content))
                )
        barrier = threading.Barrier(5)
        observations = []

        def search():
            barrier.wait(timeout=10)
            seen = []
            for _ in range(100):
                result = reader.search("library", top_k=30, alpha=0.5)
                ids = frozenset(self.ids(result))
                self.assertIn(ids, (old_ids, new_ids))
                self.assertEqual(len(result), len(ids))
                self.assertTrue(all(np.isfinite(d.metadata["score"]) for d in result))
                for document in result:
                    self.assertAlmostEqual(document.metadata["score"],
                                           expected_scores[document.metadata["chunk_id"]], places=5)
                snapshot = reader.snapshot()
                self.assertEqual(len(snapshot.documents), snapshot.dense.shape[0])
                self.assertEqual(len(snapshot.documents), snapshot.sparse.shape[0])
                seen.append(ids)
            return seen

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(search) for _ in range(4)]
            barrier.wait(timeout=10)
            for _ in range(3):
                self.build(new_documents, new_encoder)
                self.build(self.documents, self.encoder)
            self.build(new_documents, new_encoder)
            for future in futures:
                observations.extend(future.result(timeout=45))
        self.assertTrue(observations)
        self.assertEqual(frozenset(self.ids(reader.search("library", alpha=1))), new_ids)

    def test_concurrent_thread_upserts_on_independent_readers_have_no_lost_updates(self):
        indexes = [self.reopen(reload_interval=0) for _ in range(8)]
        barrier = threading.Barrier(len(indexes))

        def upsert(number):
            barrier.wait(timeout=10)
            indexes[number].upsert([make_document(f"office concurrent row {number}",
                                                  f"thread-{number}")])

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexes)) as pool:
            list(pool.map(upsert, range(len(indexes))))
        expected = set(self.ids(self.documents)) | {f"thread-{n}" for n in range(len(indexes))}
        documents = self.reopen().snapshot().documents
        self.assertEqual(set(self.ids(documents)), expected)
        self.assertEqual(len(documents), len(expected))

    def test_concurrent_process_upserts_have_no_lost_updates(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(3)
        processes = [context.Process(target=process_upsert, args=(str(self.path), i, barrier))
                     for i in range(3)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=60)
                self.assertFalse(process.is_alive(), "Cross-process upsert timed out")
                self.assertEqual(process.exitcode, 0, "Cross-process upsert failed")
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
        expected = set(self.ids(self.documents)) | {f"process-{i}" for i in range(3)}
        documents = self.reopen().snapshot().documents
        self.assertEqual(set(self.ids(documents)), expected)
        self.assertEqual(len(documents), len(expected))


class LocalChainIntegrationTests(unittest.TestCase):
    def test_real_store_build_append_and_delete_without_remote_services(self):
        script = r'''
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_local_index import FakeEmbeddings
from rag.local_index import LocalIndex, MODEL_NAME
root = Path(sys.argv[1])
data = root / "data"
data.mkdir()
os.environ.update(VECTOR_BACKEND="local", LOCAL_INDEX_DIR=str(root / "index"),
                  STORE_BACKEND="file", DOC_FRESHNESS_FILE=str(root / "dates.json"),
                  SC_SPELL_VOCAB=str(root / "vocabulary.json"))
os.environ.pop("PINECONE_API_KEY", None)
s3 = types.ModuleType("aws.s3")
s3.upload_file_to_s3 = lambda *args, **kwargs: False
sys.modules["aws.s3"] = s3
embeddings = FakeEmbeddings()
embeddings.model_name = MODEL_NAME
text = ("Student enrollment requires an application form and official school records. "
        "The registrar verifies documents before classes begin. Students must pay tuition "
        "through the accounting office and retain their receipts. Contact the registrar "
        "for the official enrollment schedule and the student handbook for academic policies. ") * 4
(data / "enrollment.txt").write_text(text, encoding="utf-8")
with patch("dotenv.load_dotenv", return_value=False), \
     patch("socket.socket.connect", side_effect=AssertionError("No remote services")), \
     patch("pinecone.Pinecone", side_effect=AssertionError("No hosted index")), \
     patch("pinecone_text.sparse.BM25Encoder.default", side_effect=AssertionError("No default corpus download")):
    import store_index
    store_index.BM25_VALUES_FILE = str(root / "bm25.json")
    store_index.build_index(str(data), embeddings=embeddings)
    index = LocalIndex(root / "index", embeddings)
    assert index.snapshot().documents
    assert (root / "bm25.json").exists()
    extra = root / "new.txt"
    extra.write_text(text.replace("enrollment", "scholarship"), encoding="utf-8")
    count = store_index.append_file_to_index(str(extra), real_name="scholarships.txt", embeddings=embeddings)
    assert count > 0
    reloaded = LocalIndex(root / "index", embeddings)
    assert reloaded.search("scholarship", filter={"source": {"$eq": "scholarships.txt"}})
    reloaded.delete_by_source("scholarships.txt")
    assert not reloaded.search("scholarship", filter={"source": {"$eq": "scholarships.txt"}})
    assert reloaded.search("enrollment", filter={"source": {"$eq": "enrollment.txt"}})
print("LOCAL_INGESTION_OK")
'''
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run([sys.executable, "-c", script, temporary], cwd=ROOT,
                                       env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                       capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        self.assertIn("LOCAL_INGESTION_OK", completed.stdout)

    def test_real_chain_import_and_retrieval_without_pinecone_key(self):
        # A fresh interpreter prevents fake external clients leaking into other suites.
        script = r'''
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_local_index import FakeEmbeddings, make_document
from langchain_core.retrievers import BaseRetriever
from pinecone_text.sparse import BM25Encoder
from rag.local_index import LocalIndex, LocalHybridSearchRetriever, MODEL_NAME as INDEX_MODEL_NAME

path = Path(sys.argv[1])
documents = [make_document("ZXQCODE scholarship office", "acronym", "scholarships.pdf"),
             make_document("library reading services", "dense", "library.pdf")]
encoder = BM25Encoder().fit([d.page_content for d in documents])
embeddings = FakeEmbeddings()
# The real chain selects its configured model; fake only inference, not retrieval.
embeddings.model_name = INDEX_MODEL_NAME
LocalIndex.build(path, documents, embeddings, encoder, embedding_model=INDEX_MODEL_NAME)
os.environ.pop("PINECONE_API_KEY", None)
os.environ.update(VECTOR_BACKEND="local", LOCAL_INDEX_DIR=str(path),
                  STORE_BACKEND="file",
                  RERANKER_ENABLED="false", RERANKER_BACKEND="local",
                  OPENROUTER_API_KEY="fake-test-key", BM25_VALUES_FILE=str(path / "unused.json"))
helper = types.ModuleType("src.helper")
helper.get_local_embeddings = lambda: embeddings
helper.MODEL_NAME = INDEX_MODEL_NAME
sys.modules["src.helper"] = helper
aws_db = types.ModuleType("aws.dynamodb")
aws_db.get_file_upload_dates = lambda: {}
sys.modules["aws.dynamodb"] = aws_db
students = types.ModuleType("aws.students")
students.get_student_by_uid = lambda *args, **kwargs: None
students.get_student_by_email = lambda *args, **kwargs: None
students.format_student_context = lambda *args, **kwargs: ""
sys.modules["aws.students"] = students

with patch("dotenv.load_dotenv", return_value=False), \
     patch("langchain_openai.ChatOpenAI", return_value=MagicMock()), \
     patch("pinecone.Pinecone", side_effect=AssertionError("Pinecone must not be constructed")):
    import config
    assert not config.PINECONE_API_KEY
    import rag.chain as chain
    assert isinstance(chain.index, LocalIndex)
    for leaf in (chain.sparse_retriever, chain.dense_retriever):
        assert isinstance(leaf, BaseRetriever)
        assert isinstance(leaf, LocalHybridSearchRetriever)
        results = leaf.invoke("ZXQCODE", filter={"source": {"$eq": "scholarships.pdf"}})
        assert [d.metadata["chunk_id"] for d in results] == ["acronym"]
    assert chain.sparse_retriever.invoke("ZXQCODE")[0].metadata["chunk_id"] == "acronym"
    assert chain.dense_retriever.invoke("ZXQCODE")[0].metadata["chunk_id"] == "dense"
    assert {d.metadata["chunk_id"] for d in chain.retriever.invoke("ZXQCODE")} == {"acronym", "dense"}
    results = chain.multi_query_retrieve("ZXQCODE", filtered_queries=[
        ("ZXQCODE", {"source": {"$eq": "scholarships.pdf"}})])
    assert [d.metadata["chunk_id"] for d in results] == ["acronym"]
    results = chain.retrieve_documents("ZXQCODE", top_k=2, dates={}, uploads={},
                                       allow_spelling=False)
    assert "acronym" in {d.metadata["chunk_id"] for d in results}
    assert "rag.chain" in sys.modules
print("LOCAL_CHAIN_INTEGRATION_OK")
'''
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ)
            environment.pop("PINECONE_API_KEY", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            completed = subprocess.run([sys.executable, "-c", script,
                                        str(Path(temporary) / "index")], cwd=ROOT,
                                       env=environment, capture_output=True, text=True,
                                       encoding="utf-8", timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        self.assertIn("LOCAL_CHAIN_INTEGRATION_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
