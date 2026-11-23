"""Offline DOCX ingestion regressions using synthetic documents and AST-only code.

Run: python -B -X utf8 tests/test_docx_ingestion.py -v
Requires python-docx and lxml, not application clients, models, or credentials.
"""

import __future__
import ast
import base64
import hashlib
import io
import os
import re
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml import etree


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Document:
    page_content: str
    metadata: dict = field(default_factory=dict)


@contextmanager
def offline_only():
    forbidden = dict.fromkeys((
        "config", "dotenv", "aws", "rag", "src", "store_index", "sc_assistant",
        "boto3", "botocore", "pinecone", "langchain", "langchain_core",
        "langchain_community", "langchain_openai", "langchain_huggingface",
        "pinecone_text", "sentence_transformers", "openai", "transformers",
    ))
    denied = AssertionError("Network access is forbidden in DOCX ingestion tests")
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, forbidden))
        for target in (
            "socket.create_connection", "socket.socket.connect",
            "socket.socket.connect_ex", "socket.getaddrinfo",
        ):
            stack.enter_context(patch(target, side_effect=denied))
        yield


def extract_definitions(relative_path, names, namespace):
    """Execute selected definitions and their pure dependencies, never imports.

    Injected effects take precedence over production definitions. Constants may
    contain literals and re.compile calls, but no environment/client setup.
    """
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    definitions = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                definitions[node.targets[0].id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions[node.target.id] = node

    selected = {}
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in definitions:
            raise AssertionError(f"Missing AST test interface: {relative_path}:{name}")
        node = definitions[name]
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise AssertionError(f"Unexpected decorated test interface: {name}")
        else:
            for expression in ast.walk(node.value):
                if isinstance(expression, ast.Call) and not (
                    isinstance(expression.func, ast.Attribute)
                    and isinstance(expression.func.value, ast.Name)
                    and expression.func.value.id == "re"
                    and expression.func.attr == "compile"
                ):
                    raise AssertionError(f"Non-pure constant in AST test seam: {name}")
                if isinstance(expression, ast.Attribute) and not (
                    isinstance(expression.value, ast.Name)
                    and expression.value.id == "re"
                ):
                    raise AssertionError(f"External attribute in AST constant: {name}")
        selected[name] = node
        pending.extend(
            reference.id for reference in ast.walk(node)
            if isinstance(reference, ast.Name) and isinstance(reference.ctx, ast.Load)
            and reference.id in definitions and reference.id not in namespace
            and reference.id not in selected
        )

    body = sorted(selected.values(), key=lambda node: node.lineno)
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(
        module, str(path), "exec", flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    ), namespace)


def add_table(document, rows):
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for row, values in zip(table.rows, rows):
        for cell, value in zip(row.cells, values):
            cell.text = value
    return table


def roundtrip(document):
    # Discard construction-time cell proxies so they cannot mask recycled id(_tc).
    stream = io.BytesIO()
    document.save(stream)
    stream.seek(0)
    return docx.Document(stream)


def markdown_rows(text):
    rows = []
    for line in text.splitlines():
        if not re.match(r"^\s*\|.*\|\s*$", line):
            continue
        if re.match(r"^\s*\|[\s:|\-]+\|\s*$", line):
            continue
        rows.append([
            cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]
        ])
    return rows


class FakeJobs:
    def __init__(self):
        self.phases = []
        self.failed = []
        self.finished = []
        self.finished_jobs = []

    def set_phase(self, job_id, filename, phase, **units):
        self.phases.append((job_id, filename, phase, units))

    def fail_file(self, job_id, filename, reason):
        self.failed.append((job_id, filename, reason))

    def finish_file(self, job_id, filename, chunks):
        self.finished.append((job_id, filename, chunks))

    def finish_job(self, job_id):
        self.finished_jobs.append(job_id)


