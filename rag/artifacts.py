"""Versioned retrieval artifacts on existing S3/DynamoDB, with local fallback."""

import hashlib
import json
import os
import threading
import time
import uuid

from rag.runtime import env_float
from rag.store import JsonBlobStore, _file_lock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"bm25_values.json", "spell_vocab.json"}
_lock = threading.Lock()
_checked_at = 0.0
_generation = "bundled"
_ANY_GENERATION = object()


def artifact_path(name):
    if name not in NAMES:
        raise ValueError("Unknown retrieval artifact")
    if name == "spell_vocab.json" and os.getenv("SC_SPELL_VOCAB"):
        return os.path.abspath(os.environ["SC_SPELL_VOCAB"])
    return os.path.join(os.getenv("ARTIFACT_CACHE_DIR", ROOT), name)


def _manifest():
    return JsonBlobStore("retrieval_artifacts", os.getenv("ARTIFACT_MANIFEST_FILE", os.path.join(ROOT, "retrieval_artifacts.json")),
                         lambda: {"artifacts": {}})


def _cloud():
    import boto3
    from botocore.config import Config
    from config import AWS_REGION, S3_BUCKET_NAME
    return boto3.client("s3", region_name=AWS_REGION, config=Config(
        connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}
    )), S3_BUCKET_NAME


def _prefix():
    return os.getenv("ARTIFACT_S3_PREFIX", "_sc_assistant/artifacts").strip("/")


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def publish_artifact(path, name):
    return publish_artifacts({name: path})


def publish_artifacts(paths, *, expected_generation=_ANY_GENERATION):
    references = {}
    for name, path in paths.items():
        if name not in NAMES:
            raise ValueError("Unknown retrieval artifact")
        digest = _sha(path)
        key = _prefix() + "/" + digest + "/" + name
        if os.getenv("STORE_BACKEND", "file").lower() == "dynamodb":
            client, bucket = _cloud()
            client.upload_file(path, bucket, key)
        references[name] = {"sha256": digest, "key": key}

    def publish(data):
        if expected_generation is not _ANY_GENERATION and data.get("generation") != expected_generation:
            raise RuntimeError("Shared artifacts changed during publication; inspect them before retrying")
        data.setdefault("artifacts", {}).update(references)
        data["generation"] = uuid.uuid4().hex
    _manifest().mutate(publish)


def mark_index_changed():
    _manifest().mutate(lambda data: data.update(generation=uuid.uuid4().hex))


def refresh_artifacts(force=False, strict=False):
    global _checked_at, _generation
    with _lock:
        now = time.monotonic()
        if not force and now - _checked_at < env_float("ARTIFACT_REFRESH_SECONDS", 10):
            return _generation
        staged = []
        try:
            lock_path = artifact_path("bm25_values.json") + ".refresh"
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            # Serialize readers sharing a container's writable layer. A slower
            # old refresh must not overwrite files installed by a newer worker.
            with _file_lock(lock_path):
                manifest = _manifest().mutate(lambda _: False)
                if os.getenv("STORE_BACKEND", "file").lower() == "dynamodb":
                    for name, info in (manifest.get("artifacts") or {}).items():
                        if name not in NAMES:
                            raise ValueError("Invalid artifact manifest name")
                        expected = info.get("sha256", "")
                        key = info.get("key", "")
                        if (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)
                                or key != _prefix() + "/" + expected + "/" + name):
                            raise ValueError("Invalid artifact manifest reference")
                        target = artifact_path(name)
                        if os.path.exists(target) and _sha(target) == expected:
                            continue
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        temporary = target + "." + uuid.uuid4().hex + ".tmp"
                        staged.append((temporary, target))
                        client, bucket = _cloud()
                        client.download_file(bucket, key, temporary)
                        if _sha(temporary) != expected:
                            raise ValueError("Retrieval artifact failed its integrity check")
                        with open(temporary, encoding="utf-8") as handle:
                            if not isinstance(json.load(handle), dict):
                                raise ValueError("Retrieval artifact is not a JSON object")
                    # Download/validate the complete set before replacing any
                    # usable file. A corrupt second artifact changes nothing.
                    for temporary, target in staged:
                        os.replace(temporary, target)
                _generation = manifest.get("generation") or "bundled"
            _checked_at = time.monotonic()
        except Exception:
            if strict:
                raise
            # Keep the last usable generation; retry on the next refresh window.
            _checked_at = time.monotonic()
        finally:
            for temporary, _ in staged:
                if os.path.exists(temporary):
                    os.remove(temporary)
        return _generation
