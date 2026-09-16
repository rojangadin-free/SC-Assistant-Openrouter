"""
tests/test_rerank_switch.py — the setting, the router, and the fallback.

What is actually worth asserting here is not "does it rank" (the two backends
each own that) but the three things a runtime switch can get wrong and that no
one would notice in production:

  1. the stored value survives and is normalized,
  2. the router dispatches to the backend that was CHOSEN, and
  3. when the chosen backend cannot rank, the other one is used and the state
     reports the difference honestly.

(3) is the one that matters. `api` selected with no OPENROUTER_API_KEY and
`local` selected with no weights both produce the same symptom — answers in
retrieval order — and both are invisible unless something says so.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._bootstrap  # noqa: F401


class _Doc:
    """Minimal stand-in for a LangChain Document."""

    def __init__(self, text, meta=None):
        self.page_content = text
        self.metadata = dict(meta or {})


class _FakeBackend:
    """
    A reranker that records that it ran.

    `works=False` models a backend that is present but cannot rank — exactly
    what both real ones do when unconfigured: they return the input untouched
    with no `rerank_score`, which is the only signal the router can use.
    """

    def __init__(self, name, works=True):
        self.name = name
        self.works = works
        self.calls = 0

    def available(self):
        return self.works

    def disabled(self):
        return False

    def unavailable_reason(self):
        return "" if self.works else f"{self.name} is not configured"

    def rerank(self, query, docs, top_k=None, max_pairs=None, queries=None):
        self.calls += 1
        out = list(docs)
        if self.works:
            for i, d in enumerate(out):
                d.metadata["rerank_score"] = float(len(out) - i)
                d.metadata["ranked_by"] = self.name
        return out[:top_k] if top_k else out

    def rerank_multi(self, aspects, docs, top_k=None, max_pairs=None):
        return self.rerank(" ".join(aspects), docs, top_k=top_k)


class RerankSettingsTests(unittest.TestCase):
    """The stored value."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.environ["RERANK_SETTINGS_FILE"] = self._tmp.name

        from rag import rerank_settings

        self.settings = rerank_settings
        self.settings.reset_cache()

    def tearDown(self):
        self.settings.reset_cache()
        os.environ.pop("RERANK_SETTINGS_FILE", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_defaults_to_local(self):
        """A deployment that upgrades must behave exactly as it did before."""
        self.assertEqual(self.settings.current(), self.settings.DEFAULT_BACKEND)

    def test_set_and_read_back(self):
        self.settings.set_backend("api", changed_by="admin@sc.edu")
        self.assertEqual(self.settings.current(refresh=True), "api")

        info = self.settings.describe()
        self.assertEqual(info["backend"], "api")
        self.assertEqual(info["updated_by"], "admin@sc.edu")

    def test_whitespace_and_case_are_normalized(self):
        """An admin who sends "API " must see "api" stored, not a failure."""
        self.assertEqual(self.settings.set_backend("  API "), "api")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            self.settings.set_backend("cohere")

    def test_corrupt_stored_value_degrades_to_default(self):
        """
        A hand-edited store must cost ranking quality at worst, never an answer:
        `current()` is on the path that answers a student's question.
        """
        self.settings.set_backend("api")
        store = self.settings._store()
        store.mutate(lambda s: s.__setitem__("backend", "nonsense"))
        self.settings.reset_cache()
        self.assertEqual(self.settings.current(refresh=True),
                         self.settings.DEFAULT_BACKEND)


class RerankRouterTests(unittest.TestCase):
    """Dispatch and fallback."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.environ["RERANK_SETTINGS_FILE"] = self._tmp.name

        from rag import rerank_settings, rerank_router

        self.settings = rerank_settings
        self.router = rerank_router
        self.settings.reset_cache()

        self.local = _FakeBackend("local")
        self.api = _FakeBackend("api")

        self._real_backends = rerank_router._backends
        rerank_router._backends = lambda: {"local": self.local, "api": self.api}

    def tearDown(self):
        self.router._backends = self._real_backends
        self.settings.reset_cache()
        os.environ.pop("RERANK_SETTINGS_FILE", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _docs(self):
        return [_Doc("a"), _Doc("b"), _Doc("c")]

    def test_dispatches_to_the_chosen_backend(self):
        self.settings.set_backend("api")
        out = self.router.rerank("q", self._docs())
        self.assertEqual(self.api.calls, 1)
        self.assertEqual(self.local.calls, 0)
        self.assertEqual(out[0].metadata["ranked_by"], "api")
        self.assertEqual(self.router.last_used(), "api")

    def test_switching_takes_effect_without_reimport(self):
        """
        The whole point of the router. `chain.py` binds these functions at
        import time, so if dispatch were decided at import the switch would
        change the stored value and nothing else.
        """
        self.settings.set_backend("local")
        self.router.rerank("q", self._docs())
        self.assertEqual(self.local.calls, 1)

        self.settings.set_backend("api")
        self.router.rerank("q", self._docs())
        self.assertEqual(self.api.calls, 1)

    def test_falls_back_when_the_choice_cannot_rank(self):
        """`api` chosen with no key must not cost students their ordering."""
        self.api.works = False
        self.settings.set_backend("api")

        out = self.router.rerank("q", self._docs())
        self.assertEqual(self.local.calls, 1)
        self.assertEqual(out[0].metadata["ranked_by"], "local")
        self.assertEqual(self.router.last_used(), "local")

    def test_neither_backend_leaves_retrieval_order_intact(self):
        self.local.works = False
        self.api.works = False
        docs = self._docs()

        out = self.router.rerank("q", docs)
        self.assertEqual([d.page_content for d in out], ["a", "b", "c"])
        self.assertNotIn("rerank_score", out[0].metadata)
        self.assertEqual(self.router.last_used(), "")

    def test_top_k_is_honoured_even_with_no_backend(self):
        self.local.works = False
        self.api.works = False
        out = self.router.rerank("q", self._docs(), top_k=2)
        self.assertEqual(len(out), 2)

    def test_a_raising_backend_does_not_break_the_answer(self):
        """Both real backends swallow their own errors, so this is unexpected —
        which is exactly why it must not reach the student."""
        def boom(*a, **k):
            raise RuntimeError("socket closed")

        self.api.rerank = boom
        self.settings.set_backend("api")

        out = self.router.rerank("q", self._docs())
        self.assertEqual(self.local.calls, 1)
        self.assertEqual(out[0].metadata["ranked_by"], "local")

    def test_describe_reports_the_fallback(self):
        """
        The state an admin screen must be able to render: chosen ≠ effective.
        Reporting only the setting is what makes a silent fallback silent.
        """
        self.api.works = False
        self.settings.set_backend("api")

        state = self.router.describe()
        self.assertEqual(state["backend"], "api")
        self.assertEqual(state["active"], "local")
        self.assertTrue(state["falling_back"])

    def test_describe_is_not_falling_back_when_honoured(self):
        self.settings.set_backend("local")
        state = self.router.describe()
        self.assertEqual(state["active"], "local")
        self.assertFalse(state["falling_back"])

    def test_rerank_multi_dispatches_too(self):
        self.settings.set_backend("api")
        out = self.router.rerank_multi(["fees", "deadline"], self._docs())
        self.assertEqual(self.api.calls, 1)
        self.assertEqual(out[0].metadata["ranked_by"], "api")

    def test_empty_input_is_a_no_op(self):
        self.assertEqual(self.router.rerank("q", []), [])
        self.assertEqual(self.router.rerank_multi(["q"], []), [])
        self.assertEqual(self.local.calls + self.api.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