class IngestionTestCase(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.resources.enter_context(offline_only())
        self.tmp = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.embeddings = Mock(name="embeddings")
        self.embeddings.embed_query.return_value = [0.0, 0.0, 0.0]
        self.pc = Mock(name="pinecone_client")
        self.retriever = Mock(name="retriever")
        encoder = Mock(name="sparse_encoder")
        encoder.load.return_value = encoder
        encoder.default.return_value = encoder
        image = Mock(name="decoded_image")
        image.convert.return_value = image
        self.ns = {
            "__name__": "_docx_ingestion_under_test",
            "re": re, "hashlib": hashlib, "io": io,
            "os": SimpleNamespace(path=os.path),
            "docx": docx, "etree": etree, "qn": qn,
            "_Document": DocxDocument, "_Cell": _Cell,
            "Paragraph": Paragraph, "Table": Table, "Document": Document,
            "CT_P": CT_P, "CT_Tbl": CT_Tbl,
            "Image": SimpleNamespace(open=Mock(return_value=image)),
            "logger": Mock(name="logger"),
            "generate_image_caption": Mock(return_value=""),
            "preprocess_for_ocr": lambda image: image,
            "_record_effective_date": Mock(name="record_date"),
            "_learn_spelling_terms": Mock(name="learn_vocabulary"),
            "get_local_embeddings": Mock(side_effect=AssertionError("No model loads")),
            "process_pdf": Mock(return_value=[]),
            "get_loader": Mock(return_value=None),
            "PINECONE_API_KEY": "offline-placeholder",
            "VECTOR_BACKEND": "pinecone",
            "BM25_VALUES_FILE": str(self.tmp / "absent-bm25.json"),
            "Pinecone": Mock(return_value=self.pc),
            "ensure_index_exists": Mock(name="ensure_index"),
            "BM25Encoder": Mock(return_value=encoder),
            "PineconeHybridSearchRetriever": Mock(return_value=self.retriever),
            "unit_context_alias": lambda text: next((
                (name, acronym) for acronym, name in (
                    ("GS", "Graduate School"),
                    ("CITAS", "College of Information Technology and Allied Sciences"),
                    ("CoEd", "College of Education"),
                    ("CLAS", "College of Liberal Arts and Sciences"),
                    ("SCTI", "Samar College Technological Institute"),
                    ("COCJE", "College of Criminal Justice Education"),
                ) if re.search(rf"\b(?:{acronym}|{re.escape(name)})\b", text.replace("_", " "), re.I)
            ), None),
        }
        extract_definitions("src/helper.py", (
            "smart_chunking", "generate_chunk_id", "is_low_value_chunk",
            "drop_toc_text", "build_chunk_context_header",
        ), self.ns)
        extract_definitions("store_index.py", (
            "_docx_table_to_markdown", "process_docx_linear", "_chunk_and_dedup",
            "append_file_to_index",
        ), self.ns)

    def save_docx(self, document, name="synthetic.docx"):
        path = self.tmp / name
        document.save(str(path))
        return str(path)

    def append(self, path, filename="Directory.DOCX", progress=None):
        return self.ns["append_file_to_index"](
            path, index_name="offline-index", real_name=filename,
            embeddings=self.embeddings, progress=progress,
        )

    def assert_no_index_work(self):
        self.ns["Pinecone"].assert_not_called()
        self.ns["ensure_index_exists"].assert_not_called()
        self.pc.Index.assert_not_called()
        self.ns["PineconeHybridSearchRetriever"].assert_not_called()
        self.retriever.add_texts.assert_not_called()

    def assert_upsert_ids(self, count, filename):
        texts, metadatas, ids = [], [], []
        self.assertGreater(self.retriever.add_texts.call_count, 0)
        for call in self.retriever.add_texts.call_args_list:
            batch_texts = call.args[0]
            batch_metadata = call.kwargs["metadatas"]
            self.assertIn("ids", call.kwargs, "Pinecone IDs must be explicit")
            batch_ids = call.kwargs["ids"]
            self.assertEqual(batch_ids, [meta["chunk_id"] for meta in batch_metadata])
            self.assertEqual(len(batch_texts), len(batch_ids))
            texts.extend(batch_texts)
            metadatas.extend(batch_metadata)
            ids.extend(batch_ids)
        self.assertEqual(count, len(texts))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(meta["source"] == filename for meta in metadatas))
        return texts


class TableConversionTests(IngestionTestCase):
    def test_all_ordinary_rows_survive_without_retaining_cell_proxies(self):
        rows = [["UNIT", "CONTACT", "ROOM"]] + [
            [f"UNIT {i:03d}", f"STAFF {i:03d}", f"ROOM {i:03d}"]
            for i in range(96)
        ]
        document = docx.Document()
        add_table(document, rows)
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                table = roundtrip(document).tables[0]
                result = self.ns["_docx_table_to_markdown"](table)
                self.assertEqual(markdown_rows(result), rows)

    def test_identical_text_in_unmerged_cells_is_not_deduplicated(self):
        document = docx.Document()
        rows = [["PRIMARY", "SECONDARY"], ["SHARED", "SHARED"], ["SHARED", "SHARED"]]
        add_table(document, rows)
        result = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        self.assertEqual(markdown_rows(result), rows)

    def test_vertical_merge_repeats_label_for_each_ordinary_row(self):
        document = docx.Document()
        rows = [
            ["UNIT", "CONTACT", "ROOM"],
            ["ARTS", "STAFF A", "101"],
            ["", "STAFF B", "102"],
            ["", "STAFF C", "103"],
            ["SCIENCE", "STAFF D", "104"],
        ]
        table = add_table(document, rows)
        table.cell(1, 0).merge(table.cell(3, 0)).text = "ARTS"
        expected = [list(row) for row in rows]
        expected[2][0] = expected[3][0] = "ARTS"
        result = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        self.assertEqual(markdown_rows(result), expected)

    def test_horizontal_merge_leaves_only_continuation_slots_blank(self):
        document = docx.Document()
        rows = [
            ["UNIT", "CONTACT", "ROOM"],
            ["SHARED SERVICE", "", "ANNEX"],
            ["SCIENCE", "STAFF A", "201"],
        ]
        table = add_table(document, rows)
        table.cell(1, 0).merge(table.cell(1, 1)).text = "SHARED SERVICE"
        result = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        self.assertEqual(markdown_rows(result), rows)

    def test_literal_pipes_are_escaped_without_changing_cell_boundaries(self):
        document = docx.Document()
        rows = [["Unit | Desk", "Contact"], ["Arts | Science", "STAFF A"]]
        add_table(document, rows)
        result = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        self.assertIn(r"Unit \| Desk", result)
        self.assertIn(r"Arts \| Science", result)
        self.assertEqual(markdown_rows(result), rows)


