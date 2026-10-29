"""Bounded per-worker execution and request budgets; no cloud dependencies."""

import concurrent.futures
import os
import threading
import time


def env_int(name, default, minimum=1, maximum=10000):
    try:
        return max(minimum, min(maximum, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


class CapacityError(RuntimeError):
    """Work was not admitted; callers must not mistake this for no evidence."""


class RequestBudget:
    def __init__(self, seconds=None):
        seconds = seconds if seconds is not None else env_int("CHAT_TIMEOUT_SECONDS", 90)
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()

    def remaining(self, maximum=None):
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("The answer request ended or exceeded its time budget.")
        return remaining if maximum is None else min(remaining, maximum)


class BoundedExecutor:
    """Bound running AND queued jobs. A timed-out job retains its slot until done."""

    def __init__(self, workers, pending, name):
        self.workers = workers
        self.name = name
        self._slots = threading.BoundedSemaphore(workers + pending)
        self._lock = threading.Lock()
        self._pool = None
        self._stopped = False

    def submit(self, fn, *args, admission_timeout=0, **kwargs):
        if not self._slots.acquire(timeout=max(0, admission_timeout)):
            raise CapacityError(f"{self.name} is busy; please retry shortly.")
        result = concurrent.futures.Future()

        def run():
            if result.set_running_or_notify_cancel():
                return fn(*args, **kwargs)
            return None

        def finished(inner):
            # Cancelling the public future does NOT remove the executor's
            # queued work item. Hold capacity until that item is really consumed.
            self._slots.release()
            if inner.cancelled():
                result.cancel()
            elif not result.cancelled():
                error = inner.exception()
                if error is not None:
                    result.set_exception(error)
                else:
                    result.set_result(inner.result())

        try:
            with self._lock:
                if self._stopped:
                    raise RuntimeError("Executor has been shut down.")
                if self._pool is None:
                    self._pool = concurrent.futures.ThreadPoolExecutor(
                        max_workers=self.workers, thread_name_prefix=self.name,
                    )
                future = self._pool.submit(run)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(finished)
        return result

    def map_ordered(self, fn, values, *, timeout=30, in_flight=2):
        """Share a worker pool fairly without submitting every probe at once."""
        values = list(values)
        results = [None] * len(values)
        remaining = iter(enumerate(values))
        pending = {}
        deadline = time.monotonic() + timeout

        def schedule():
            item = next(remaining, None)
            if item is not None:
                index, value = item
                pending[self.submit(fn, value)] = index

        try:
            for _ in range(min(in_flight, len(values))):
                schedule()
            while pending:
                done, _ = concurrent.futures.wait(
                    pending, timeout=max(0, deadline - time.monotonic()),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError(f"{self.name} exceeded its time budget.")
                for future in done:
                    results[pending.pop(future)] = future.result()
                    schedule()
            return results
        finally:
            for future in pending:
                future.cancel()

    def shutdown(self):
        with self._lock:
            self._stopped = True
            pool = self._pool
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


# These limits are PER PROCESS. With three workers: 12 retrieval jobs and three
# embedding calls (the embedding wrapper serializes its model), not 30 x 8 jobs.
retrieval_pool = BoundedExecutor(env_int("RETRIEVAL_WORKERS", 4), 24, "retrieval")
preparation_pool = BoundedExecutor(env_int("PREPARATION_WORKERS", 4), 24, "preparation")
optimizer_pool = BoundedExecutor(env_int("OPTIMIZER_WORKERS", 2), 0, "optimizer")
answer_pool = BoundedExecutor(env_int("ANSWER_WORKERS", 12), 0, "answer")
