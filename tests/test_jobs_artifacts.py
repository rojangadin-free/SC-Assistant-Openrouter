"""Durable queue ownership and shared artifact tests with fake S3 only."""

import _bootstrap  # noqa: F401
import ast
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import List, Callable, Optional
from unittest.mock import patch

from rag import artifacts, indexing, jobs, store
from rag.index_client import TimedIndex
from test_storage_scaling import Table


class Upload:
    def __init__(self, name):
        self.filename = name

    def save(self, path):
        with open(path, "wb") as handle:
            handle.write(b"sample school document")


class S3:
    def __init__(self):
        self.objects = {}
        self.reads = 0
        self.corrupt = False
        self.fail_upload = False

    def upload_file(self, path, bucket, key, **kwargs):
        with open(path, "rb") as handle:
            self.objects[key] = handle.read()
        if self.fail_upload:
            raise OSError("simulated staging failure")

    def download_file(self, bucket, key, path):
        self.reads += 1
        with open(path, "wb") as handle:
            handle.write(b"corrupt" if self.corrupt else self.objects[key])

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


class JobArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sc_job_tests_")
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "STORE_BACKEND": "file", "INDEX_JOBS_FILE": os.path.join(self.temp.name, "jobs.json"),
            "INDEX_STAGING_DIR": os.path.join(self.temp.name, "staging"),
            "ARTIFACT_CACHE_DIR": self.temp.name,
            "ARTIFACT_MANIFEST_FILE": os.path.join(self.temp.name, "manifest.json"),
            "SC_SPELL_VOCAB": os.path.join(self.temp.name, "spell_vocab.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        artifacts._checked_at, artifacts._generation = 0, "bundled"
        indexing._embeddings = None

    def test_stage_survives_new_store_object_and_hides_private_payload(self):
        job_id = indexing.stage_uploads([Upload("directory.txt")], "admin-id")
        jobs._store_obj = None
        job = jobs.claim_next("worker-a")
        self.assertEqual(job["job_id"], job_id)
        self.assertTrue(os.path.exists(job["staged"][0]["path"]))
        self.assertIsNone(jobs.claim_next("worker-b"))
        self.assertNotIn("staged", jobs.get_job(job_id))
        self.assertNotIn("uid", jobs.get_job(job_id))

    def test_expired_claim_recovers_and_old_owner_cannot_write(self):
        job_id = indexing.stage_uploads([Upload("directory.txt")], "admin-id")
        first = jobs.claim_next("worker-a")
        with patch.object(jobs.time, "time", return_value=first["lease_until"] + 1):
            second = jobs.claim_next("worker-b")
            self.assertEqual(second["attempts"], 2)
            with self.assertRaises(jobs.LeaseLost):
                jobs.finish_file(job_id, "directory.txt", 5, owner="worker-a")
            with self.assertRaises(ValueError):
                jobs.finish_job(job_id, owner="worker-b")
            jobs.finish_file(job_id, "directory.txt", 5, owner="worker-b")
            jobs.finish_job(job_id, owner="worker-b")
        indexing.cleanup_staging()
        self.assertEqual(jobs.get_job(job_id)["status"], "done")
        self.assertNotIn("staged", jobs._store().load()["jobs"][job_id])

    def test_retry_limit_marks_unfinished_files_failed(self):
        job_id = indexing.stage_uploads([Upload("directory.txt")], "admin")
        with patch.dict(os.environ, {"INDEX_JOB_MAX_ATTEMPTS": "1"}):
            job = jobs.claim_next("worker-a")
            with patch.object(jobs.time, "time", return_value=job["lease_until"] + 1):
                self.assertIsNone(jobs.claim_next("worker-b"))
                status = jobs.get_job(job_id)
                self.assertTrue(status["done"])
                self.assertEqual(status["files"][0]["status"], "error")
                with self.assertRaises(jobs.LeaseLost):
                    jobs.finish_file(job_id, "directory.txt", 1, owner="worker-a")

    def test_staging_failure_is_not_accepted_and_compensates_uploaded_object(self):
        cloud, table = S3(), Table()
        cloud.fail_upload = True
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), \
                patch.object(store, "_table", return_value=table), \
                patch.object(indexing, "_cloud", return_value=(cloud, "bucket")):
            with self.assertRaises(OSError):
                indexing.stage_uploads([Upload("directory.txt")], "admin")
        self.assertEqual(cloud.objects, {})
        self.assertEqual(table.items, {})

    def test_worker_indexes_then_stores_then_registers_and_completes(self):
        job_id = indexing.stage_uploads([Upload("directory.txt")], "admin")
        order = []

        def append(path, **kwargs):
            self.assertFalse(kwargs["publish_shared"])
            kwargs["progress"]("indexing", 1, 1)
            order.append("index")
            with open(artifacts.artifact_path("spell_vocab.json"), "w", encoding="utf-8") as handle:
                json.dump({"terms": {"directory": 3}}, handle)
            return 2

        modules = {
            "config": SimpleNamespace(INDEX_NAME="fake-index"),
            "store_index": SimpleNamespace(append_file_to_index=append),
            "src.helper": SimpleNamespace(get_local_embeddings=lambda: object()),
            "aws.dynamodb": SimpleNamespace(save_file_metadata=lambda *a: order.append("metadata")),
            "aws.s3": SimpleNamespace(upload_file_to_s3=lambda *a: order.append("file") or True),
        }
        with patch.dict(sys.modules, modules):
            self.assertTrue(indexing.run_once("worker-a"))
        self.assertEqual(order, ["index", "file", "metadata"])
        self.assertEqual(jobs.get_job(job_id)["status"], "done")

    def test_artifact_hash_refresh_cache_and_failed_download_preserve_file(self):
        cloud, table = S3(), Table()
        source = os.path.join(self.temp.name, "source.json")
        with open(source, "wb") as handle:
            handle.write(b'{"terms":{"directory":3}}')
        target = artifacts.artifact_path("spell_vocab.json")
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb", "ARTIFACT_REFRESH_SECONDS": "999"}), \
                patch.object(store, "_table", return_value=table), \
                patch.object(artifacts, "_cloud", return_value=(cloud, "bucket")):
            artifacts.publish_artifact(source, "spell_vocab.json")
            generation = artifacts.refresh_artifacts(force=True, strict=True)
            self.assertNotEqual(generation, "bundled")
            for _ in range(30):
                self.assertEqual(artifacts.refresh_artifacts(), generation)
            self.assertEqual(cloud.reads, 1)
            with open(target, "rb") as handle:
                original = handle.read()
            with open(source, "wb") as handle:
                handle.write(b'{"terms":{"directory":4}}')
            artifacts.publish_artifact(source, "spell_vocab.json")
            cloud.corrupt = True
            with self.assertRaises(ValueError):
                artifacts.refresh_artifacts(force=True, strict=True)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), original)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.temp.name)))

    def test_spelling_cache_notices_another_process_replacing_vocabulary(self):
        import rag.spelling as spelling
        importlib.reload(spelling)
        self.assertEqual(spelling.correct_query("zormpatg"), "")
        temporary = spelling.SPELL_VOCAB_FILE + ".replace"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"terms": {"zormpath": 5}}, handle)
        os.replace(temporary, spelling.SPELL_VOCAB_FILE)
        self.assertEqual(spelling.correct_query("zormpatg"), "zormpath")

    def test_failed_second_artifact_does_not_replace_the_first(self):
        cloud, table = S3(), Table()
        paths = {}
        for name in ("bm25_values.json", "spell_vocab.json"):
            path = os.path.join(self.temp.name, "source-" + name)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": "new"}, handle)
            paths[name] = path
            with open(artifacts.artifact_path(name), "w", encoding="utf-8") as handle:
                json.dump({"version": "old"}, handle)
        download = cloud.download_file

        def fail_second(bucket, key, path):
            if key.endswith("spell_vocab.json"):
                raise OSError("second artifact unavailable")
            download(bucket, key, path)

        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), \
                patch.object(store, "_table", return_value=table), \
                patch.object(artifacts, "_cloud", return_value=(cloud, "bucket")):
            artifacts.publish_artifacts(paths)
            with patch.object(cloud, "download_file", side_effect=fail_second):
                self.assertEqual(artifacts.refresh_artifacts(force=True), "bundled")
            for name in paths:
                with open(artifacts.artifact_path(name), encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle), {"version": "old"})

    def test_failed_manifest_read_keeps_last_good_generation(self):
        artifacts._generation = "last-good"
        with patch.object(artifacts, "_manifest") as manifest:
            manifest.return_value.mutate.side_effect = OSError("store unavailable")
            self.assertEqual(artifacts.refresh_artifacts(force=True), "last-good")
            with self.assertRaises(OSError):
                artifacts.refresh_artifacts(force=True, strict=True)

    def test_recovered_indexing_does_not_double_learn_vocabulary(self):
        import rag.spelling as spelling
        importlib.reload(spelling)
        spelling.learn_terms(["registrar directory directory"], source="handbook.txt")
        spelling.learn_terms(["registrar directory directory"], source="handbook.txt")
        with open(spelling.SPELL_VOCAB_FILE, encoding="utf-8") as handle:
            learned = json.load(handle)
        self.assertEqual(learned["terms"]["directory"], 2)

    def test_actual_append_uses_stable_ids_and_blocks_writes_after_lease_loss(self):
        with open("store_index.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        nodes = [node for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name == "append_file_to_index"
        ) or (isinstance(node, ast.Import) and any(alias.name == "hashlib" for alias in node.names))]
        calls = []
        lost = [False]
        raw = SimpleNamespace(delete=lambda **kw: calls.append("delete"),
                              upsert=lambda vectors, **kw: calls.append(vectors))
        doc = SimpleNamespace(page_content="Current school directory", metadata={"source": "directory.txt"})

        class Retriever:
            def __init__(self, **kwargs):
                self.index = kwargs["index"]

            def add_texts(self, texts, ids, metadatas):
                lost[0] = fail_after_embedding[0]
                self.index.upsert([{"id": identity} for identity in ids])

        def progress(*args):
            if lost[0]:
                raise jobs.LeaseLost("lease expired during embedding")

        namespace = {
            "List": List, "Document": SimpleNamespace, "Callable": Callable, "Optional": Optional,
            "PINECONE_API_KEY": "offline", "Pinecone": lambda **kw: SimpleNamespace(Index=lambda _: raw),
            "PineconeHybridSearchRetriever": Retriever,
            "get_embedding_dimension": lambda _: 384, "ensure_index_exists": lambda *a, **kw: None,
            "TimedIndex": TimedIndex, "BM25_VALUES_FILE": os.path.join(self.temp.name, "absent.json"),
            "BM25Encoder": lambda: SimpleNamespace(default=lambda: object()), "os": os,
            "get_loader": lambda _: SimpleNamespace(load=lambda: [doc]), "clean_metadata": lambda _: None,
            "_chunk_and_dedup": lambda docs: docs, "batched": lambda docs, size: [docs],
            "_record_effective_date": lambda *a: None, "_learn_spelling_terms": lambda _: calls.append("learn"),
            "logger": SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "store_index.py", "exec"), namespace)
        fail_after_embedding = [False]
        count = namespace["append_file_to_index"]("directory.txt", embeddings=object(),
                                                   progress=progress, publish_shared=False)
        self.assertEqual(count, 1)
        self.assertEqual(calls[1][0]["id"], hashlib.sha256(b"directory.txt\nCurrent school directory").hexdigest())
        self.assertEqual(calls[-1], "learn")
        calls.clear()
        fail_after_embedding[0] = True
        with self.assertRaises(jobs.LeaseLost):
            namespace["append_file_to_index"]("directory.txt", embeddings=object(), progress=progress, publish_shared=False)
        self.assertEqual(calls, ["delete"])


if __name__ == "__main__":
    unittest.main()