class TableChunkingTests(IngestionTestCase):
    def test_table_rows_are_never_section_headings(self):
        for line in (
            "| UNIT | CONTACT |", "| ARTS | STAFF A |",
            "  | COLLEGE OF SCIENCE | STAFF B |  ", "| --- | --- |",
        ):
            with self.subTest(line=line):
                self.assertFalse(self.ns["is_heading_line"](line))
                self.assertFalse(self.ns["is_major_heading"](line))
                self.assertIsNone(self.ns["detect_block_header"](line))
        self.assertTrue(self.ns["is_major_heading"]("ACADEMIC SERVICES"))

    def test_small_and_numeric_meaningful_tables_survive_value_filter(self):
        short = "| Id | Fee |\n| --- | --- |\n| A | 7 |"
        numeric = "| ITEM | FEE |\n| --- | --- |\n" + "\n".join(
            f"| A{i:02d} | {i * 100} |" for i in range(1, 9)
        )
        self.assertLess(len(short), 40)
        for table in (short, numeric):
            for prefix in ("", "[SECTION: Fees]\n"):
                with self.subTest(table=table, tagged=bool(prefix)):
                    self.assertFalse(self.ns["is_low_value_chunk"](prefix + table))
        for empty in ("", "   ", "[SECTION: Fees]", "| --- | --- |"):
            self.assertTrue(self.ns["is_low_value_chunk"](empty))

    def test_large_table_splits_repeat_header_and_preserve_each_row(self):
        rows = [["UNIT", "CONTACT", "DETAILS"]] + [
            [f"UNIT {i:02d}", f"STAFF {i:02d}", "Visits use the east entrance daily"]
            for i in range(18)
        ]
        document = docx.Document()
        add_table(document, rows)
        table = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        header, separator, *body = table.splitlines()
        for overlap in (0, 15):
            with self.subTest(overlap=overlap):
                chunks, heading = self.ns["smart_chunking"](
                    table, max_tokens=65, overlap=overlap,
                    token_counter=lambda text: len(text.split()),
                    running_header="Directory contacts",
                )
                self.assertGreater(len(chunks), 1)
                self.assertEqual(heading, "Directory contacts")
                observed = []
                for chunk in chunks:
                    self.assertTrue(chunk.startswith("[SECTION: Directory contacts]\n"))
                    lines = [line for line in chunk.splitlines() if line.startswith("|")]
                    self.assertEqual(lines[:2], [header, separator])
                    self.assertGreater(len(lines), 2, "No header-only table chunks")
                    self.assertTrue(all(line in body for line in lines[2:]))
                    observed.extend(lines[2:])
                self.assertEqual(set(observed), set(body))
                if overlap == 0:
                    self.assertEqual(Counter(observed), Counter(body))

    def test_single_over_budget_table_row_is_not_split_into_sentences(self):
        document = docx.Document()
        rows = [
            ["UNIT", "NOTES"],
            ["ARTS", "The first complete sentence describes the required documents. "
             "The second complete sentence describes where visitors must submit them."],
            ["SCIENCE", "The office accepts forms each working day before noon."],
        ]
        add_table(document, rows)
        table = self.ns["_docx_table_to_markdown"](roundtrip(document).tables[0])
        chunks, _ = self.ns["smart_chunking"](
            table, max_tokens=12, overlap=0,
            token_counter=lambda text: len(text.split()), running_header="Office notes",
        )
        lines = [line for chunk in chunks for line in chunk.splitlines()]
        for row in table.splitlines()[2:]:
            self.assertEqual(lines.count(row), 1)

    def test_large_table_continuations_repeat_the_table_context(self):
        context = "[TABLE context: Digital Systems / Second Year / Second Semester]"
        rows = [["CODE", "TITLE", "UNITS"]] + [
            [f"DS {i:03d}", f"Practical laboratory subject {i:03d}", "3"]
            for i in range(20)
        ]
        document = docx.Document()
        table = self.ns["_docx_table_to_markdown"](add_table(document, rows))
        chunks, _ = self.ns["smart_chunking"](
            context + "\n" + table, max_tokens=65, overlap=0,
            token_counter=lambda text: len(text.split()), running_header="Curriculum",
        )
        self.assertGreater(len(chunks), 1)
        observed = []
        for chunk in chunks:
            self.assertIn(context, chunk)
            parsed = markdown_rows(chunk)
            self.assertEqual(parsed[0], rows[0])
            self.assertGreater(len(parsed), 1)
            observed.extend(parsed[1:])
        self.assertEqual(observed, rows[1:])


