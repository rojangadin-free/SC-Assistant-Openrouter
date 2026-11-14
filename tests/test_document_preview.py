"""Offline DOCX preview regressions; no application config, AWS, or indexing.

Run: python -B -X utf8 tests/test_document_preview.py -v
Requires Flask, python-docx, and lxml. All documents and storage are synthetic.
"""

import ast
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import docx
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from lxml import html

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def offline_only():
    forbidden = dict.fromkeys((
        "config", "dotenv", "sc_assistant", "aws", "rag", "src", "store_index",
        "boto3", "botocore", "pinecone", "pinecone_text", "langchain",
        "langchain_core", "langchain_community", "langchain_openai",
        "langchain_huggingface", "sentence_transformers", "openai", "transformers",
    ))
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, forbidden))
        network = [stack.enter_context(patch(
            target, side_effect=AssertionError("Preview tests must remain offline"),
        )) for target in (
            "socket.create_connection", "socket.socket.connect",
            "socket.socket.connect_ex", "socket.getaddrinfo",
        )]
        try:
            yield
        finally:
            for operation in network:
                operation.assert_not_called()


def extract_functions(relative_path, names, namespace):
    """Execute only the selected functions, keeping their real route decorators."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != set(names):
        raise AssertionError(f"Missing AST test interface in {relative_path}: {names}")
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102


def document_bytes(document):
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def archive_bytes(entries):
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return stream.getvalue()


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.resources.enter_context(offline_only())
        spec = importlib.util.spec_from_file_location(
            "_document_preview_under_test", ROOT / "sc_assistant/document_preview.py",
        )
        self.preview = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.preview)


class PreviewExtractionTests(OfflineTestCase):
    def test_paragraphs_headings_and_list_groups_keep_reading_order(self):
        document = docx.Document()
        document.add_heading("Handbook", level=0)
        document.add_paragraph("Before the lists.")
        document.add_paragraph(" \t ")
        document.add_heading("Admissions", level=1)
        for text in ("Bring a form", "Bring an ID"):
            document.add_paragraph(text, style="List Bullet")
        document.add_paragraph("Submit the form", style="List Number")
        document.add_paragraph("After the lists.")
        document.add_paragraph("A separate list", style="List Bullet")
        document.add_heading("Fine print", level=9)
        blocks = self.preview.build_docx_preview(document_bytes(document))
        self.assertEqual([block["kind"] for block in blocks], [
            "heading", "paragraph", "heading", "list", "list", "paragraph", "list", "heading",
        ])
        self.assertEqual(blocks[0], {"kind": "heading", "level": 2, "text": "Handbook"})
        self.assertEqual(blocks[1]["text"], "Before the lists.")
        self.assertEqual(blocks[2]["level"], 2)
        self.assertEqual(blocks[3]["items"], ["Bring a form", "Bring an ID"])
        self.assertFalse(blocks[3]["ordered"])
        self.assertEqual(blocks[4]["items"], ["Submit the form"])
        self.assertTrue(blocks[4]["ordered"])
        self.assertEqual(blocks[5]["text"], "After the lists.")
        self.assertEqual(blocks[6]["items"], ["A separate list"])
        self.assertEqual(blocks[7]["level"], 6)

    def test_nested_and_merged_tables_preserve_physical_cells_and_text(self):
        document = docx.Document()
        document.add_paragraph("Before table")
        table = document.add_table(rows=4, cols=3)
        table.cell(0, 0).merge(table.cell(0, 1)).text = "Merged header"
        table.cell(0, 2).text = "Third column"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "Vertical label"
        table.cell(1, 1).text = table.cell(1, 2).text = "Repeated value"
        table.cell(2, 1).text = "Independent cell"
        cell = table.cell(2, 2)
        cell.text = "Before nested table"
        nested = cell.add_table(rows=1, cols=2)
        nested.cell(0, 0).text = "Inner A"
        nested.cell(0, 1).text = "Inner B"
        cell.add_paragraph("After nested table")
        table.cell(3, 2).text = "Last row"
        document.add_paragraph("After table")
        blocks = self.preview.build_docx_preview(document_bytes(document))
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table", "paragraph"])
        self.assertEqual(blocks[0]["text"], "Before table")
        self.assertEqual(blocks[2]["text"], "After table")
        rows = blocks[1]["rows"]
        self.assertEqual([len(row) for row in rows], [2, 3, 3, 3])
        self.assertEqual([cell["colspan"] for cell in rows[0]], [2, 1])
        self.assertEqual(rows[2][0]["blocks"], [])
        inner = rows[2][2]["blocks"]
        self.assertEqual([block["kind"] for block in inner], ["paragraph", "table", "paragraph"])
        self.assertEqual(inner[0]["text"], "Before nested table")
        self.assertEqual(inner[2]["text"], "After nested table")
        self.assertEqual([cell["blocks"][0]["text"] for cell in inner[1]["rows"][0]],
                         ["Inner A", "Inner B"])
        self.assertEqual(rows[3][2]["blocks"][0]["text"], "Last row")
        serialized = json.dumps(blocks)
        self.assertEqual(serialized.count("Merged header"), 1)
        self.assertEqual(serialized.count("Vertical label"), 1)
        self.assertEqual(serialized.count("Repeated value"), 2)

    def test_external_hyperlink_is_plain_text_without_fetching_its_target(self):
        document = docx.Document()
        paragraph = document.add_paragraph("Visit ")
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), document.part.relate_to(
            "https://private.example.invalid/office", RELATIONSHIP_TYPE.HYPERLINK,
            is_external=True,
        ))
        run, text = OxmlElement("w:r"), OxmlElement("w:t")
        text.text = "the office"
        run.append(text)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        self.assertEqual(self.preview.build_docx_preview(document_bytes(document)), [
            {"kind": "paragraph", "text": "Visit the office"},
        ])

    def test_empty_document_has_no_preview_blocks(self):
        self.assertEqual(self.preview.build_docx_preview(document_bytes(docx.Document())), [])

    def test_corrupt_and_non_docx_data_raise_controlled_value_error(self):
        for data in (b"", b"not a DOCX", b"PK\x03\x04truncated",
                     archive_bytes({"unrelated.txt": b"not an Office package"})):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                self.preview.build_docx_preview(data)

    def test_archive_limits_reject_before_the_docx_parser(self):
        cases = (
            ("MAX_DOCX_BYTES", 16, b"x" * 17),
            ("MAX_EXPANDED_BYTES", 32, archive_bytes({"word/document.xml": b"x" * 33})),
            ("MAX_ARCHIVE_ENTRIES", 2, archive_bytes({str(i): b"" for i in range(3)})),
        )
        for name, limit, data in cases:
            with (self.subTest(limit=name), patch.object(self.preview, name, limit),
                  patch.object(self.preview, "Document") as parser):
                with self.assertRaises(OverflowError):
                    self.preview.build_docx_preview(data)
                parser.assert_not_called()

    def test_exact_limits_are_accepted_and_nested_text_and_cells_are_counted(self):
        document = docx.Document()
        document.add_paragraph("abc")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "def"
        data = document_bytes(document)
        with ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
        with patch.multiple(self.preview, MAX_DOCX_BYTES=len(data),
                            MAX_EXPANDED_BYTES=sum(entry.file_size for entry in entries),
                            MAX_ARCHIVE_ENTRIES=len(entries), MAX_PREVIEW_ITEMS=4,
                            MAX_PREVIEW_TEXT=6):
            self.assertEqual(len(self.preview.build_docx_preview(data)), 2)
        for name, limit in (("MAX_PREVIEW_ITEMS", 3), ("MAX_PREVIEW_TEXT", 5)):
            with (self.subTest(limit=name), patch.object(self.preview, name, limit),
                  self.assertRaises(OverflowError)):
                self.preview.build_docx_preview(data)

    def test_blank_paragraphs_still_consume_the_item_budget(self):
        document = docx.Document()
        for _ in range(3):
            document.add_paragraph("")
        with patch.object(self.preview, "MAX_PREVIEW_ITEMS", 2), self.assertRaises(OverflowError):
            self.preview.build_docx_preview(document_bytes(document))

    def test_deep_tables_and_untrusted_grid_spans_are_bounded(self):
        document = docx.Document()
        container = document
        for _ in range(10):
            container = container.add_table(rows=1, cols=1).cell(0, 0)
        with self.assertRaises(OverflowError):
            self.preview.build_docx_preview(document_bytes(document))
        for span in (0, 101, 1_000_000):
            with self.subTest(span=span):
                document = docx.Document()
                cell = document.add_table(rows=1, cols=1).cell(0, 0)
                cell._tc.get_or_add_tcPr().get_or_add_gridSpan().val = span
                with self.assertRaises(OverflowError):
                    self.preview.build_docx_preview(document_bytes(document))


class DocumentRouteTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.tmp / "data"
        self.data.mkdir()
        document = docx.Document()
        document.add_paragraph("Synthetic handbook text")
        self.original = document_bytes(document)
        self.read_s3 = Mock(return_value=self.original)
        self.signed_url = "https://storage.example.invalid/original?signature=synthetic"
        self.presign = Mock(return_value=self.signed_url)
        self.build = Mock(wraps=self.preview.build_docx_preview)
        self.app = Flask("document_preview_tests",
                         template_folder=str(ROOT / "sc_assistant/templates"),
                         static_folder=str(ROOT / "sc_assistant/static"))
        self.app.testing = True
        self.app.secret_key = "offline-preview-test-secret"
        self.app.add_url_rule("/login", "auth.auth_page", lambda: "Sign in")
        self.app.add_url_rule("/chat/", "chat.chat_page", lambda: "Chat")
        bp = Blueprint("chat", __name__, url_prefix="/chat")
        self.ns = {
            "__name__": "_document_routes_under_test",
            # Redirect only the route's data lookup, never the real repository data/.
            "__file__": str(self.tmp / "sc_assistant/chat.py"),
            "Path": Path, "bp": bp, "request": request, "session": session,
            "abort": abort, "redirect": redirect, "url_for": url_for,
            "current_app": current_app, "make_response": make_response,
            "render_template": render_template, "send_from_directory": send_from_directory,
            "jsonify": jsonify, "read_s3_file": self.read_s3,
            "get_s3_presigned_url": self.presign, "build_docx_preview": self.build,
            "MAX_DOCX_BYTES": self.preview.MAX_DOCX_BYTES,
        }
        extract_functions("sc_assistant/chat.py", ("serve_document",), self.ns)
        self.app.register_blueprint(bp)
        self.ns["bp"] = Blueprint("admin", __name__)
        extract_functions("sc_assistant/admin.py", ("get_view_url", "get_download_url"), self.ns)
        self.app.register_blueprint(self.ns["bp"])

    def client(self, user="guest"):
        client = self.app.test_client()
        if user is not None:
            with client.session_transaction() as state:
                state.update(user=user, is_guest=user == "guest", role="student")
        return client

    def document_url(self, filename):
        return "/chat/document/" + quote(filename, safe="")

    def assert_preview(self, response, status=200):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.mimetype, "text/html")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertTrue(response.cache_control.no_store)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        csp = response.headers["Content-Security-Policy"]
        for directive in ("default-src 'none'", "style-src 'self'", "script-src 'self'",
                          "base-uri 'none'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)
        self.assertNotIn("'unsafe-inline'", csp)
        return html.fromstring(response.get_data(as_text=True))

    def download_link(self, page, filename):
        links = page.xpath("//a[contains(@href, 'download=1')]/@href")
        self.assertTrue(links, "The preview must offer a usable original download")
        for link in links:
            url = urlsplit(link)
            self.assertFalse(url.scheme or url.netloc)
            self.assertEqual(unquote(url.path), "/chat/document/" + filename)
            self.assertEqual(parse_qs(url.query), {"download": ["1"]})
        return links[0]

    def test_local_and_s3_previews_accept_guest_accounts_and_original_names(self):
        for storage in ("local", "s3"):
            filename = f"{storage} O'Brien & Fees #1 + 100%.DoCx"
            if storage == "local":
                (self.data / filename).write_bytes(self.original)
            for user in ("guest", "reader@example.invalid"):
                with self.subTest(storage=storage, user=user):
                    self.read_s3.reset_mock()
                    self.build.reset_mock()
                    with self.client(user).get(self.document_url(filename)) as response:
                        page = self.assert_preview(response)
                    self.assertIn(filename, page.text_content())
                    self.assertIn("Synthetic handbook text", page.text_content())
                    self.download_link(page, filename)
                    self.build.assert_called_once_with(self.original)
                    if storage == "local":
                        self.read_s3.assert_not_called()
                    else:
                        self.read_s3.assert_called_once_with(filename, self.preview.MAX_DOCX_BYTES)
        self.presign.assert_not_called()

    def test_anonymous_preview_requires_a_session_before_reading_storage(self):
        (self.data / "local.DOCX").write_bytes(self.original)
        client = self.client(None)
        for filename in ("local.DOCX", "remote.docx"):
            for query in ({}, {"download": "0"}, {"download": "true"}):
                with (self.subTest(filename=filename, query=query),
                      client.get(self.document_url(filename), query_string=query) as response):
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(urlsplit(response.location).path, "/login")
        self.read_s3.assert_not_called()
        self.presign.assert_not_called()
        self.build.assert_not_called()

    def test_text_and_filename_are_escaped_and_resources_are_local(self):
        filename = 'Guide <img src=x onerror=alert(1)> & "quoted".DOCX'
        payload = '<script>alert("document")</script> & <img src=x onerror=alert(2)>'
        document = docx.Document()
        document.add_heading(payload, level=1)
        document.add_paragraph(payload)
        document.add_paragraph(payload, style="List Bullet")
        document.add_table(rows=1, cols=1).cell(0, 0).text = payload
        self.read_s3.return_value = document_bytes(document)
        with self.client().get(self.document_url(filename)) as response:
            page = self.assert_preview(response)
            source = response.get_data(as_text=True)
        self.assertIn(filename, page.text_content())
        self.assertEqual(page.text_content().count(payload), 4)
        self.assertNotIn(filename, source)
        self.assertNotIn(payload, source)
        self.assertEqual(page.xpath("//script[not(@src)] | //iframe | //object | //embed"), [])
        self.assertEqual(page.xpath("//@onerror | //@onload | //@onclick"), [])
        resources = page.xpath("//script/@src | //link[@rel='stylesheet']/@href | //img/@src")
        self.assertTrue(resources)
        for resource in resources:
            url = urlsplit(resource)
            self.assertFalse(url.scheme or url.netloc)
            self.assertTrue(url.path.startswith("/static/"))
        self.download_link(page, filename)

    def test_local_and_s3_corruption_offer_byte_preserving_downloads(self):
        corrupt = b"Synthetic broken DOCX, not a real document"
        for storage in ("local", "s3"):
            filename = storage + " corrupt.docx"
            if storage == "local":
                (self.data / filename).write_bytes(corrupt)
            else:
                self.read_s3.return_value = corrupt
            client = self.client()
            with self.subTest(storage=storage):
                with client.get(self.document_url(filename)) as response:
                    page = self.assert_preview(response, 422)
                self.assertIn("could not be previewed", page.text_content())
                link = self.download_link(page, filename)
                self.build.reset_mock()
                with client.get(link) as response:
                    if storage == "local":
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.data, corrupt)
                        self.assertIn("attachment", response.headers["Content-Disposition"])
                    else:
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response.location, self.signed_url)
                        self.presign.assert_called_with(filename, for_download=True)
                self.build.assert_not_called()

    def test_local_read_is_bounded_but_original_download_is_not(self):
        filename = "oversized.docx"
        (self.data / filename).write_bytes(self.original)
        client = self.client()
        with (patch.dict(self.ns, {"MAX_DOCX_BYTES": 16}),
              patch.object(self.preview, "MAX_DOCX_BYTES", 16)):
            with client.get(self.document_url(filename)) as response:
                page = self.assert_preview(response, 413)
            self.build.assert_called_once_with(self.original[:17])
            with client.get(self.download_link(page, filename)) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, self.original)
                self.assertIn("attachment", response.headers["Content-Disposition"])
        self.read_s3.assert_not_called()

    def test_s3_missing_limit_and_storage_failures_are_controlled(self):
        for error, status in ((FileNotFoundError("private missing key"), 404),
                              (OverflowError("private size details"), 413),
                              (OSError("private storage credentials"), 503)):
            with self.subTest(status=status), patch.object(self.app.logger, "warning") as warning:
                self.read_s3.side_effect = error
                with self.client().get(self.document_url("Unavailable.docx")) as response:
                    page = self.assert_preview(response, status)
                self.assertNotIn(str(error), page.text_content())
                self.assertNotIn("Traceback", page.text_content())
                self.assertNotIn(str(error), str(warning.call_args_list))
                if status == 404:
                    self.assertEqual(page.xpath("//a[contains(@href, 'download=1')]"), [])
                else:
                    self.download_link(page, "Unavailable.docx")
        self.build.assert_not_called()
        self.presign.assert_not_called()

    def test_original_download_auth_policy_is_unchanged_and_never_parses(self):
        filename = "Original DoCx.DOCX"
        (self.data / filename).write_bytes(self.original)
        for user in (None, "guest", "reader@example.invalid"):
            client = self.client(user)
            with (self.subTest(user=user, storage="local"),
                  client.get(self.document_url(filename), query_string={"download": "1"}) as response):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, self.original)
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn(filename, response.headers["Content-Disposition"])
            with (self.subTest(user=user, storage="s3"),
                  client.get(self.document_url("Remote.DOCX") + "?download=1") as response):
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, self.signed_url)
                self.presign.assert_called_with("Remote.DOCX", for_download=True)
        self.read_s3.assert_not_called()
        self.build.assert_not_called()

    def test_pdfs_keep_inline_and_attachment_paths_without_docx_processing(self):
        pdf = b"%PDF-1.4\n% Synthetic offline response fixture\n%%EOF"
        (self.data / "Local guide.PDF").write_bytes(pdf)
        client = self.client(None)
        for download in (False, True):
            query = {"download": "1"} if download else {}
            with (self.subTest(download=download, storage="local"),
                  client.get(self.document_url("Local guide.PDF"), query_string=query) as response):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, pdf)
                self.assertEqual(response.mimetype, "application/pdf")
                self.assertTrue(response.headers["Content-Disposition"].startswith(
                    "attachment" if download else "inline",
                ))
            with (self.subTest(download=download, storage="s3"),
                  client.get(self.document_url("Remote guide.pdf"), query_string=query) as response):
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, self.signed_url)
                self.presign.assert_called_with("Remote guide.pdf", for_download=download)
        self.read_s3.assert_not_called()
        self.build.assert_not_called()

    def test_unix_windows_and_control_character_traversal_never_reaches_storage(self):
        (self.tmp / "outside.docx").write_bytes(self.original)
        names = (".", "..", "../outside.docx", "folder/outside.docx", "..\\outside.docx",
                 "C:\\temp\\outside.docx", "\\\\server\\share\\outside.docx",
                 "safe.docx:alternate", "bad\x00.docx", "bad\r\n.docx", "bad\x7f.docx")
        client = self.client()
        for filename in names:
            for query in ({}, {"download": "1"}):
                with (self.subTest(filename=filename, query=query),
                      client.get(self.document_url(filename), query_string=query) as response):
                    self.assertEqual(response.status_code, 404)
        self.read_s3.assert_not_called()
        self.presign.assert_not_called()
        self.build.assert_not_called()

    def test_resolved_local_path_cannot_escape_through_a_symlink_or_junction(self):
        outside = self.tmp / "outside.docx"
        outside.write_bytes(self.original)
        resolve = Path.resolve

        def resolved(path, *args, **kwargs):
            # Model a symlink/junction without requiring Windows symlink privileges.
            return outside if path == self.data / "linked.docx" else resolve(path, *args, **kwargs)

        with (patch.object(Path, "resolve", resolved),
              self.client().get(self.document_url("linked.docx")) as response):
            self.assertEqual(response.status_code, 404)
        self.read_s3.assert_not_called()
        self.presign.assert_not_called()
        self.build.assert_not_called()

    def test_url_apis_route_docx_to_preview_and_keep_pdf_and_download_signing(self):
        for user in ("guest", "reader@example.invalid"):
            client = self.client(user)
            filename = "Staff & Fees #1.DoCx"
            with self.subTest(user=user, operation="docx-preview"):
                self.presign.reset_mock()
                with client.get("/api/files/view-url/" + quote(filename, safe="")) as response:
                    self.assertEqual(response.status_code, 200)
                    result = response.get_json()
                self.assertTrue(result["success"])
                url = urlsplit(result["url"])
                self.assertFalse(url.scheme or url.netloc or url.query)
                self.assertEqual(unquote(url.path), "/chat/document/" + filename)
                self.presign.assert_not_called()
            for operation, name, download in (("view-url", "Guide.PDF", False),
                                               ("download-url", filename, True),
                                               ("download-url", "Guide.pdf", True)):
                with self.subTest(user=user, operation=operation, filename=name):
                    self.presign.reset_mock()
                    with client.get("/api/files/" + operation + "/" + quote(name, safe="")) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_json(), {"success": True, "url": self.signed_url})
                    self.presign.assert_called_once_with(name, for_download=download)
        self.read_s3.assert_not_called()
        self.build.assert_not_called()

    def test_url_apis_still_reject_anonymous_users_without_signing(self):
        client = self.client(None)
        for operation in ("view-url", "download-url"):
            for filename in ("Guide.docx", "Guide.pdf"):
                with (self.subTest(operation=operation, filename=filename),
                      client.get(f"/api/files/{operation}/{filename}") as response):
                    self.assertEqual(response.status_code, 403)
                    self.assertFalse(response.get_json()["success"])
        self.presign.assert_not_called()
        self.read_s3.assert_not_called()


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__("Synthetic S3 failure")
        self.response = {"Error": {"Code": code}}


class RecordingBody(io.BytesIO):
    def __init__(self, data, error=None):
        super().__init__(data)
        self.read_sizes = []
        self.error = error

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.error is not None:
            raise self.error
        return super().read(size)


class S3ReadTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.client = Mock(spec=["get_object"])
        self.ns = {"s3_client": self.client, "S3_BUCKET_NAME": "offline-fixed-bucket",
                   "ClientError": FakeClientError, "closing": closing}
        extract_functions("aws/s3.py", ("read_s3_file",), self.ns)

    def test_exact_size_and_unknown_length_reads_are_bounded_and_closed(self):
        for headers in ({"ContentLength": 8}, {}):
            with self.subTest(headers=headers):
                body = RecordingBody(b"original")
                self.client.get_object.return_value = {"Body": body, **headers}
                self.client.reset_mock()
                self.assertEqual(self.ns["read_s3_file"]("Staff & Fees.DoCx", 8), b"original")
                self.assertEqual(body.read_sizes, [9])
                self.assertTrue(body.closed)
                self.client.get_object.assert_called_once_with(
                    Bucket="offline-fixed-bucket", Key="Staff & Fees.DoCx",
                )

    def test_advertised_oversize_closes_body_without_reading(self):
        body = RecordingBody(b"unused")
        self.client.get_object.return_value = {"Body": body, "ContentLength": 9}
        with self.assertRaises(OverflowError):
            self.ns["read_s3_file"]("large.docx", 8)
        self.assertEqual(body.read_sizes, [])
        self.assertTrue(body.closed)

    def test_missing_or_understated_length_cannot_bypass_the_byte_limit(self):
        for headers in ({}, {"ContentLength": 1}):
            with self.subTest(headers=headers):
                body = RecordingBody(b"x" * 100)
                self.client.get_object.return_value = {"Body": body, **headers}
                with self.assertRaises(OverflowError):
                    self.ns["read_s3_file"]("large.docx", 8)
                self.assertEqual(body.read_sizes, [9])
                self.assertTrue(body.closed)

    def test_read_failure_propagates_and_always_closes_the_stream(self):
        error = OSError("Synthetic interrupted stream")
        body = RecordingBody(b"", error=error)
        self.client.get_object.return_value = {"Body": body}
        with self.assertRaises(OSError) as caught:
            self.ns["read_s3_file"]("broken.docx", 8)
        self.assertIs(caught.exception, error)
        self.assertEqual(body.read_sizes, [9])
        self.assertTrue(body.closed)

    def test_only_missing_key_errors_become_file_not_found(self):
        for code in ("NoSuchKey", "NotFound", "404", "AccessDenied", "SlowDown", None):
            error = FakeClientError(code)
            self.client.get_object.side_effect = error
            with self.subTest(code=code):
                if code in ("NoSuchKey", "NotFound", "404"):
                    with self.assertRaises(FileNotFoundError) as caught:
                        self.ns["read_s3_file"]("missing.docx", 8)
                    self.assertIs(caught.exception.__cause__, error)
                else:
                    with self.assertRaises(FakeClientError) as caught:
                        self.ns["read_s3_file"]("protected.docx", 8)
                    self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
