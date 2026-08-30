"""
probe_candidates.py — inspect the RECALL stage only.

`probe_retrieval.py` shows the final ranking. When an answer is missing, the
first question is always: was the right chunk even in the candidate pool?
This prints the raw hybrid-retrieval candidates (before reranking) plus a
keyword check, so recall failures and ranking failures can be told apart.

Usage
-----
  python tools/probe_candidates.py "Samar College high school principal elementary principal Citas dean" --grep CITAS
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="Probe hybrid retrieval recall.")
    ap.add_argument("query")
    ap.add_argument("--grep", action="append", default=[],
                    help="Keyword to look for in the pool (repeatable)")
    args = ap.parse_args()

    from rag.chain import multi_query_retrieve, expand_queries

    print(f"Expanded queries: {expand_queries(args.query)}")
    docs = multi_query_retrieve(args.query)
    print(f"\nCandidate pool: {len(docs)} docs\n")

    for i, d in enumerate(docs):
        md = d.metadata or {}
        snippet = d.page_content.replace("\n", " ")[:90]
        print(f"  [{i+1:>2}] {md.get('source')} p{md.get('page')} "
              f"| {md.get('section')} | {snippet}")

    for kw in args.grep:
        hits = [
            (i + 1, (d.metadata or {}).get("source"), (d.metadata or {}).get("page"))
            for i, d in enumerate(docs)
            if kw.lower() in d.page_content.lower()
        ]
        print(f"\n'{kw}' found in {len(hits)} candidate(s): {hits}")


if __name__ == "__main__":
    main()