class UploadConfigurationTests(IngestionTestCase):
    def test_online_uploads_are_enabled_by_default_but_still_admin_only(self):
        from flask import Flask, jsonify, request, session

        tree = ast.parse((ROOT / "sc_assistant/admin.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upload_file")
        handler.decorator_list = []
        namespace = {"os": os, "request": request, "session": session, "jsonify": jsonify,
                     "is_admin": lambda: session.get("role") == "admin"}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "upload_configuration", "exec"), namespace)
        app = Flask("upload_configuration_test")
        app.secret_key = "offline-upload-test"
        app.add_url_rule("/upload", view_func=namespace["upload_file"], methods=["POST"])
        for flag, role, expected in ((None, "admin", 400), ("true", "admin", 400),
                                     ("false", "admin", 503), ("true", "user", 403), ("true", None, 403)):
            with self.subTest(flag=flag, role=role), patch.dict(os.environ, {}, clear=False):
                if flag is None:
                    os.environ.pop("WEB_INDEXING_ENABLED", None)
                else:
                    os.environ["WEB_INDEXING_ENABLED"] = flag
                client = app.test_client()
                if role:
                    with client.session_transaction() as state:
                        state.update(user="preview@example.com", role=role)
                response = client.post("/upload")
                self.assertEqual(response.status_code, expected)
                if expected == 400:
                    self.assertEqual(response.get_json()["message"], "No file part")
                elif expected == 503:
                    self.assertIn("WEB_INDEXING_ENABLED", response.get_json()["message"])
        self.assert_no_index_work()


class NativeExtractionTests(IngestionTestCase):
    def test_conflict_scanner_includes_native_table_rows_in_reading_order(self):
        document = docx.Document()
        document.add_paragraph("Academic leadership directory")
        add_table(document, [["Office", "Person"], ["Dean, Example College", "ALICE EXAMPLE"]])
        document.add_paragraph("Historical staff captions")
        path = self.save_docx(document)
        extract_definitions("sc_assistant/admin_conflicts.py", ("_pages_from_docx",), self.ns)
        pages = list(self.ns["_pages_from_docx"](path, "Directory.docx"))
        self.assertEqual(pages[0][:2], ("Directory.docx", None))
        text = pages[0][2]
        self.assertIn("Dean, Example College\nALICE EXAMPLE", text)
        self.assertLess(text.index("Academic leadership"), text.index("ALICE EXAMPLE"))
        self.assertLess(text.index("ALICE EXAMPLE"), text.index("Historical staff"))

    def test_live_prompt_does_not_treat_a_filename_as_an_absolute_overwrite(self):
        tree = ast.parse((ROOT / "src/prompt.py").read_text(encoding="utf-8"))
        prompt = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                      and node.targets[0].id == "system_prompt")
        self.assertNotIn("ABSOLUTE OVERWRITE", prompt)
        self.assertNotIn("deans_update.docx", prompt)
        self.assertIn("PERSONNEL ROSTERS", prompt)

    def test_invalid_docx_raises_value_error(self):
        path = self.tmp / "invalid.docx"
        path.write_bytes(b"Synthetic invalid DOCX fixture, not a zip package")
        with self.assertRaises(ValueError):
            self.ns["process_docx_linear"](str(path), "Invalid.DOCX")
        self.assert_no_index_work()

    def test_empty_native_docx_raises_value_error(self):
        document = docx.Document()
        document.add_paragraph(" \t ")
        path = self.save_docx(document)
        with self.assertRaises(ValueError):
            self.ns["process_docx_linear"](path, "Empty.DOCX")
        self.assert_no_index_work()

    def test_image_only_docx_with_empty_ocr_raises_value_error(self):
        document = docx.Document()
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
        document.add_picture(io.BytesIO(pixel))
        with self.assertRaises(ValueError):
            self.ns["process_docx_linear"](self.save_docx(document), "ImageOnly.DOCX")
        self.ns["generate_image_caption"].assert_called_once()
        self.assert_no_index_work()

    def test_native_sections_preserve_table_facts_order_and_source_metadata(self):
        document = docx.Document()
        headings = ["Directory contacts", "Facilities contacts"]
        first_rows = [["UNIT", "CONTACT", "ROOM"]] + [
            [f"UNIT {i:02d}", f"STAFF {i:02d}", f"ROOM {i:02d}"]
            for i in range(36)
        ]
        second_rows = [["SERVICE", "CONTACT"], ["REPAIRS", "STAFF Z"]]
        for heading, rows in zip(headings, (first_rows, second_rows)):
            document.add_heading(heading, level=1)
            document.add_paragraph("Before the contact table.")
            add_table(document, rows)
            document.add_paragraph("After the contact table.")
        result = self.ns["process_docx_linear"](
            self.save_docx(document, "staged.bin"), "StaffDirectory.DoCx",
        )
        self.assertEqual(len(result), 2)
        for section, heading, rows in zip(result, headings, (first_rows, second_rows)):
            self.assertEqual(section.metadata["source"], "StaffDirectory.DoCx")
            self.assertEqual(section.metadata["type"], "docx_section")
            self.assertEqual(section.metadata["section_heading"], heading)
            self.assertEqual(markdown_rows(section.page_content), rows)
            self.assertLess(section.page_content.index("Before the contact table."),
                            section.page_content.index("| " + rows[0][0]))
            self.assertLess(section.page_content.index("| " + rows[-1][0]),
                            section.page_content.index("After the contact table."))
        self.ns["generate_image_caption"].assert_not_called()


