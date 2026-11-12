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
            "BM25_VALUES_FILE": str(self.tmp / "absent-bm25.json"),
            "Pinecone": Mock(return_value=self.pc),
            "ensure_index_exists": Mock(name="ensure_index"),
            "BM25Encoder": Mock(return_value=encoder),
            "PineconeHybridSearchRetriever": Mock(return_value=self.retriever),
        }
        extract_definitions("src/helper.py", (
            "smart_chunking", "generate_chunk_id", "is_low_value_chunk",
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
            self.assertTrue(chunk.page_content.startswith(f"[SECTION: {heading}]\n"))
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
