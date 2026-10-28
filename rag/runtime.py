"""Bounded per-process resources; no models, credentials or network at import."""

import copy
import os
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager


class CapacityError(RuntimeError):
    """An expensive-work queue is full, not a missing knowledge-base answer."""


def env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def env_float(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


class BoundedPool:
    def __init__(self, workers, pending, name):
        self._slots = threading.BoundedSemaphore(workers + pending)
        self._tasks = queue.Queue(maxsize=workers + pending)
        self._lock = threading.Lock()
        self._closing = False
        self._threads = [threading.Thread(target=self._run, name=name + "-" + str(i), daemon=True)
                         for i in range(workers)]
        for thread in self._threads:
            thread.start()

    def _run(self):
        while True:
            task = self._tasks.get()
            if task is None:
                self._tasks.task_done()
                return
            future, fn, args, kwargs = task
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self._slots.release()
                self._tasks.task_done()

    def submit(self, fn, *args, queue_timeout=None, **kwargs):
        timeout = env_float("RAG_QUEUE_TIMEOUT", 10) if queue_timeout is None else queue_timeout
        if not self._slots.acquire(timeout=timeout):
            raise CapacityError("The assistant is busy. Please try again shortly.")
        try:
            with self._lock:
                if self._closing:
                    raise RuntimeError("Worker pool is closed")
                future = Future()
                self._tasks.put_nowait((future, fn, args, kwargs))
        except BaseException:
            self._slots.release()
            raise
        return future

    def shutdown(self, wait=True):
        with self._lock:
            self._closing = True
            while True:
                try:
                    future, _, _, _ = self._tasks.get_nowait()
                    future.cancel()
                    future.set_running_or_notify_cancel()
                    self._slots.release()
                    self._tasks.task_done()
                except queue.Empty:
                    break
            for _ in self._threads:
                self._tasks.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


_resource_lock = threading.Lock()
_pid = None
_pools = {}
_gates = {}


def _reset_after_fork():
    global _pid, _pools, _gates
    if _pid != os.getpid():
        _pid, _pools, _gates = os.getpid(), {}, {}


def shared_pool(name):
    defaults = {"retrieval": (4, 64), "preparation": (4, 32), "pipeline": (12, 12)}
    with _resource_lock:
        _reset_after_fork()
        if name not in _pools:
            workers, pending = defaults[name]
            prefix = "RAG_" + name.upper()
            _pools[name] = BoundedPool(
                env_int(prefix + "_WORKERS", workers),
                env_int(prefix + "_PENDING", pending, 0), name,
            )
        return _pools[name]


@contextmanager
def model_slot(kind="stream", queue_timeout=None):
    with _resource_lock:
        _reset_after_fork()
        if kind not in _gates:
            default = 12 if kind == "stream" else 2
            _gates[kind] = threading.BoundedSemaphore(
                env_int("LLM_" + kind.upper() + "_CONCURRENCY", default)
            )
        gate = _gates[kind]
    timeout = env_float("LLM_QUEUE_TIMEOUT", 10) if queue_timeout is None else queue_timeout
    if not gate.acquire(timeout=timeout):
        raise CapacityError("The assistant is busy. Please try again shortly.")
    try:
        yield
    finally:
        gate.release()


class TTLCache:
    """Bounded, single-flight cache. Returned values never share mutable metadata."""

    def __init__(self, entries, ttl, wait=40):
        self.entries, self.ttl, self.wait = entries, ttl, wait
        self._lock = threading.Lock()
        self._values = OrderedDict()
        self._running = {}

    def get_or_compute(self, key, compute):
        if self.entries <= 0 or self.ttl <= 0:
            return compute()
        with self._lock:
            now = time.monotonic()
            for stale in [k for k, (expiry, _) in self._values.items() if expiry <= now]:
                del self._values[stale]
            if key in self._values:
                self._values.move_to_end(key)
                return copy.deepcopy(self._values[key][1])
            future = self._running.get(key)
            owner = future is None
            if owner:
                future = self._running[key] = Future()
        if not owner:
            return copy.deepcopy(future.result(timeout=self.wait))
        try:
            value = compute()
            cached = copy.deepcopy(value)
            with self._lock:
                self._values[key] = (time.monotonic() + self.ttl, cached)
                while len(self._values) > self.entries:
                    self._values.popitem(last=False)
                self._running.pop(key, None)
            future.set_result(cached)
            return value
        except BaseException as exc:
            with self._lock:
                self._running.pop(key, None)
            future.set_exception(exc)
            raise

    def clear(self):
        with self._lock:
            self._values.clear()


retrieval_cache = TTLCache(
    env_int("RETRIEVAL_CACHE_ENTRIES", 64, 0),
    env_float("RETRIEVAL_CACHE_TTL", 30),
)
query_rewrite_cache = TTLCache(env_int("QUERY_REWRITE_CACHE_ENTRIES", 64, 0),
                              env_float("QUERY_REWRITE_CACHE_TTL", 30))