class SectionIdentityTests(IngestionTestCase):
    def test_indexed_chunks_have_canonical_unit_context_headers(self):
        cases = (
            ("Graduate School", "GRADING SYSTEM OF THE GS", "Averaging system table includes grades and rating ranges."),
            ("Academic Calendar", "CITAS CALENDAR OF ACTIVITIES A.Y. 2026-2027", "Opening of 2nd Semester is scheduled for December."),
            ("Curriculum", "College of Education / BEEd", "Course subjects and units are listed for every semester."),
            ("SCTI_Program.docx", "Training Offerings", "Cookery NC II and Computer System Servicing are offered."),
            ("CLAS.docx", "College of Liberal Arts and Sciences", "The CLAS department offers student courses."),
            ("COCJE.docx", "College of Criminal Justice Education", "COCJE publishes the program requirements."),
        )
        expected_scopes = ("Graduate School (GS)", "College of Information Technology and Allied Sciences (CITAS)",
                           "College of Education (CoEd)", "Samar College Technological Institute (SCTI)",
                           "College of Liberal Arts and Sciences (CLAS)",
                           "College of Criminal Justice Education (COCJE)")
        for (source, heading, body), scope in zip(cases, expected_scopes):
            with self.subTest(heading=heading):
                chunk, = self.ns["_chunk_and_dedup"]([Document(
                    page_content=body,
                    metadata={"source": source, "type": "docx_section", "section_heading": heading},
                )])
                self.assertTrue(chunk.page_content.startswith(f"{scope} - {heading}\n"), chunk.page_content[:200])

    def test_toc_headings_and_dotted_leader_entries_are_removed_before_chunking(self):
        self.assertEqual(self.ns["drop_toc_text"]("TABLE OF CONTENTS\nChapter 1 ........ 1\nChapter 2 ...... 15"), "")
        self.assertEqual(self.ns["drop_toc_text"]("Useful policy\nChapter 1 ........ 1\nKeep this paragraph."),
                         "Useful policy\nKeep this paragraph.")
        self.assertEqual(self.ns["drop_toc_text"]("Chapter 1 ........ 1\nChapter 2 ........ 5\nKeep this paragraph."),
                         "Keep this paragraph.")
        chunks = self.ns["_chunk_and_dedup"]([
            Document("TABLE OF CONTENTS\nChapter 1 ........ 1\nChapter 2 ...... 15",
                     {"source": "Handbook.pdf", "page": 1, "type": "pdf"}),
            Document("Chapter 1 ........ 1\nUseful policy details remain here for students and staff.",
                     {"source": "Handbook.pdf", "page": 2, "type": "pdf"}),
        ])
        self.assertEqual(len(chunks), 1)
        self.assertNotIn("TABLE OF CONTENTS", chunks[0].page_content)
        self.assertNotIn("Chapter 1 ........ 1", chunks[0].page_content)
        self.assertIn("Useful policy details", chunks[0].page_content)

    def test_docx_table_of_contents_section_is_not_emitted(self):
        document = docx.Document()
        document.add_heading("TABLE OF CONTENTS", level=1)
        document.add_paragraph("Graduate School ........ 16")
        document.add_paragraph("Grading System ........ 18")
        document.add_heading("ADMISSION REQUIREMENTS", level=1)
        document.add_paragraph("Applicants submit their complete academic records.")
        sections = self.ns["process_docx_linear"](self.save_docx(document), "Handbook.docx")
        self.assertEqual(len(sections), 1, [(section.metadata, section.page_content) for section in sections])
        self.assertEqual(sections[0].metadata["section_heading"], "ADMISSION REQUIREMENTS")
        self.assertNotIn("Graduate School ........ 16", sections[0].page_content)

    def test_docx_section_metadata_controls_heading_without_cross_section_stitching(self):
        docs = [
            Document(
                page_content=f"The contact is {marker}. Visitors should bring their "
                "completed forms to the office during regular working hours.",
                metadata={"source": "Directory.DOCX", "type": "docx_section",
                          "section_heading": heading},
            )
            for marker, heading in (("STAFF ALPHA", "Primary contacts"),
                                    ("STAFF BETA", "Secondary contacts"))
        ]
        chunks = self.ns["_chunk_and_dedup"](docs)
        self.assertEqual({chunk.metadata["section_heading"] for chunk in chunks},
                         {"Primary contacts", "Secondary contacts"})
        for chunk in chunks:
            heading = chunk.metadata["section_heading"]
            own, other = (("STAFF ALPHA", "STAFF BETA") if heading == "Primary contacts"
                          else ("STAFF BETA", "STAFF ALPHA"))
            self.assertEqual(chunk.metadata["section"], heading)
            self.assertTrue(chunk.page_content.startswith(f"Samar Colleges, Inc. - {heading}\n[SECTION: {heading}]\n"))
            self.assertIn(own, chunk.page_content)
            self.assertNotIn(other, chunk.page_content)

    def test_identical_facts_in_different_docx_sections_do_not_collapse(self):
        text = "Visitors must bring their completed application forms to the office."
        docs = [Document(text, {
            "source": "Directory.DOCX", "type": "docx_section", "section_heading": heading,
        }) for heading in ("Morning contacts", "Afternoon contacts")]
        chunks = self.ns["_chunk_and_dedup"](docs)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len({chunk.metadata["chunk_id"] for chunk in chunks}), 2)

    def test_identical_pdf_and_docx_content_has_distinct_stable_source_ids(self):
        text = "Visitors must bring their completed application forms to the office."
        docs = [
            Document(text, {"source": "Directory.PDF", "page": 0, "type": "pdf_text"}),
            Document(text, {"source": "Directory.DOCX", "type": "docx_section",
                            "section_heading": "General College Information"}),
        ]
        chunks = self.ns["_chunk_and_dedup"](docs)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].page_content, chunks[1].page_content)
        ids = [chunk.metadata["chunk_id"] for chunk in chunks]
        self.assertNotEqual(ids[0], ids[1])
        for chunk in chunks:
            self.assertEqual(chunk.metadata["chunk_id"], self.ns["generate_chunk_id"](
                chunk.metadata["source"], 0, chunk.metadata["chunk_index"],
                chunk.page_content,
            ))
        self.assertEqual(ids, [
            chunk.metadata["chunk_id"] for chunk in self.ns["_chunk_and_dedup"](docs)
        ])


