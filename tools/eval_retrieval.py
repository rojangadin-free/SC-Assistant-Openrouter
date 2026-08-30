"""
eval_retrieval.py — regression test for retrieval STABILITY.

The bug this file guards against: the same user question answered correctly
about half the time. The cause was not randomness in the model but variance in
the *search query* — an LLM rewrites the question differently on each turn, and
a cross-encoder scores a keyword bag very differently from a real question.

So the test is: for each case, run EVERY plausible phrasing of the question
(the natural question plus realistic keyword rewrites) and require the gold
evidence to reach the top-K in ALL of them. A case only passes if it is
phrasing-independent.

Usage
-----
  python tools/eval_retrieval.py            # run all cases
  python tools/eval_retrieval.py -k 12      # change the top-K requirement
  python tools/eval_retrieval.py -v         # print full rankings
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Each case: the user's question, the keyword rewrites an LLM optimizer might
# produce for it, and text markers that must appear in the top-K results.
CASES = [
    {
        "name": "principals + CITAS dean",
        "question": "who are the principal of highschool, elementary and who is the dean of citas?",
        "rewrites": [
            "Samar College high school principal elementary principal Citas dean",
            "Samar College principal junior high senior high elementary CITAS dean names",
            "principal elementary dean CITAS",
        ],
        "must_include": [
            "PRINCIPAL, JUNIOR HIGH SCHOOL",
            "PRINCIPAL, ELEMENTARY",
            "CITAS",
        ],
    },
    {
        # Reported failure: the splitter mangled the 2nd and 3rd asks into
        # "who is the president of samar the dean of citas", so all three
        # aspects ranked the same HISTORICAL ACCOUNT pages and only the
        # president was answered. Guards the determiner-led fragment case.
        "name": "president + CITAS dean + HS principal",
        "question": "who is the president of samar college and the dean of citas and the principal of highschool",
        "rewrites": [
            "Samar College president, Dean of CITAS, and High School principal",
            "samar college president CITAS dean high school principal names",
        ],
        "must_include": [
            "PRESIDENT",
            "CITAS",
            "PRINCIPAL, JUNIOR HIGH SCHOOL",
        ],
    },
    {
        "name": "programs offered",

        "question": "what are the programs offered?",
        "rewrites": [
            "Samar College course offering list of programs",
            "programs offered Samar College complete list",
        ],
        "must_include": ["COURSE OFFERING"],
    },
    {
        "name": "college deans",
        "question": "who are the deans of each college?",
        "rewrites": [
            "Samar College deans list college dean names",
            "dean college of business management criminal justice education",
        ],
        "must_include": ["Dean"],
    },
]


def check(docs, markers):
    """Return {marker: rank or None} for the given ranking."""
    found = {}
    for m in markers:
        found[m] = next(
            (i + 1 for i, d in enumerate(docs) if m.lower() in d.page_content.lower()),
            None,
        )
    return found


def main():
    ap = argparse.ArgumentParser(description="Retrieval stability regression test.")
    ap.add_argument("-k", "--top-k", type=int, default=12)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    from rag.chain import retrieve_documents

    total = 0
    passed = 0

    for case in CASES:
        print("\n" + "=" * 78)
        print(f"CASE: {case['name']}")
        print(f"  question: {case['question']}")
        print("=" * 78)

        # The natural question alone, plus each keyword rewrite paired with the
        # natural question (which is what the app now always does).
        variants = [("question only", case["question"], None)]
        variants += [
            (f"rewrite: {r[:52]}", r, case["question"]) for r in case["rewrites"]
        ]

        for label, query, user_question in variants:
            docs = retrieve_documents(
                query,
                top_k=args.top_k,
                verbose=args.verbose,
                user_question=user_question,
            )
            found = check(docs, case["must_include"])
            ok = all(v is not None for v in found.values())

            total += 1
            passed += 1 if ok else 0

            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {label}")
            for marker, rank in found.items():
                print(f"          {marker!r:45} -> rank {rank}")

    print("\n" + "=" * 78)
    print(f"RESULT: {passed}/{total} variants passed (top-{args.top_k})")
    print("=" * 78)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
