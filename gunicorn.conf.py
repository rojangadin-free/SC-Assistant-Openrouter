"""HTTP concurrency is configurable independently of expensive RAG work."""

import os


def integer(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


workers = integer("GUNICORN_WORKERS", 3)
workers_file = os.getenv("GUNICORN_WORKERS_FILE")
if workers_file:
    with open(workers_file, encoding="utf-8") as handle:
        workers = max(1, int(handle.read().strip()))
threads = integer("GUNICORN_THREADS", 12)
worker_class = "gthread"
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8080")
timeout = integer("GUNICORN_TIMEOUT", 120)
graceful_timeout = integer("GUNICORN_GRACEFUL_TIMEOUT", 120)
keepalive = 5
max_requests = integer("GUNICORN_MAX_REQUESTS", 400)
max_requests_jitter = integer("GUNICORN_MAX_REQUESTS_JITTER", 50, 0)
preload_app = False
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
accesslog = "-"
errorlog = "-"
capture_output = True


def post_worker_init(worker):
    if workers_file:
        with open(os.path.join(os.path.dirname(workers_file), "ready-" + str(os.getpid())), "w") as handle:
            handle.write("ready")


def child_exit(server, worker):
    if workers_file:
        path = os.path.join(os.path.dirname(workers_file), "ready-" + str(worker.pid))
        if os.path.exists(path):
            os.remove(path)
