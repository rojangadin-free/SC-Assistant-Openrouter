"""Preview or publish the existing local retrieval files, never rebuild an index."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Upload existing files to shared S3/DynamoDB state")
    args = parser.parse_args(argv)
    import config
    from rag import artifacts, jobs

    paths, summaries = {}, {}
    for name in sorted(artifacts.NAMES):
        path = artifacts.artifact_path(name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if name == "bm25_values.json":
            if not data.get("n_docs") or not data.get("avgdl") or not data.get("doc_freq"):
                raise ValueError("BM25 file must contain fitted corpus statistics")
            count = data["n_docs"]
        else:
            if not isinstance(data.get("terms"), dict) or not data["terms"]:
                raise ValueError("The local spelling vocabulary is empty or invalid")
            count = len(data["terms"])
        paths[name] = path
        summaries[name] = {"sha256": artifacts._sha(path), "entries": count}

    result = {"published": False, "index": config.INDEX_NAME, "artifacts": summaries}
    if args.publish:
        if config.STORE_BACKEND != "dynamodb":
            raise ValueError("Use the shared DynamoDB backend to publish production artifacts")
        active = jobs._store().mutate(lambda _: False).get("jobs") or {}
        if any(jobs._settle(dict(job)).get("status") in ("queued", "running") for job in active.values()):
            raise RuntimeError("Wait for active uploads/indexing to finish before syncing local artifacts")
        before = artifacts._manifest().mutate(lambda _: False)
        artifacts.publish_artifacts(paths, expected_generation=before.get("generation"))
        after = artifacts._manifest().mutate(lambda _: False)
        for name, summary in summaries.items():
            reference = after.get("artifacts", {}).get(name, {})
            if reference.get("sha256") != summary["sha256"]:
                raise RuntimeError("Shared artifact verification failed")
            client, bucket = artifacts._cloud()
            # Check the uploaded object's existence without printing credentials,
            # the vocabulary, or any document/student content.
            if client.head_object(Bucket=bucket, Key=reference["key"])["ContentLength"] != os.path.getsize(paths[name]):
                raise RuntimeError("Published artifact size does not match the local file")
        result.update(published=True, generation=after.get("generation"))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
