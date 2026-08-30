"""
tests/_bootstrap.py — make the repo root importable, and the CWD predictable.

Same job as tools/_bootstrap.py, and it exists separately so `tests/` and `tools/`
have no dependency on each other.

Why the chdir matters more here than the sys.path fix
-----------------------------------------------------
The import fix is the obvious half: these suites do `from rag.store import ...` and
`from sc_assistant.pwa import bp_pwa`, which only resolve with the repo root on
`sys.path`. Miss it and you get ModuleNotFoundError — loud, immediate, nobody is
fooled.

The `os.chdir` is the half that would have cost an afternoon. Several suites read
source files by relative path to assert on their contents:

    open("sc_assistant/static/js/chat.js")        # test_pwa.py
    open("sc_assistant/templates/base.html")      # test_pwa.py

Run from `tests/`, those become `tests/sc_assistant/...` and raise
FileNotFoundError — recoverable, but only after you have gone looking. Worse is
the class of check that reads a file and asserts a *substring is present*: if a
path silently resolved to something empty or wrong, the assertion would fail with
"registration is not inside an overridable block" and send you to edit a template
that was never broken. Forcing the CWD to the repo root keeps every relative path
meaning what it meant when it was written.

Import it first, before any repo import:

    import _bootstrap  # noqa: F401
"""

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

# Prepend rather than append: `config` is also a real package on PyPI, and if one
# is installed in the venv an append would silently load the wrong module.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# No-op when already run from the repo root (the normal case via run_tests.py);
# load-bearing when a suite is run directly as `python tests/test_pwa.py`.
os.chdir(_REPO_ROOT)
