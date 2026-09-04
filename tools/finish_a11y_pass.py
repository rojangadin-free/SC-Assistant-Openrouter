"""
Close the last two source-level accessibility gaps the audit found.

1. auth.css semantic tokens
   `--success: #16a34a` and `--error: #ef4444` are used two ways: as a fill
   behind white text (the step indicator) and as ink on a tinted panel (the
   notification icon). Both directions failed — white on #16a34a is 3.13:1, and
   #ef4444 on its own #fee2e2 tint is 3.08:1. polish.css was repainting the
   notification icons, but a token used in two directions has to be fixed at the
   definition or the next component that uses it inherits the failure.

   The replacements are the same values polish.css already measured, so light
   mode does not shift: #12783c gives white 5.63:1 and reads 5.13:1 on the green
   tint; #c2262c gives white 5.82:1 and 4.76:1 on the red tint.

   The 10px checkmark glyph goes to 12px here too — polish.css was already
   overriding it, and leaving 10px in the source is the same two-values-for-one
   problem.

2. Unlabelled inputs (WCAG 2.2 §3.3.2 / §1.1.1)
   Thirteen inputs have no label, no aria-label and no aria-labelledby. Six of
   them are the verification-code boxes: a screen reader announces "edit, blank"
   six times with nothing to distinguish them, so a student cannot tell which
   digit they are on. The rest are date and text fields in the admin forms where
   a placeholder was doing the label's job — and a placeholder disappears as
   soon as the field has content, which is exactly when a user re-reading the
   form needs it.

   These get aria-label rather than a visible <label> because each one already
   sits under a visible caption in the layout; adding a second visible label
   would duplicate text on screen. aria-label gives the accessible name without
   changing the design.

Idempotent: a tag that already has an aria-label is skipped. Run with --check to
report without writing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "sc_assistant"

# --- 1. auth.css -----------------------------------------------------------
# Exact-string swaps. Narrow on purpose: `--success:` only ever appears once as
# a definition, and matching the whole declaration avoids touching a usage.
CSS_EDITS = [
    ("auth.css",
     "--success: #16a34a;",
     "--success: #12783c;"),   # white on it 3.13:1 -> 5.63:1
    ("auth.css",
     "--error: #ef4444;",
     "--error: #c2262c;"),     # on its own #fee2e2 tint 3.08:1 -> 4.76:1
    ("auth.css",
     ".custom-checkbox i { color: white; font-size: 10px; display: none; }",
     ".custom-checkbox i { color: white; font-size: 12px; display: none; }"),
]

# --- 2. accessible names ---------------------------------------------------
# Matched on the attribute that already identifies the field, so the patch does
# not depend on line numbers that shift the moment anyone edits the template.
LABELS = {
    "auth.html": [
        (r'data-index="0"', "Verification code, digit 1 of 6"),
        (r'data-index="1"', "Verification code, digit 2 of 6"),
        (r'data-index="2"', "Verification code, digit 3 of 6"),
        (r'data-index="3"', "Verification code, digit 4 of 6"),
        (r'data-index="4"', "Verification code, digit 5 of 6"),
        (r'data-index="5"', "Verification code, digit 6 of 6"),
    ],
    "dashboard.html": [
        (r'id="calPreviewQ"',    "Test question for calendar preview"),
        (r'id="calPreviewDate"', "Date to preview the answer for"),
        (r'id="annAsOf"',        "Show announcements active on this date"),
        (r'id="annTitle"',       "Announcement title"),
        (r'id="annStart"',       "Announcement start date"),
        (r'id="annEnd"',         "Announcement end date"),
        (r'id="annPinned"',      "Pin this announcement to the top"),
        # Second pass. The static scan only looks at <input>, so every <select>
        # and <textarea> on the page went unreported — including the composer,
        # which is the most used control in the app. These came out of a rendered
        # check that asked the same question the browser's a11y tree asks: does
        # this control have an accessible name from ANY source?
        (r'id="gapStatusFilter"',      "Filter content gaps by status"),
        (r'id="escStatusFilter"',      "Filter escalations by status"),
        (r'id="calAsOf"',              "Show calendar entries active on this date"),
        (r'id="calLabel"',             "Calendar entry label"),
        (r'id="calStart"',             "Calendar entry start date"),
        (r'id="calEnd"',               "Calendar entry end date"),
        (r'id="calNote"',              "Calendar entry note"),
        (r'id="calScanText"',          "Paste calendar text to scan"),
        (r'id="calScanYear"',          "Academic year for the scan"),
        (r'id="annBody"',              "Announcement body"),
        (r'id="annPriority"',          "Announcement priority"),
        (r'id="annAudience"',          "Who this announcement is for"),
        (r'id="annPreviewAudience"',   "Preview as this audience"),
        (r'id="annPreviewDate"',       "Preview announcements for this date"),
    ],
    "chat.html": [
        (r'id="messageInput"',   "Type your question"),
        (r'id="imageInput"',     "Attach an image"),
        (r'id="escalateRoute"',  "Send this question to"),
        (r'id="escalateNote"',   "Extra details for the staff member"),
    ],
}


FIELD_TAG = re.compile(r"<(input|select|textarea)\b[^>]*>", re.IGNORECASE | re.DOTALL)


def add_labels(text: str, rules: list[tuple[str, str]]) -> tuple[str, int]:
    """Insert aria-label into the form control that carries each marker."""
    added = 0
    for marker, label in rules:
        # Match the whole opening tag containing the marker, so the insert cannot
        # land inside a neighbouring element or in the middle of an attribute.
        for m in list(FIELD_TAG.finditer(text)):
            tag = m.group(0)
            if not re.search(marker, tag):
                continue
            if "aria-label" in tag:
                break  # already named — nothing to do
            name = m.group(1)
            new_tag = tag.replace(f"<{name}", f'<{name} aria-label="{label}"', 1)
            text = text[:m.start()] + new_tag + text[m.end():]
            added += 1
            break
    return text, added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only")
    args = ap.parse_args()

    total_css = total_aria = 0

    for filename, before, after in CSS_EDITS:
        path = ROOT / "static" / "css" / filename
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8")
        if before not in text:
            continue
        text = text.replace(before, after, 1)
        total_css += 1
        print(f"{filename}: {before.strip()[:52]} -> {after.strip()[:52]}")
        if not args.check:
            path.write_text(text, encoding="utf-8")

    for filename, rules in LABELS.items():
        path = ROOT / "templates" / filename
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8")
        text, added = add_labels(text, rules)
        if not added:
            continue
        total_aria += added
        print(f"{filename}: {added} input(s) named")
        if not args.check:
            path.write_text(text, encoding="utf-8")

    verb = "would apply" if args.check else "applied"
    print(f"\n{verb} {total_css} CSS token fix(es) and {total_aria} aria-label(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
