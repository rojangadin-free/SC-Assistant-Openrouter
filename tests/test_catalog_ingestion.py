"""Offline catalog/context and PDF fallback regressions using AST-only code.

Run: python -B -X utf8 tests/test_catalog_ingestion.py -v
Only synthetic DOCX documents are opened; PDF pages and OCR are test doubles.
"""

import re
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import docx

from test_docx_ingestion import (
    Document, IngestionTestCase, add_table, extract_definitions, markdown_rows,
    offline_only,
)


CATALOG_ITEMS = (
    ("Graduate School", "List Paragraph"),
    ("Master of Arts in Learning Research (MALR)", "List Paragraph"),
    ("Doctor of Philosophy in Community Studies (DPCS)", "List Paragraph"),
    ("College of Learning Sciences", "Heading 2"),
    ("Bachelor of Science in Learning Design (BSLD)", "Heading 2"),
    ("Major in Classroom Technology", "List Paragraph"),
    ("Major in Community Learning", "List Paragraph"),
    ("Bachelor of Elementary Studies (BES)", "Heading 2"),
    ("College of Coastal Studies", "List Paragraph"),
    ("Bachelor of Science in Coastal Mapping (BSCM)", "List Paragraph"),
    ("Major in Survey Design", "List Paragraph"),
    ("College of Applied Computing", "List Paragraph"),
    ("Bachelor of Science in Digital Systems (BSDS)", "List Paragraph"),
    ("College of Management Studies", "List Paragraph"),
    ("Bachelor of Science in Service Management (BSSM)", "List Paragraph"),
    ("Major in Hospitality Operations", "List Paragraph"),
    ("Technical Training Institute", "List Paragraph"),
    ("Training Offerings:", "List Paragraph"),
    ("Certificate in Equipment Servicing", "List Paragraph"),
    ("Certificate in Digital Fabrication", "List Paragraph"),
    ("Basic Education", "List Paragraph"),
    ("Senior High School", "List Paragraph"),
    ("Junior High School", "List Paragraph"),
    ("Elementary Education", "List Paragraph"),
)

DEAN_ROWS = [
    ["COLLEGE", "DEAN", "OFFICE"],
    ["COLLEGE OF LEARNING SCIENCES", "DEAN ALPHA", "ROOM 101"],
    ["COLLEGE OF COASTAL STUDIES", "DEAN BETA", "ROOM 102"],
    ["COLLEGE OF APPLIED COMPUTING", "DEAN GAMMA", "ROOM 103"],
    ["COLLEGE OF MANAGEMENT STUDIES", "DEAN DELTA", "ROOM 104"],
]


