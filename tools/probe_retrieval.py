"""
probe_retrieval.py — run the REAL retrieval + rerank pipeline for one or more
queries and print the ranked documents, without calling the chat LLM.

This is the offline reproduction harness for "sometimes it answers right,
sometimes it doesn't" bugs: the only thing that changes between two identical
user questions is the *search query text*, so you can compare them here.

Usage
-----
  python tools/probe_retrieval.py "who are the principal of highschool, elementary and who is the dean of citas?"
  python tools/probe_retrieval.py -k 12 "Samar College high school principal elementary principal Citas dean"
  python tools/probe_retrieval.py --full "who is the dean of citas?"

  # Simulate the app exactly: LLM keyword rewrite + the user's real question
  python tools/probe_retrieval.py -q "who is the dean of citas?" "Samar College CITAS dean"
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="Probe the live retrieval pipeline.")
    ap.add_argument("queries", nargs="+", help="One or more queries to run")
    ap.add_argument("-k", "--top-k", type=int, default=None, help="Docs to show")
    ap.add_argument("--full", action="store_true", help="Print full chunk text")
    ap.add_argument(
        "--chars", type=int, default=240, help="Snippet length when not --full"
    )
    ap.add_argument(
        "-q",
        "--question",
        default=None,
        help="The user's literal question (the app always passes this alongside "
             "the optimized query; ranking depends on it)",
    )
    args = ap.parse_args()


    # Imported here so --help works without network/model loading.
    from rag.chain import retrieve_documents, FINAL_TOP_K

    top_k = args.top_k or FINAL_TOP_K

    for q in args.queries:
        print("\n" + "#" * 78)
        print(f"# QUERY: {q}")
        print("#" * 78)

        docs = retrieve_documents(
            q, top_k=top_k, verbose=True, user_question=args.question
        )


        for i, d in enumerate(docs):
            md = d.metadata or {}
            print("-" * 78)
            print(
                f"[{i + 1}] {md.get('source')} p{md.get('page')} "
                f"| section={md.get('section')} "
                f"| rerank={md.get('rerank_score')}"
            )
            body = d.page_content
            print(body if args.full else body[: args.chars].replace("\n", " "))

        print(f"\n--> {len(docs)} document(s) returned for: {q}")


if __name__ == "__main__":
    main()