class CurriculumIngestionTests(IngestionTestCase):
    def test_normal_style_titles_keep_degree_identity_across_course_categories(self):
        document = docx.Document()
        expected = []
        programs = (("College of Criminal Justice Education", "Bachelor of Science in Criminology"),
                    ("College of Information Technology", "Bachelor of Science in Information Technology"))
        for college, program in programs:
            for text in ("UNDERGRADUATE PROGRAMS", college, "ACADEMIC PROGRAMS", "Programs Offered", "Curriculum", program):
                document.add_paragraph(text)
            for category in ("General Education (24 units)", "Professional Courses (57 units)"):
                document.add_paragraph(category)
                rows = [["COURSE CODE", "COURSE TITLE", "UNITS", "PRE-REQUISITE"],
                        ["CoCJE GE 111", "Understanding Self, GAD and Peace Education", "3 units", "None"]]
                add_table(document, rows)
                expected.append((college, program, category, rows))
        document.add_heading("FACULTY", level=1)
        add_table(document, [["COURSE CODE", "COURSE TITLE", "UNITS"], ["OTHER 101", "Unrelated course", "3"]])
        docs = self.ns["process_docx_linear"](self.save_docx(document), "Categories.docx")
        tables = [doc for doc in docs if markdown_rows(doc.page_content)]
        self.assertEqual(len(tables), len(expected) + 1)
        for table, (college, program, category, rows) in zip(tables, expected):
            self.assertEqual(markdown_rows(table.page_content), rows)
            for label in (college, program, category):
                self.assertIn(label, table.metadata["section_heading"])
                self.assertIn(label, table.page_content.splitlines()[0])
            other = programs[1][1] if program == programs[0][1] else programs[0][1]
            self.assertNotIn(other, table.page_content)
        self.assertNotIn("Bachelor", tables[-1].metadata["section_heading"])
        self.assert_no_index_work()

    def curriculum(self, rows_per_table=3):
        document = docx.Document()
        document.add_heading("ACADEMIC PROGRAMS", level=1)
        programs = ("Bachelor of Science in Digital Systems (BSDS)",
                    "Bachelor of Multimedia Arts (BMMA)")
        expected = []
        for program_index, program in enumerate(programs):
            document.add_heading(program, level=2)
            document.add_heading("Curriculum", level=3)
            document.add_paragraph("The program includes general and professional courses.")
            for year in ("First Year", "Second Year"):
                document.add_heading(year, level=3)
                for semester in ("First Semester", "Second Semester"):
                    # Semester labels in exported curricula are often Normal, not headings.
                    document.add_paragraph(semester)
                    rows = [["COURSE CODE", "DESCRIPTIVE TITLE", "LEC", "LAB", "UNITS / PRE-REQUISITE"]]
                    rows += [[f"P{program_index} {i:03d}", f"Laboratory subject {i:03d}", "2", "3", "3"]
                             for i in range(rows_per_table)]
                    add_table(document, rows)
                    expected.append((program, year, semester, rows))
        return document, expected

    def test_each_semester_keeps_its_own_program_year_and_complete_rows(self):
        document, expected = self.curriculum()
        docs = self.ns["process_docx_linear"](self.save_docx(document), "Curriculum.DOCX")
        extract_definitions("rag/reranker.py", ("_header_of",), self.ns)
        tables = [doc for doc in docs if markdown_rows(doc.page_content)]
        self.assertEqual(len(tables), len(expected), "Each semester needs independent retrieval context")
        for table, (program, year, semester, rows) in zip(tables, expected):
            with self.subTest(program=program, year=year, semester=semester):
                self.assertEqual(markdown_rows(table.page_content), rows)
                chunks = self.ns["_chunk_and_dedup"]([table])
                self.assertEqual(len(chunks), 1)
                for chunk in chunks:
                    self.assertEqual(chunk.metadata["source"], "Curriculum.DOCX")
                    self.assertIn("subjects", chunk.page_content.lower())
                    self.assertEqual(chunk.page_content.splitlines()[0].count("ACADEMIC PROGRAMS"), 1)
                    for label in (program, year, semester):
                        self.assertIn(label, chunk.metadata["section_heading"])
                        self.assertIn(label, chunk.page_content.splitlines()[0])
                        self.assertIn(label, self.ns["_header_of"](chunk))
                    other_program = expected[-1][0] if "BSDS" in program else expected[0][0]
                    other_semester = "Second Semester" if semester == "First Semester" else "First Semester"
                    self.assertNotIn(other_program, chunk.page_content)
                    self.assertNotIn(other_semester, chunk.page_content)
        chunks = self.ns["_chunk_and_dedup"](docs)
        ids = [chunk.metadata["chunk_id"] for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)), "Shared GE rows must keep distinct program/term identities")
        self.assert_no_index_work()

    def test_non_catalog_tables_keep_a_shared_caption_until_the_next_heading(self):
        document = docx.Document()
        program = "Bachelor of Science in Digital Systems (BSDS)"
        document.add_heading(program, level=1)
        document.add_heading("Third Year", level=2)
        document.add_paragraph("First Semester")
        for index in range(2):
            add_table(document, [["SUBJECT", "CREDITS"], [f"Systems Laboratory {index}", "3"]])
        document.add_heading("Second Semester", level=3)
        add_table(document, [["SUBJECT", "CREDITS"], ["Systems Practicum", "6"]])
        docs = self.ns["process_docx_linear"](self.save_docx(document), "Degree.docx")
        tables = [doc for doc in docs if markdown_rows(doc.page_content)]
        self.assertEqual(len(tables), 3)
        for index, table in enumerate(tables):
            heading = table.metadata["section_heading"]
            self.assertIn(program, heading)
            self.assertIn("Third Year", heading)
            self.assertIn("First Semester" if index < 2 else "Second Semester", heading)
            self.assertNotIn("Second Semester" if index < 2 else "First Semester", heading)
            self.assertFalse(table.metadata.get("collection_heading"))
        self.assert_no_index_work()

    def test_large_curriculum_tables_keep_context_and_rows_on_every_continuation(self):
        document, expected = self.curriculum(rows_per_table=90)
        docs = self.ns["process_docx_linear"](self.save_docx(document), "LargeCurriculum.docx")
        tables = [doc for doc in docs if markdown_rows(doc.page_content)]
        self.assertEqual(len(tables), len(expected))
        for table, (program, year, semester, rows) in zip(tables, expected):
            chunks = self.ns["_chunk_and_dedup"]([table])
            self.assertGreater(len(chunks), 1)
            observed = []
            for chunk in chunks:
                for label in (program, year, semester):
                    self.assertIn(label, chunk.metadata["section_heading"])
                    self.assertIn(label, chunk.page_content.splitlines()[0])
                parsed = markdown_rows(chunk.page_content)
                self.assertEqual(parsed[0], rows[0])
                self.assertGreater(len(parsed), 1)
                observed.extend(parsed[1:])
            self.assertEqual(observed, rows[1:])
        self.assert_no_index_work()


