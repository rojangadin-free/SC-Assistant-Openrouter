"""Run separately from Gunicorn; SIGTERM leaves recoverable staged jobs."""

import os
import signal
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import config  # noqa: F401
    from rag.indexing import run_once
    from rag.runtime import env_float
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    owner = uuid.uuid4().hex
    while not stop.is_set():
        try:
            worked = run_once(owner, stop)
        except Exception as exc:
            print(f"[index-worker] task will retry if interrupted: {type(exc).__name__}", flush=True)
            worked = False
        if not worked:
            stop.wait(env_float("INDEX_POLL_SECONDS", 5, 0.5))


if __name__ == "__main__":
    main()
