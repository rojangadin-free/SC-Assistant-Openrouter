"""Bounded, text-only DOCX extraction for an autoescaped browser preview."""

import re
from io import BytesIO
from zipfile import ZipFile

from docx import Document
from docx.table import _Cell
from docx.text.paragraph import Paragraph

MAX_DOCX_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
MAX_PREVIEW_ITEMS = 10000
MAX_PREVIEW_TEXT = 1_000_000


def build_docx_preview(data):
    """Return plain text/structure, never document-supplied HTML or resources.

    OverflowError means the original remains downloadable but is too expensive
    to preview. ValueError means it is not a readable Word document.
    """
    if len(data) > MAX_DOCX_BYTES:
        raise OverflowError("Document exceeds the preview size limit")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if (len(entries) > MAX_ARCHIVE_ENTRIES
                    or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES):
                raise OverflowError("Document exceeds the expanded size limit")
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("Encrypted documents cannot be previewed")

        document = Document(BytesIO(data))
        item_count = 0
        text_count = 0

        def extract(container, depth=0):
            nonlocal item_count, text_count
            if depth > 8:
                raise OverflowError("Document tables are too deeply nested")
            blocks = []
            for item in container.iter_inner_content():
                item_count += 1
                if item_count > MAX_PREVIEW_ITEMS:
                    raise OverflowError("Document has too many preview items")
                if isinstance(item, Paragraph):
                    text = item.text
                    text_count += len(text)
                    if text_count > MAX_PREVIEW_TEXT:
                        raise OverflowError("Document has too much preview text")
                    if not text.strip():
                        continue
                    style = item.style.style_id if item.style else ""
                    heading = re.fullmatch(r"Heading([1-9])", style, re.IGNORECASE)
                    if heading or style == "Title":
                        level = min(int(heading[1]) + 1, 6) if heading else 2
                        blocks.append({"kind": "heading", "level": level, "text": text})
                    elif style.startswith(("ListBullet", "ListNumber")):
                        ordered = style.startswith("ListNumber")
                        if not blocks or blocks[-1].get("list_style") != style:
                            blocks.append({"kind": "list", "ordered": ordered,
                                           "list_style": style, "items": []})
                        blocks[-1]["items"].append(text)
                    else:
                        blocks.append({"kind": "paragraph", "text": text})
                else:
                    rows = []
                    for row in item.rows:
                        cells = []
                        # Physical cells avoid duplicating merged text, and avoid
                        # allocating a huge logical grid from an untrusted span.
                        for tc in row._tr.tc_lst:
                            item_count += 1
                            span = tc.grid_span
                            if item_count > MAX_PREVIEW_ITEMS or not 1 <= span <= 100:
                                raise OverflowError("Document table exceeds preview limits")
                            cells.append({"colspan": span,
                                          "blocks": extract(_Cell(tc, item), depth + 1)})
                        if cells:
                            rows.append(cells)
                    if rows:
                        blocks.append({"kind": "table", "rows": rows})
            return blocks

        return extract(document)
    except OverflowError:
        raise
    except Exception as exc:
        raise ValueError("This file is not a readable DOCX document") from exc