class AppendTests(IngestionTestCase):
    def test_empty_extraction_fails_before_client_or_index_creation(self):
        self.ns["process_docx_linear"] = Mock(return_value=[])
        with self.assertRaises(ValueError):
            self.append(str(self.tmp / "unused.docx"))
        self.assert_no_index_work()

    def test_empty_chunks_fail_before_client_or_index_creation(self):
        document = docx.Document()
        document.add_paragraph("This native text is deliberately filtered by the chunk stub.")
        self.ns["_chunk_and_dedup"] = Mock(return_value=[])
        with self.assertRaises(ValueError):
            self.append(self.save_docx(document))
        self.ns["_chunk_and_dedup"].assert_called_once()
        self.assert_no_index_work()

    def test_valid_native_docx_upserts_explicit_ids_and_original_filename(self):
        document = docx.Document()
        document.add_heading("Directory contacts", level=1)
        rows = [["UNIT", "CONTACT", "ROOM"]] + [
            [f"UNIT {i:02d}", f"STAFF {i:02d}", f"ROOM {i:02d}"]
            for i in range(20)
        ]
        add_table(document, rows)
        progress = Mock()
        count = self.append(self.save_docx(document, "staged.bin"),
                            filename="StaffDirectory.DoCx", progress=progress)
        self.assertIsInstance(count, int)
        self.assertGreater(count, 0)
        texts = self.assert_upsert_ids(count, "StaffDirectory.DoCx")
        observed = markdown_rows("\n".join(texts))
        for row in rows:
            self.assertIn(row, observed)
        self.ns["Pinecone"].assert_called_once_with(api_key="offline-placeholder")
        self.pc.Index.assert_called_once_with("offline-index")
        self.ns["PineconeHybridSearchRetriever"].assert_called_once()
        indexing = [call.args for call in progress.call_args_list
                    if call.args[0] == "indexing"]
        batches = self.retriever.add_texts.call_count
        self.assertEqual(indexing[-1], ("indexing", batches, batches))

    def test_append_reports_actual_chunk_count_not_batch_count(self):
        document = docx.Document()
        document.add_paragraph("Synthetic native text for the isolated batching contract.")
        chunks = [Document(f"Synthetic searchable contact number {i}.", {
            "source": "Directory.DOCX", "chunk_id": f"synthetic-chunk-{i}",
            "chunk_index": i,
        }) for i in range(101)]
        self.ns["_chunk_and_dedup"] = Mock(return_value=chunks)
        count = self.append(self.save_docx(document))
        self.assertEqual(count, 101)
        self.assertEqual(self.retriever.add_texts.call_count, 2)
        self.assert_upsert_ids(count, "Directory.DOCX")


class UploadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.resources.enter_context(offline_only())
        self.jobs = FakeJobs()
        self.ns = {
            "jobs": self.jobs, "INDEX_NAME": "offline-index", "embeddings": object(),
            "append_file_to_index": Mock(return_value=0),
            "save_file_metadata": Mock(name="save_metadata"),
            "upload_file_to_s3": Mock(return_value=True),
            "os": SimpleNamespace(path=SimpleNamespace(exists=lambda path: True),
                                  remove=Mock(name="remove_staged_file")),
            "print": Mock(),
        }
        extract_definitions("sc_assistant/admin.py", ("_index_uploaded_files",), self.ns)

    def test_zero_chunks_marks_failure_without_metadata_or_s3_upload(self):
        self.ns["_index_uploaded_files"](
            "job-one", [("synthetic-stage.docx", "Directory.DOCX")], "test-user",
        )
        self.assertEqual(len(self.jobs.failed), 1)
        self.assertEqual(self.jobs.failed[0][:2], ("job-one", "Directory.DOCX"))
        self.assertTrue(self.jobs.failed[0][2])
        self.assertEqual(self.jobs.finished, [])
        self.assertEqual(self.jobs.finished_jobs, ["job-one"])
        self.assertFalse(any(phase[2] == "storing" for phase in self.jobs.phases))
        self.ns["save_file_metadata"].assert_not_called()
        self.ns["upload_file_to_s3"].assert_not_called()
        self.ns["os"].remove.assert_called_once_with("synthetic-stage.docx")

    def test_zero_chunk_failure_does_not_abandon_next_valid_file(self):
        self.ns["append_file_to_index"].side_effect = [0, 2]
        self.ns["_index_uploaded_files"]("job-two", [
            ("empty-stage.docx", "Empty.DOCX"),
            ("valid-stage.docx", "Directory.DoCx"),
        ], "test-user")
        self.assertEqual(len(self.jobs.failed), 1)
        self.assertEqual(self.jobs.failed[0][:2], ("job-two", "Empty.DOCX"))
        self.assertEqual(self.jobs.finished, [("job-two", "Directory.DoCx", 2)])
        self.assertEqual(self.jobs.finished_jobs, ["job-two"])
        self.ns["save_file_metadata"].assert_called_once_with("Directory.DoCx", "test-user")
        self.ns["upload_file_to_s3"].assert_called_once_with("valid-stage.docx", "Directory.DoCx")
        self.assertEqual(self.ns["os"].remove.call_count, 2)


if __name__ == "__main__":
    unittest.main()
