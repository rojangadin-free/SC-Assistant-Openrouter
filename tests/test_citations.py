"""
test_citations.py — offline checks for rag/citations.py.

No Pinecone, no LLM, no Flask: the module is pure data shaping, so it is tested
as such. Documents are faked with a tiny stand-in that mimics the only thing
build_citations() reads — `.metadata`.

Run:  python tests/test_citations.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

from rag.citations import build_citations, citation_labels, _collapse, _clean_source


class Doc:
    def __init__(self, source, page=None):
        self.metadata = {"source": source, "page": page}
        self.page_content = "..."


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
    return ok


def main():
    results = []

    print("\n[1] Storage paths are reduced to a filename")
    results.append(check(
        "s3 key -> basename",
        _clean_source("s3://sc-bucket/uploads/Samar-College-update.pdf"),
        "Samar-College-update.pdf",
    ))
    results.append(check(
        "windows path -> basename",
        _clean_source(r"data\Samar-College-2024.pdf"),
        "Samar-College-2024.pdf",
    ))

    print("\n[2] Consecutive pages collapse into ranges")
    results.append(check("run + single", _collapse([14, 15, 16, 20]), "14-16, 20"))
    results.append(check("single page", _collapse([7]), "7"))
    results.append(check("no pages", _collapse([]), ""))

    print("\n[3] Twelve chunks become a short, grouped source list")
    docs = [
        Doc("Samar-College-update.pdf", 14),
        Doc("Samar-College-update.pdf", 15),
        Doc("Samar-College-update.pdf", 14),   # duplicate page
        Doc("Samar-College-2024.pdf", 3),
    ]
    cites = build_citations(docs)
    results.append(check("one entry per file", len(cites), 2))
    results.append(check(
        "pages merged + collapsed",
        cites[0]["label"],
        "Samar-College-update.pdf, pp. 14-15",
    ))
    results.append(check("chunk count kept", cites[0]["chunks"], 3))
    results.append(check(
        "single page uses 'p.'",
        cites[1]["label"],
        "Samar-College-2024.pdf, p. 3",
    ))

    print("\n[4] Reranker order is preserved (most relevant file first)")
    ordered = build_citations([Doc("B.pdf", 2), Doc("A.pdf", 1)])
    results.append(check("first doc's file leads", ordered[0]["source"], "B.pdf"))

    print("\n[5] The list is capped so it stays a pointer, not a bibliography")
    many = [Doc(f"file{i}.pdf", i) for i in range(1, 9)]
    results.append(check("capped at 4 files", len(build_citations(many)), 4))
    results.append(check("cap is overridable", len(build_citations(many, max_sources=2)), 2))

    print("\n[6] Degenerate metadata does not raise")
    results.append(check("no docs", build_citations([]), []))
    results.append(check("None", build_citations(None), []))
    results.append(check("missing source is skipped", build_citations([Doc("", 1)]), []))
    no_page = build_citations([Doc("handbook.pdf", None)])
    results.append(check("missing page -> filename only", no_page[0]["label"], "handbook.pdf"))
    str_page = build_citations([Doc("handbook.pdf", "12.0")])
    results.append(check("float-ish page string", str_page[0]["pages"], [12]))

    print("\n[7] Flat labels for the feedback payload")
    results.append(check(
        "labels only",
        citation_labels([Doc("x.pdf", 1), Doc("x.pdf", 2)]),
        ["x.pdf, pp. 1-2"],
    ))

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
