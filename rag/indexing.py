"""Durable staged inputs and a leased, at-least-once indexing worker."""

import mimetypes
import os
import shutil
import tempfile
import threading
import uuid

from rag import jobs
from rag.artifacts import refresh_artifacts, publish_artifacts, artifact_path

_embeddings = None


def _staging_root():
    return os.getenv("INDEX_STAGING_DIR", os.path.join(tempfile.gettempdir(), "sc-assistant-indexing"))


def _cloud():
    from aws.s3 import s3_client
    from config import S3_BUCKET_NAME
    return s3_client, S3_BUCKET_NAME


def stage_uploads(files, uid):
    names = [os.path.basename((item.filename or "").replace("\\", "/")) for item in files]
    if not names or len(names) > 10 or len(set(names)) != len(names):
        raise ValueError("Send 1-10 files with distinct filenames")
    if any(not name or len(name) > 240 or os.path.splitext(name)[1].lower() not in
           {".pdf", ".docx", ".txt", ".md", ".csv"} for name in names):
        raise ValueError("Unsupported filename or file type")
    token = uuid.uuid4().hex
    shared = os.getenv("STORE_BACKEND", "file").lower() == "dynamodb"
    root = os.path.join(_staging_root(), token)
    os.makedirs(root, exist_ok=True)
    staged = []
    try:
        for uploaded, name in zip(files, names):
            local = os.path.join(root, name)
            uploaded.save(local)
            item = {"filename": name}
            if shared:
                key = "_sc_assistant/indexing/" + token + "/" + name
                item["key"] = key
                staged.append(item)
                client, bucket = _cloud()
                client.upload_file(local, bucket, key, ExtraArgs={
                    "ContentType": mimetypes.guess_type(name)[0] or "application/octet-stream"
                })
            else:
                item["path"] = local
                staged.append(item)
        job_id = jobs.create_job(names, staged=staged, uid=uid)
    except BaseException:
        if shared:
            for item in staged:
                try:
                    client, bucket = _cloud()
                    client.delete_object(Bucket=bucket, Key=item["key"])
                except Exception:
                    pass
        shutil.rmtree(root, ignore_errors=True)
        raise
    if shared:
        shutil.rmtree(root, ignore_errors=True)
    return job_id


def cleanup_staging():
    for job in jobs.terminal_staging():
        try:
            for item in job["staged"]:
                if item.get("key"):
                    client, bucket = _cloud()
                    client.delete_object(Bucket=bucket, Key=item["key"])
                elif item.get("path"):
                    parent = os.path.dirname(item["path"])
                    if os.path.commonpath([os.path.abspath(parent), os.path.abspath(_staging_root())]) != os.path.abspath(_staging_root()):
                        raise ValueError("Invalid staging path")
                    shutil.rmtree(parent, ignore_errors=True)
            jobs.staging_cleaned(job["job_id"])
        except Exception as exc:
            print(f"[index-worker] staging cleanup will retry: {type(exc).__name__}")


def run_once(owner, stop=None):
    global _embeddings
    cleanup_staging()
    job = jobs.claim_next(owner)
    if not job:
        return False
    stop = stop or threading.Event()
    heartbeat_stop = threading.Event()
    lost = threading.Event()

    def renew():
        while not heartbeat_stop.wait(10):
            if stop.is_set():
                return
            try:
                jobs.heartbeat(job["job_id"], owner)
            except Exception:
                lost.set()
                return

    heartbeat = threading.Thread(target=renew, name="index-lease", daemon=True)
    heartbeat.start()
    try:
        from config import INDEX_NAME
        from store_index import append_file_to_index
        from src.helper import get_local_embeddings
        from aws.dynamodb import save_file_metadata
        if _embeddings is None:
            _embeddings = get_local_embeddings()
        for item in job["staged"]:
            name = item["filename"]
            if any(f["filename"] == name and f.get("status") in ("done", "error") for f in job["files"]):
                continue

            def progress(phase, done=0, total=0):
                if stop.is_set() or lost.is_set():
                    raise jobs.LeaseLost("Indexing interrupted; the staged task will be recovered")
                try:
                    jobs.set_phase(job["job_id"], name, phase, units_done=done or None,
                                   units_total=total or None, owner=owner)
                except Exception as exc:
                    raise jobs.LeaseLost("Indexing ownership could not be verified") from exc

            try:
                progress("queued")
                refresh_artifacts(force=True, strict=True)
                with tempfile.TemporaryDirectory(prefix="sc_index_work_") as working:
                    path = os.path.join(working, name)
                    if item.get("key"):
                        client, bucket = _cloud()
                        client.download_file(bucket, item["key"], path)
                    else:
                        shutil.copyfile(item["path"], path)
                    chunks = append_file_to_index(path, real_name=name, embeddings=_embeddings,
                                                  index_name=INDEX_NAME, progress=progress,
                                                  publish_shared=False)
                    if chunks <= 0:
                        raise ValueError("No readable passages were indexed")
                    progress("storing")
                    if item.get("key"):
                        client, bucket = _cloud()
                        client.copy_object(Bucket=bucket, Key=name,
                                           CopySource={"Bucket": bucket, "Key": item["key"]})
                    else:
                        from aws.s3 import upload_file_to_s3
                        if not upload_file_to_s3(path, name):
                            raise RuntimeError("File storage failed")
                    if stop.is_set() or lost.is_set():
                        raise jobs.LeaseLost("Indexing interrupted before publication")
                    jobs.heartbeat(job["job_id"], owner)
                    save_file_metadata(name, job.get("uid"))
                    paths = {"spell_vocab.json": artifact_path("spell_vocab.json")}
                    if os.path.exists(artifact_path("bm25_values.json")):
                        paths["bm25_values.json"] = artifact_path("bm25_values.json")
                    publish_artifacts(paths)
                    jobs.finish_file(job["job_id"], name, chunks, owner=owner)
            except jobs.LeaseLost:
                raise
            except ValueError as exc:
                jobs.fail_file(job["job_id"], name, str(exc), owner=owner)
        jobs.finish_job(job["job_id"], owner=owner)
    except BaseException:
        jobs.release(job["job_id"], owner)
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
    cleanup_staging()
    return True
