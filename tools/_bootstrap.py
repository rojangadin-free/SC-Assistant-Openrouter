"""
tools/_bootstrap.py — make the repo root importable, and the CWD predictable.

Why this file has to exist
-------------------------
Every script in this folder was written to be run from the repo root, where
`import config` and `from rag.chain import ...` just work because Python puts the
script's own directory on `sys.path[0]`. Moving them into `tools/` changed that
directory to `tools/`, so those imports now fail with ModuleNotFoundError — the
scripts are not wrong, their idea of "here" moved.

Two separate problems, both fixed here:

1. **Imports.** The repo root is prepended to `sys.path`, so `config`, `rag`,
   `src`, `aws` and `store_index` resolve exactly as before.

2. **Relative file paths.** This is the subtler one and it would have produced
   wrong output rather than a clean crash. `rag/chain.py` opens
   `bm25_values.json` by bare filename, and several scripts read `data/*.pdf` the
   same way. Run from `tools/`, those paths resolve to `tools/bm25_values.json`
   and `tools/data/` — which do not exist. The BM25 case is the dangerous one:
   `chain.py` catches the miss and falls back to `BM25Encoder().default()` with
   only a printed warning, so retrieval would silently run with untrained sparse
   weights and every probe would report subtly wrong rankings. A tool used to
   diagnose retrieval quietly lying about retrieval is the worst outcome here, so
   the CWD is forced back to the repo root.

Import it first, before any repo import:

    import _bootstrap  # noqa: F401  (must precede the rag/config imports)
"""

import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)

# Prepend, not append: a stdlib or site-packages module sharing a name with one of
# ours (there is a real `config` package on PyPI) would otherwise win.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# See point 2 above. Unconditional on purpose — running from the repo root makes
# this a no-op, and anywhere else it is the difference between correct output and
# plausible-looking nonsense.
os.chdir(_REPO_ROOT)
