"""Remove the explanatory prose that opened six admin dashboard sections.

Data Conflicts, Content Gaps, Answer Quality, Ask a Human, Academic Calendar and
Announcements each opened with an accented card carrying a paragraph that
explained what the section is for. That is onboarding copy: useful the first
time, then it is a wall of text between the admin and the queue they came to
work, on every visit.

The important detail — and the reason the first version of this script removed
nothing — is that those cards are NOT prose-only. The Data Conflicts card also
holds the "Scan documents" button (#rescanConflictsBtn) and its status line; the
Content Gaps card holds a row of live figures. Deleting the card would have taken
a working control with it. So the operation is narrower than it looks:

  1. remove the top-level <p class="sc-lead"> paragraph inside the card;
  2. remove the now-empty card ONLY if nothing but whitespace remains.

Deliberately untouched:

  * <p class="sc-lead sc-lead--sm"> field helpers ("Paste the academic-calendar
    section of the handbook") — they tell someone what to type into the input
    directly below them, so removing them leaves a bare textarea.
  * Overview panel captions, which label a live list rather than explain a page.

Idempotent: a second run finds nothing and writes nothing.

    python tools/remove_section_intros.py           # apply
    python tools/remove_section_intros.py --check   # report only, no write
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "sc_assistant" / "templates" / "dashboard.html"

# The accent card that opens a section. .sc-accent-* is also on the Overview KPI
# tiles, but those are .stat-card / .analytics-kpi and cannot match this.
OPEN = re.compile(r'[ \t]*<div class="sc-card sc-mb sc-accent-[a-z]+">[ \t]*\r?\n')

# The section blurb, and only it: `class="sc-lead"` exactly, so the
# `sc-lead sc-lead--sm` field helpers are invisible to this pattern. <p> is not
# nested in this template, so matching to the first </p> is safe.
LEAD = re.compile(
    r'[ \t]*<p class="sc-lead">.*?</p>[ \t]*\r?\n',
    re.DOTALL,
)

TAG = re.compile(r"</?div\b")
COMMENT = re.compile(r"<!--.*?-->|\{#.*?#\}", re.DOTALL)


def find_block_end(text: str, start: int) -> int:
    """Index just past the </div> closing the block that opens at `start`.

    Counts div tags so nested cards cannot fool it — the alternative, matching
    to the first </div>, truncates the block mid-card.
    """
    depth = 0
    for m in TAG.finditer(text, start):
        depth += -1 if m.group().startswith("</") else 1
        if depth == 0:
            gt = text.find(">", m.end())
            if gt == -1:
                return -1
            nl = text.find("\n", gt)
            return len(text) if nl == -1 else nl + 1
    return -1


def ascii_safe(s: str) -> str:
    """Windows consoles are cp1252; the copy contains emoji. Do not die for it."""
    return s.encode("ascii", "replace").decode("ascii")


def preview(block: str) -> str:
    txt = COMMENT.sub(" ", block)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return ascii_safe(txt[:70] + ("..." if len(txt) > 70 else ""))


def card_is_now_empty(block: str) -> bool:
    """True when the card holds nothing but its own div tags and whitespace."""
    inner = block.strip()
    inner = inner[inner.find(">") + 1:]              # drop the opening <div ...>
    inner = inner[:inner.rfind("</div>")]            # drop the closing </div>
    inner = COMMENT.sub("", inner)
    return inner.strip() == ""


def main() -> int:
    check_only = "--check" in sys.argv
    text = TEMPLATE.read_text(encoding="utf-8")

    out = text
    pos = 0
    paragraphs = 0
    cards_dropped = 0
    report: list[str] = []

    while True:
        m = OPEN.search(out, pos)
        if not m:
            break
        end = find_block_end(out, m.start())
        if end == -1:
            report.append("  ! unterminated card, left alone")
            pos = m.end()
            continue

        block = out[m.start():end]
        stripped, hits = LEAD.subn("", block)

        if not hits:
            report.append(f"  = no section blurb: {preview(block)}")
            pos = end
            continue

        paragraphs += hits
        gone = preview(block[:block.find("</p>")]) if "</p>" in block else preview(block)

        if card_is_now_empty(stripped):
            report.append(f"  - card removed (prose only): {gone}")
            out = out[:m.start()] + out[end:]
            cards_dropped += 1
            pos = m.start()
        else:
            kept = re.findall(r'id="([^"]+)"', stripped)
            note = f" (kept: {', '.join(kept)})" if kept else " (kept the card's controls)"
            report.append(f"  - blurb removed{note}: {gone}")
            out = out[:m.start()] + stripped + out[end:]
            pos = m.start() + len(stripped)

    print(f"{TEMPLATE.relative_to(ROOT)}")
    print(f"  section blurbs removed: {paragraphs}")
    print(f"  cards removed entirely: {cards_dropped}")
    for line in report:
        print(line)

    survivors = len(re.findall(r'class="sc-lead sc-lead--sm"', out))
    print(f"  field-helper paragraphs still present: {survivors}")

    if check_only:
        print("\ncheck only, nothing written")
        return 0

    if out != text:
        TEMPLATE.write_text(out, encoding="utf-8")
        print(f"  lines removed: {text.count(chr(10)) - out.count(chr(10))}")
    else:
        print("  unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
