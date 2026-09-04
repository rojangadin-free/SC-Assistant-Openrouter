"""
Retire the literal hex colours that polish.css was overriding.

WHY THIS EXISTS
polish.css raised 68 failing text/background pairs to WCAG AA by re-declaring
them last in the cascade. That works in the browser — measured 2.41:1 -> 4.83:1
on the chat page, 8.15:1 in dark mode — but it leaves two values for one colour:
the failing literal in chat.css and the passing token in polish.css. Anyone
editing chat.css reads `#9ca3af` and has no way to know it never renders.

So this points the SOURCE declarations at the same tokens. After it runs,
polish.css's contrast block is belt-and-braces rather than the only thing
holding the ratios up, and a static reader of any single file sees the value
that actually renders.

WHAT IT DELIBERATELY DOES NOT DO
  * It only rewrites declarations whose property is exactly `color`. The same
    golds and greens are used as backgrounds and borders, where they are fine —
    #fbbc05 fails as ink on white and is perfectly good as a fill. A blind
    global hex replace would flatten the brand.
  * It never touches a custom-property definition (`--text-tertiary: #9ca3af`).
    Those are the theme's own vocabulary; changing them would move colours on
    surfaces this script has not measured.
  * The `background` rewrites are named individually, because each one is a
    fill that carries white text and was measured failing.
  * #fbbc05 is NOT in the map, even though the audit called it the worst pair
    in the app (1.62:1). Two of the places gold is used as ink sit on the solid
    #0d4503 green, where the bright gold is already ~7.5:1 — darkening it drops
    that pair to 2.11:1, so the "fix" would be the regression. Gold is handled
    by selector in polish.css instead, against the surface each instance is
    really on. That distinction is only visible in a browser; a file scanner
    assumes the page background and gets it backwards.

Idempotent: a second run finds nothing because the literals are gone. Run with
--check to report without writing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "sc_assistant"
CSS_DIR = ROOT / "static" / "css"

# The templates and the jQuery that builds rows write `style="color:#ef4444"`
# straight into the markup, and an inline style outranks every stylesheet — so
# the contrast block in polish.css could never reach them. A custom property is
# legal inside a style attribute, which is what makes this fixable without
# resorting to !important.
MARKUP_DIRS = [ROOT / "templates", ROOT / "static" / "js"]
MARKUP_GLOBS = ("*.html", "*.js")

# Literal -> token, for `color:` declarations only. Each token is defined in
# polish.css with its measured ratio and a dark-mode counterpart, which is the
# reason to route through a variable instead of writing a darker hex here: a
# grey dark enough for #f9f9f9 is invisible on #0f1115.
INK = {
    "#9ca3af": "var(--ui-muted)",        # 2.41:1 -> 4.83:1  (24 occurrences)
    "#ef4444": "var(--ui-danger-ink)",   # 3.57:1 -> 5.10:1
    "#dc2626": "var(--ui-danger-ink)",   # 3.95:1 -> 5.10:1
    "#22c55e": "var(--ui-success-ink)",  # 2.16:1 -> 5.03:1
    "#16a34a": "var(--ui-success-ink)",  # 3.13:1 -> 5.03:1
    "#f59e0b": "var(--ui-warn-ink)",     # 2.04:1 -> 4.62:1
    "#d97706": "var(--ui-warn-ink)",     # 2.80:1 -> 4.62:1
    "#3b82f6": "var(--ui-info-ink)",     # 3.49:1 -> 6.31:1
    "#d946ef": "var(--ui-code-ink)",     # 3.28:1 -> 5.32:1  (inline code)
}

# Fills that carry white text. Named one by one, with the measured ratio of
# white ON the fill, because the fix here is to darken the background rather
# than the text — the text is already #fff and cannot go lighter.
FILLS = [
    ("admin-components.css", "#22c55e", "var(--ui-fill-success)"),  # white 2.28:1 -> 5.08:1
    ("admin-components.css", "#94a3b8", "var(--ui-fill-grey)"),     # white 2.56:1 -> 5.43:1
    ("admin-components.css", "#3b82f6", "var(--ui-fill-info)"),     # white 3.68:1 -> 6.31:1
    ("admin-components.css", "#ef4444", "var(--ui-fill-danger)"),   # white 3.76:1 -> 5.10:1
    ("admin-components.css", "#8b5cf6", "var(--ui-fill-violet)"),   # white 4.23:1 -> 6.55:1
]

# `color` declarations only, and never a `--custom-property:` definition.
COLOR_DECL = re.compile(
    r"(?<!-)\bcolor\s*:\s*(#[0-9a-fA-F]{6})\b",
)
VAR_DEF_LINE = re.compile(r"^\s*--[\w-]+\s*:")


def rewrite_ink(text: str) -> tuple[str, dict[str, int]]:
    """Rewrite `color: <hex>` on lines that are not variable definitions."""
    counts: dict[str, int] = {}
    out_lines = []

    for line in text.splitlines(keepends=True):
        # A line like `  --ui-muted: #6b7280;` is the vocabulary itself. Leave it.
        if VAR_DEF_LINE.match(line):
            out_lines.append(line)
            continue

        def sub(m: re.Match) -> str:
            hexv = m.group(1).lower()
            token = INK.get(hexv)
            if not token:
                return m.group(0)
            counts[hexv] = counts.get(hexv, 0) + 1
            return m.group(0).replace(m.group(1), token)

        out_lines.append(COLOR_DECL.sub(sub, line))

    return "".join(out_lines), counts


def rewrite_fills(filename: str, text: str) -> tuple[str, int]:
    """Darken the handful of named fills that carry white text."""
    n = 0
    for target_file, hexv, token in FILLS:
        if target_file != filename:
            continue
        # Only in a `background` / `background-color` declaration, so the same
        # hex used as a border or an accent stripe is left alone.
        pattern = re.compile(r"(background(?:-color)?\s*:\s*)" + re.escape(hexv) + r"\b",
                             re.IGNORECASE)
        text, k = pattern.subn(lambda m: m.group(1) + token, text)
        n += k
    return text, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args()

    if not CSS_DIR.is_dir():
        print(f"CSS directory not found: {CSS_DIR}", file=sys.stderr)
        return 2

    total = 0
    targets = [p for p in sorted(CSS_DIR.glob("*.css"))]
    for d in MARKUP_DIRS:
        if d.is_dir():
            for g in MARKUP_GLOBS:
                targets += sorted(d.glob(g))

    for path in targets:
        # polish.css is where the tokens are declared; it has no literals left
        # to retire and rewriting it would turn a definition into a self-reference.
        if path.name == "polish.css":
            continue

        original = path.read_text(encoding="utf-8")
        text, counts = rewrite_ink(original)
        text, fills = rewrite_fills(path.name, text) if path.suffix == ".css" else (text, 0)

        changed = sum(counts.values()) + fills
        if not changed:
            continue

        total += changed
        detail = ", ".join(f"{h}x{n}" for h, n in sorted(counts.items()))
        if fills:
            detail += f", {fills} fill(s)"
        print(f"{path.name}: {changed} declaration(s) -> tokens  [{detail}]")

        if not args.check:
            path.write_text(text, encoding="utf-8")

    verb = "would rewrite" if args.check else "rewrote"
    print(f"\n{verb} {total} colour declaration(s) across css/, templates/ and js/")
    if total and not args.check:
        print("Tokens are defined in polish.css; it stays last in the cascade so "
              "the dark-mode counterparts still apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