class CatalogExtractionTests(IngestionTestCase):
    def test_mixed_native_styles_keep_every_item_under_the_catalog_heading(self):
        for heading in ("COURSE OFFERING", "Programs Offered", "Academic Programs"):
            with self.subTest(heading=heading):
                document = docx.Document()
                document.add_heading(heading, level=2)
                for text, style in CATALOG_ITEMS:
                    document.add_paragraph(text, style=style)
                document.add_paragraph("ORGANIZATIONAL STRUCTURE", style="Normal")
                add_table(document, DEAN_ROWS)

                docs = self.ns["process_docx_linear"](
                    self.save_docx(document), "SyntheticCatalog.DoCx",
                )
                catalogs = [doc for doc in docs
                            if doc.metadata.get("collection_heading") == heading]
                self.assertEqual(len(catalogs), 1, "A small catalog is one logical section")
                catalog = catalogs[0]
                self.assertEqual(catalog.metadata["source"], "SyntheticCatalog.DoCx")
                self.assertEqual(catalog.metadata["type"], "docx_section")
                self.assertEqual(catalog.metadata["section_heading"], heading)
                positions = []
                for text, _ in CATALOG_ITEMS:
                    self.assertEqual(catalog.page_content.count(text), 1, text)
                    positions.append(catalog.page_content.index(text))
                self.assertEqual(positions, sorted(positions), "Catalog order changed")
                self.assertNotIn("ORGANIZATIONAL STRUCTURE", catalog.page_content)
                self.assertEqual(markdown_rows(catalog.page_content), [])

                rosters = [doc for doc in docs if markdown_rows(doc.page_content)]
                self.assertEqual(len(rosters), 1)
                self.assertEqual(rosters[0].metadata["section_heading"],
                                 "ORGANIZATIONAL STRUCTURE")
                self.assertFalse(rosters[0].metadata.get("collection_heading"))
                self.assertEqual(markdown_rows(rosters[0].page_content), DEAN_ROWS)

                chunks = self.ns["_chunk_and_dedup"](docs)
                for text, _ in CATALOG_ITEMS:
                    matches = [chunk for chunk in chunks if text in chunk.page_content]
                    self.assertTrue(matches, f"Catalog item was dropped: {text}")
                    for chunk in matches:
                        self.assertEqual(chunk.metadata.get("collection_heading"), heading)
                        self.assertIn(heading, chunk.page_content)
                        self.assertFalse(markdown_rows(chunk.page_content))
                roster_chunks = [chunk for chunk in chunks
                                 if markdown_rows(chunk.page_content)]
                self.assertTrue(roster_chunks)
                self.assertEqual(markdown_rows("\n".join(
                    chunk.page_content for chunk in roster_chunks
                )), DEAN_ROWS)
                for chunk in roster_chunks:
                    self.assertFalse(chunk.metadata.get("collection_heading"))
                    self.assertNotIn(heading, chunk.page_content)
                    self.assertNotIn("Bachelor of", chunk.page_content)
                self.assert_no_index_work()
        self.ns["generate_image_caption"].assert_not_called()

    def test_an_unrelated_native_roster_heading_ends_the_catalog(self):
        document = docx.Document()
        document.add_heading("Programs Offered", level=2)
        document.add_heading("College of Coastal Studies", level=2)
        degree = "Bachelor of Science in Coastal Mapping (BSCM)"
        document.add_heading(degree, level=2)
        document.add_heading("Academic Personnel", level=2)
        add_table(document, DEAN_ROWS)
        docs = self.ns["process_docx_linear"](
            self.save_docx(document), "SyntheticCatalog.docx",
        )
        catalog = next(doc for doc in docs if degree in doc.page_content)
        self.assertEqual(catalog.metadata.get("collection_heading"), "Programs Offered")
        self.assertNotIn("Academic Personnel", catalog.page_content)
        roster = next(doc for doc in docs if markdown_rows(doc.page_content))
        self.assertEqual(roster.metadata["section_heading"], "Academic Personnel")
        self.assertFalse(roster.metadata.get("collection_heading"))
        self.assertEqual(markdown_rows(roster.page_content), DEAN_ROWS)
        self.assert_no_index_work()

    def test_degree_headings_do_not_invent_an_implicit_catalog(self):
        document = docx.Document()
        for heading, text in (
            ("College of Coastal Studies", "Contact the office for research partnerships."),
            ("Bachelor of Science in Coastal Mapping", "Applicants must submit a portfolio."),
        ):
            document.add_heading(heading, level=2)
            document.add_paragraph(text)
        docs = self.ns["process_docx_linear"](
            self.save_docx(document), "SyntheticDepartment.docx",
        )
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(not doc.metadata.get("collection_heading") for doc in docs))
        self.assertNotIn("Applicants", docs[0].page_content)
        self.assertNotIn("research partnerships", docs[1].page_content)
        self.assert_no_index_work()

    def test_oversized_catalog_keeps_its_parent_in_every_continuation(self):
        heading = "Academic Programs"
        document = docx.Document()
        document.add_heading(heading, level=2)
        document.add_heading("College of Applied Studies", level=2)
        entries = [
            f"Bachelor of Science in Applied Studies {index:03d}, "
            f"Major in Research Pathway {index:03d}, with laboratory and field practice."
            for index in range(110)
        ]
        for entry in entries:
            document.add_paragraph(entry, style="List Paragraph")
        document.add_paragraph("ORGANIZATIONAL STRUCTURE", style="Normal")
        add_table(document, DEAN_ROWS)
        docs = self.ns["process_docx_linear"](
            self.save_docx(document), "LargeSyntheticCatalog.docx",
        )
        chunks = self.ns["_chunk_and_dedup"](docs)
        catalogs = [chunk for chunk in chunks
                    if chunk.metadata.get("collection_heading") == heading]
        self.assertGreater(len(catalogs), 1, "Fixture must exceed the real chunk budget")
        for chunk in catalogs:
            self.assertEqual(chunk.metadata["section_heading"], heading)
            self.assertIn(heading, chunk.page_content)
            self.assertNotIn("DEAN ALPHA", chunk.page_content)
            self.assertNotIn("ORGANIZATIONAL STRUCTURE", chunk.page_content)
        combined = "\n".join(chunk.page_content for chunk in catalogs)
        positions = []
        for entry in entries:
            self.assertIn(entry, combined, "A continuation lost a program or its major")
            positions.append(combined.index(entry))
        self.assertEqual(positions, sorted(positions))
        for chunk in chunks:
            if any(entry in chunk.page_content for entry in entries):
                self.assertEqual(chunk.metadata.get("collection_heading"), heading)
        self.assertEqual(len({chunk.metadata["chunk_id"] for chunk in catalogs}),
                         len(catalogs))
        self.assert_no_index_work()


class GenerationContextTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        resources.enter_context(offline_only())
        self.ns = {
            "__name__": "_catalog_context_under_test",
            "source_priority": Mock(return_value={}),
            "doc_key": Mock(side_effect=lambda source: source.rsplit("/", 1)[-1].lower()),
        }
        extract_definitions("rag/chain.py", ("docs_to_context",), self.ns)

    def test_metadata_only_heading_reaches_generation_for_each_existing_key(self):
        body = "Bachelor of Science in Coastal Mapping\nMajor in Survey Design"
        for key in ("section", "header", "section_heading", "collection_heading"):
            with self.subTest(key=key):
                heading = "Programs Offered"
                doc = Document(body, {"source": "SyntheticCatalog.docx", "page": 7,
                                      key: heading})
                context = self.ns["docs_to_context"]([doc])
                self.assertNotIn(heading, body)
                self.assertIn(heading, context)
                self.assertIn(body, context)
                self.assertIn("Source: SyntheticCatalog.docx", context)
                self.assertIn("Page: 7", context)
                self.assertNotIn("MOST RECENT SOURCE", context)
                self.assertNotIn("SUPERSEDED", context)
                self.assertEqual(doc.page_content, body)
                self.assertEqual(doc.metadata[key], heading)

    def test_all_distinct_metadata_headings_and_the_full_body_are_preserved(self):
        headings = {
            "section": "College of Coastal Studies",
            "header": "Undergraduate Pathways",
            "section_heading": "Programs Offered",
            "collection_heading": "Academic Programs",
        }
        body = "\n".join(
            f"Bachelor of Science in Subject {index:03d}, Major in Field {index:03d}."
            for index in range(150)
        )
        metadata = {"source": "LongCatalog.docx", **headings}
        context = self.ns["docs_to_context"]([Document(body, metadata)])
        for heading in headings.values():
            self.assertIn(heading, context)
        self.assertIn(body, context, "Generation context must not truncate the catalog")
        self.assertEqual(metadata, {"source": "LongCatalog.docx", **headings})

    def test_metadata_labels_preserve_date_based_priority_and_document_order(self):
        docs = [
            Document("Current catalog includes the Field Systems degree.", {
                "source": "archive/catalog.docx", "page": 3,
                "collection_heading": "Programs Offered",
            }),
            Document("Previous catalog lists the earlier Laboratory Studies degree.", {
                "source": "catalog-update-2099.pdf", "page": 8,
                "section_heading": "Former Academic Programs",
            }),
        ]
        dates = {"catalog.docx": "2026-09-01", "catalog-update-2099.pdf": "2024-06-01"}
        self.ns["source_priority"].return_value = {
            "catalog.docx": {"is_newest": True, "date": "2026-09-01"},
            "catalog-update-2099.pdf": {"is_newest": False, "date": "2024-06-01"},
        }
        context = self.ns["docs_to_context"](docs, dates=dates)
        recent, previous = context.split("\n\n---\n\n")
        self.assertIn("[DOCUMENT 1]", recent)
        self.assertIn("MOST RECENT SOURCE", recent)
        self.assertIn("effective 2026-09-01", recent)
        self.assertIn("Programs Offered", recent)
        self.assertNotIn("SUPERSEDED", recent)
        self.assertIn("[DOCUMENT 2]", previous)
        self.assertIn("SUPERSEDED", previous)
        self.assertIn("effective 2024-06-01", previous)
        self.assertIn("Former Academic Programs", previous)
        self.assertNotIn("MOST RECENT SOURCE", previous)
        for doc, part in zip(docs, (recent, previous)):
            self.assertIn(doc.page_content, part)
        priority_call = self.ns["source_priority"].call_args
        self.assertEqual(set(priority_call.args), {doc.metadata["source"] for doc in docs})
        self.assertIs(priority_call.kwargs["dates"], dates)
        self.assertEqual([call.args[0] for call in self.ns["doc_key"].call_args_list],
                         [doc.metadata["source"] for doc in docs])


class PDFTextFallbackTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        resources.enter_context(offline_only())
        self.ns = {
            "__name__": "_catalog_pdf_under_test", "re": re, "Document": Document,
            "logger": Mock(),
            "detect_columns": Mock(),
            "render_page_image": Mock(return_value=object()),
            "render_region_image": Mock(return_value=object()),
            "ocr_page_image": Mock(return_value=""),
            "ocr_column": Mock(return_value=""),
            "_extract_and_append_tables": Mock(side_effect=lambda page, text, *args: text),
        }
        extract_definitions("src/helper.py", ("clean_text",), self.ns)
        extract_definitions("store_index.py", ("process_pdf",), self.ns)

    def read_pdf(self, native, column_texts=None):
        # Every loader, page, image and OCR operation stays in memory.
        rect = object()
        columns = [object() for _ in column_texts] if column_texts else [rect]
        clipped = dict(zip(columns, column_texts or [native]))

        def get_text(kind, clip=None):
            if kind == "words":
                return [(0, 0, 1, 1, word, 0, 0, index)
                        for index, word in enumerate(native.split())]
            if kind == "text":
                return clipped[clip] if clip is not None else native
            raise AssertionError(f"Unexpected fake PDF extraction mode: {kind}")

        page = SimpleNamespace(rect=rect, get_text=Mock(side_effect=get_text))
        fdoc = MagicMock()
        fdoc.__len__.return_value = 1
        fdoc.load_page.return_value = page
        pdoc = SimpleNamespace(pages=[SimpleNamespace(extract_text=Mock(return_value=native))])
        self.ns["fitz"] = SimpleNamespace(open=Mock(return_value=nullcontext(fdoc)))
        self.ns["pdfplumber"] = SimpleNamespace(open=Mock(return_value=nullcontext(pdoc)))
        self.ns["detect_columns"].return_value = columns
        docs = self.ns["process_pdf"]("not-a-real-file.pdf", "SyntheticCatalog.PDF")
        self.ns["fitz"].open.assert_called_once_with("not-a-real-file.pdf")
        self.ns["pdfplumber"].open.assert_called_once_with("not-a-real-file.pdf")
        for doc in docs:
            self.assertEqual(doc.metadata["source"], "SyntheticCatalog.PDF")
            self.assertEqual(doc.metadata["page"], 1)
        return docs

    def test_readable_native_text_skips_page_and_column_ocr(self):
        native = (
            "Programs offered include coastal mapping and digital systems. "
            "Students can select majors in survey design or community learning. "
            "The graduate school offers research degrees with supervised field work. "
            "Technical training covers equipment servicing and digital fabrication. "
            "Basic education includes elementary classes and secondary school programs."
        )
        docs = self.read_pdf(native)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, native)
        self.assertEqual(docs[0].metadata["type"], "pdf")
        for effect in ("ocr_page_image", "ocr_column", "render_page_image", "render_region_image"):
            self.ns[effect].assert_not_called()
        self.ns["_extract_and_append_tables"].assert_called_once()

    def test_blank_page_ocr_retains_native_text_and_native_type(self):
        native = "Bachelor of Science in Coastal Mapping with a major in Survey Design."
        for blank in ("", " \n\t "):
            with self.subTest(blank=repr(blank)):
                self.ns["ocr_page_image"].reset_mock()
                self.ns["ocr_page_image"].return_value = blank
                docs = self.read_pdf(native)
                self.assertEqual(len(docs), 1, "Blank OCR must not erase a native page")
                self.assertEqual(docs[0].page_content, native)
                self.assertEqual(docs[0].metadata["type"], "pdf")
                self.ns["ocr_page_image"].assert_called_once()

    def test_failed_page_ocr_retains_native_text_and_native_type(self):
        native = "Certificate in Equipment Servicing with supervised workshop practice."
        self.ns["ocr_page_image"].side_effect = RuntimeError("Synthetic OCR failure")
        docs = self.read_pdf(native)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, native)
        self.assertEqual(docs[0].metadata["type"], "pdf")
        self.ns["ocr_page_image"].assert_called_once()

    def test_successful_page_ocr_uses_the_transcript_and_ocr_type(self):
        transcript = "Programs offered include a Bachelor of Science in Field Systems."
        self.ns["ocr_page_image"].return_value = transcript
        docs = self.read_pdf("Partial native heading")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, transcript)
        self.assertEqual(docs[0].metadata["type"], "pdf_ocr")
        self.ns["ocr_page_image"].assert_called_once()
        self.ns["_extract_and_append_tables"].assert_not_called()

    def test_partial_column_ocr_never_discards_the_native_fallback(self):
        columns = [
            "Bachelor of Science in Coastal Mapping with a major in Survey Design.",
            "Certificate in Digital Fabrication and Elementary Education Program.",
        ]
        for failed_column in (0, 1):
            for failure in ("", RuntimeError("Synthetic column OCR failure")):
                with self.subTest(failed_column=failed_column, failure=repr(failure)):
                    outcomes = list(columns)
                    outcomes[failed_column] = failure
                    self.ns["ocr_column"].reset_mock()
                    self.ns["ocr_column"].side_effect = outcomes
                    docs = self.read_pdf("\n\n".join(columns), column_texts=columns)
                    self.assertEqual(len(docs), 1)
                    for native_column in columns:
                        self.assertIn(native_column, docs[0].page_content)
                    self.assertEqual(self.ns["ocr_column"].call_count, 2)
                    self.ns["ocr_page_image"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
