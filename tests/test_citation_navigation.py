"""Offline tests of the real inline citation renderer, without loading the app.

Run: python -B -X utf8 tests/test_citation_navigation.py -v
Requires Node.js on PATH; no npm packages, browser, config, or cloud services.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
URI_SAFE = "~!*'()"  # encodeURIComponent's additional unescaped characters.
NODE_RUNNER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const stub = `
const marked = {
  parse(text) {
    return String(text).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[ch]);
  }
};
renderCitations(rawText);
`;
const output = vm.runInNewContext(input.functions + '\n' + stub, {
  rawText: input.text, URLSearchParams
}, {
  filename: 'chat-citation-functions.js',
  timeout: 1000,
  contextCodeGeneration: { strings: false, wasm: false }
});
process.stdout.write(JSON.stringify(output));
"""


def extract_function(source, name):
    """Select a declaration by its known two-space closing boundary in chat.js."""
    match = re.search(
        rf"^  function {re.escape(name)}\([^\n]*\) \{{\n.*?^  \}}[ \t]*$",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Cannot isolate {name} from chat.js")
    return match.group()


class CitationHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.links = []
        self.elements = []
        self.current_link = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, attrs))
        if tag == "a":
            self.current_link = {"attrs": dict(attrs), "text": ""}
            self.links.append(self.current_link)

    def handle_endtag(self, tag):
        if tag == "a":
            self.current_link = None

    def handle_data(self, data):
        if self.current_link is not None:
            self.current_link["text"] += data


class CitationNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required for citation renderer tests")
        source = (ROOT / "sc_assistant/static/js/chat.js").read_text(encoding="utf-8-sig")
        cls.functions = "\n".join(
            extract_function(source, name) for name in ("escapeHtml", "renderCitations")
        )

    def render(self, text):
        result = subprocess.run(
            [self.node, "-e", NODE_RUNNER],
            input=json.dumps({"functions": self.functions, "text": text}),
            capture_output=True, encoding="utf-8", timeout=10, cwd=ROOT, check=False,
            env={**os.environ, "NODE_OPTIONS": "", "NODE_PATH": ""},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html = json.loads(result.stdout)
        self.assertNotIn("CITATIONPLACEHOLDER", html)
        dom = CitationHTML(html)
        for tag, attrs in dom.elements:
            self.assertEqual(len(attrs), len(dict(attrs)), f"Duplicate attributes on {tag}")
        return dom

    def assert_link(self, link, filename, fragment, number=1):
        attrs = link["attrs"]
        expected = "/chat/document/" + quote(filename, safe=URI_SAFE)
        if fragment:
            expected += "#" + fragment
        self.assertEqual(attrs["href"], expected)
        self.assertEqual(urlsplit(attrs["href"]).query, "", "Excerpts must never reach the server")
        self.assertEqual(attrs["data-source"], filename)
        self.assertIn(filename, attrs["title"])
        self.assertEqual(attrs["target"], "_blank")
        self.assertEqual(set(attrs.get("rel", "").split()), {"noopener", "noreferrer"})
        self.assertEqual(link["text"], f"[{number}]")

    def assert_docx(self, link, filename, excerpt=None, number=1):
        fragment = "citation=unavailable"
        if excerpt is not None:
            fragment = urlsplit(link["attrs"]["href"]).fragment
            self.assertEqual(parse_qs(fragment, strict_parsing=True), {"quote": [excerpt]})
            # Both percent-encoded and form-encoded spaces are valid fragment data.
            self.assertRegex(fragment, r"\Aquote=(?:[A-Za-z0-9_.!~*'()+-]|%[0-9a-fA-F]{2})+\Z")
        self.assert_link(link, filename, fragment, number)
        self.assertNotRegex(link["attrs"]["title"], r"(?i)\bpages?\s+\d|\bp\.\s*\d")

    def test_docx_suffix_and_extension_are_case_insensitive_and_trimmed(self):
        excerpt = "Students must submit the completed form to the registrar."
        for extension, prefix in (("docx", "quote"), ("DOCX", "QUOTE"), ("DoCx", "QuOtE")):
            with self.subTest(extension=extension, prefix=prefix):
                filename = "Academic Rules." + extension
                dom = self.render(f"[sOuRcE:  {filename}  | {prefix}: \t{excerpt}\n ]")
                self.assertEqual(len(dom.links), 1)
                self.assert_docx(dom.links[0], filename, excerpt)

    def test_numbers_in_docx_quotes_do_not_become_multiple_page_badges(self):
        excerpt = "Stage2 requires 12 credits in AY2026 and a GPA of 2.5."
        dom = self.render(f"[SOURCE:Rules2026.docx|quote:{excerpt}]")
        self.assertEqual(len(dom.links), 1)
        self.assert_docx(dom.links[0], "Rules2026.docx", excerpt)

    def test_same_file_passages_share_a_number_but_have_exact_distinct_targets(self):
        first = "Submit the completed application to the registrar before enrollment."
        second = "Bring the original receipt to the cashier after payment."
        dom = self.render(
            f"[SOURCE:Rules.docx|quote:{first}] [SOURCE:Other.pdf|p.4] "
            f"[SOURCE:Rules.docx|quote:{second}] [SOURCE:Rules.docx|quote:{first}]"
        )
        self.assertEqual(len(dom.links), 4)
        self.assert_docx(dom.links[0], "Rules.docx", first)
        self.assertEqual(dom.links[1]["text"], "[2]")
        self.assert_docx(dom.links[2], "Rules.docx", second)
        self.assert_docx(dom.links[3], "Rules.docx", first)
        self.assertNotEqual(dom.links[0]["attrs"]["href"], dom.links[2]["attrs"]["href"])

    def test_special_characters_and_unicode_round_trip_in_fragment_only(self):
        filename = "Registrar's C++ & <Guide> #50?=v2% \u00f1.DoCx"
        excerpt = 'Costs (A+B)? use .* ^$ {2} \\ and C++ & 50% #?= / "caf\u00e9" \u4e2d\u6587'
        dom = self.render(f"[SOURCE:{filename}|quote:{excerpt}]")
        self.assertEqual(len(dom.links), 1)
        self.assert_docx(dom.links[0], filename, excerpt)
        target = urlsplit(dom.links[0]["attrs"]["href"])
        self.assertEqual(parse_qs(target.fragment), {"quote": [excerpt]})
        self.assertIn("&lt;Guide&gt;", dom.html)

    def test_legacy_docx_citations_fall_back_without_fake_pages(self):
        for suffix in ("", "|p.1", "|p.12.0", "|pp.2, 8-10", "|12, 14", "|section 7"):
            with self.subTest(suffix=suffix):
                dom = self.render(f"[SOURCE:Rules.docx{suffix}]")
                self.assertEqual(len(dom.links), 1)
                self.assert_docx(dom.links[0], "Rules.docx")

    def test_malformed_docx_locators_fall_back_to_the_original_file(self):
        for suffix in ("|", "|   ", "|quote", "|quote=an exact excerpt", "|quotes:an exact excerpt"):
            with self.subTest(suffix=suffix):
                dom = self.render(f"[SOURCE:Rules.docx{suffix}]")
                self.assertEqual(len(dom.links), 1)
                self.assert_docx(dom.links[0], "Rules.docx")

    def test_docx_quote_length_boundaries_are_inclusive_after_trimming(self):
        for size in (8, 300):
            with self.subTest(size=size):
                excerpt = "x" * size
                dom = self.render(f"[SOURCE:Rules.docx|quote: \t{excerpt} \n]")
                self.assertEqual(len(dom.links), 1)
                self.assert_docx(dom.links[0], "Rules.docx", excerpt)

    def test_invalid_docx_quote_sizes_are_not_truncated_or_linked(self):
        for excerpt in ("", " \t ", "1234567", " 1234567 ", "x" * 301):
            with self.subTest(size=len(excerpt), excerpt=excerpt[:15]):
                dom = self.render(f"[SOURCE:Rules.docx|quote:{excerpt}]")
                self.assertEqual(len(dom.links), 1)
                self.assert_docx(dom.links[0], "Rules.docx")

    def test_pdf_single_page_retains_preceding_word_search(self):
        cases = (
            ("", "12", None),
            ("See it on 2026. ", "12", None),
            ("Scholarships are available. ", "12.0", "Scholarships"),
            ("EncyclopedicallyLong a b c d e f g registrar ", "12", "registrar"),
            ("Visit pre-enrollment, 12345678901234567890. ", "12", "pre-enrollment"),
        )
        for preceding, page, search in cases:
            with self.subTest(preceding=preceding, page=page):
                dom = self.render(f"{preceding}[SOURCE:Handbook.PDF|p.{page}]")
                self.assertEqual(len(dom.links), 1)
                fragment = "page=12" + (f"&search={search}" if search else "")
                self.assert_link(dom.links[0], "Handbook.PDF", fragment)
                self.assertIn("page 12", dom.links[0]["attrs"]["title"])
                if search:
                    self.assertIn(f"#page=12&amp;search={search}", dom.html)

    def test_pdf_multi_page_badges_preserve_numbers_and_range_endpoints(self):
        dom = self.render("Registrar details [SOURCE:Handbook.pdf|pp.2, 4-6, 8.0]")
        self.assertEqual(len(dom.links), 4)
        for link, page in zip(dom.links, (2, 4, 6, 8)):
            self.assert_link(link, "Handbook.pdf", f"page={page}&search=Registrar")
            self.assertIn(f"page {page}", link["attrs"]["title"])

    def test_pdf_without_a_page_remains_a_plain_document_link(self):
        for suffix in ("", "|not specified"):
            with self.subTest(suffix=suffix):
                dom = self.render(f"[SOURCE:Handbook.pdf{suffix}]")
                self.assertEqual(len(dom.links), 1)
                self.assert_link(dom.links[0], "Handbook.pdf", "")

    def test_malicious_filenames_and_excerpts_cannot_create_attributes_or_tags(self):
        excerpt = 'Use " x=" autofocus onfocus="alert(1)" <svg/onload=boom> safely.'
        for extension, suffix in (("docx", "quote:" + excerpt), ("pdf", "p.6")):
            with self.subTest(extension=extension):
                filename = (
                    'Report" data-injected="yes" onmouseover="alert(1)" '
                    '<img src=x onerror=boom> &copy; & "Q".' + extension
                )
                baseline = self.render(f"[SOURCE:Safe.{extension}|{suffix}]")
                dom = self.render(f"[SOURCE:{filename}|{suffix}]")
                self.assertEqual(len(dom.links), 1)
                if extension == "docx":
                    self.assert_docx(dom.links[0], filename, excerpt)
                else:
                    self.assert_link(dom.links[0], filename, "page=6")
                self.assertEqual(
                    [(tag, set(dict(attrs))) for tag, attrs in dom.elements],
                    [(tag, set(dict(attrs))) for tag, attrs in baseline.elements],
                )
                for _, attrs in dom.elements:
                    self.assertFalse(any(name.startswith("on") for name, _ in attrs))
                self.assertIn("&quot;", dom.html)
                self.assertIn("&lt;img", dom.html)
                self.assertIn("&amp;", dom.html)


if __name__ == "__main__":
    unittest.main()
