"""Remove the per-section "Back to Overview" buttons from the admin dashboard.

Every one of the ten dashboard sections opened with its own back button, which
duplicated navigation the sidebar already provides: the "Overview" item
(#analyticsMenuItem) is permanently visible on desktop and one tap away in the
drawer on mobile, so the in-page button was a second route to a place the user
could already reach. Removing it also removes ten chances for the two routes to
disagree about which section is current.

Done as a script rather than by hand because it is the same four-line block
repeated ten times in a very large template — the sweep is easier to audit, and
trivially repeatable, in this form. Matches the existing tools/ convention
(refactor_admin_styles.py, retire_hardcoded_colors.py).

Idempotent: a second run reports 0 removals and rewrites nothing.

    python tools/remove_back_buttons.py          # apply
    python tools/remove_back_buttons.py --check  # report only, exit 1 if any remain
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "sc_assistant" / "templates" / "dashboard.html"
SCRIPT = ROOT / "sc_assistant" / "static" / "js" / "dashboard.js"

# The whole <button> element, with its leading indentation and trailing newline
# so no blank line is left behind. Guarded on "Back to Overview" in the label:
# .back-button is a shared class and this must not take an unrelated control.
BUTTON = re.compile(
    r"[ \t]*<button[^>]*class=\"back-button\"[^>]*>"   # opening tag
    r"(?:(?!</button>).)*?"                            # never cross the close tag
    r"Back to Overview"                                # the label, as a guard
    r"(?:(?!</button>).)*?"
    r"</button>[ \t]*\r?\n",
    re.DOTALL,
)

# Dead handlers left behind in dashboard.js. jQuery on an empty set is a no-op,
# so these are inert either way — but a handler bound to an id that no longer
# exists in the markup is a trap for the next reader. Each pattern is anchored
# tightly enough that it cannot match anything else, and a pattern that does not
# match is reported rather than forced.
JS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "const #backToAnalytics",
        re.compile(r"[ \t]*const backToAnalytics = \$\('#backToAnalytics'\);[ \t]*\r?\n"),
    ),
    (
        "const #backToAnalyticsFromUsers",
        re.compile(
            r"[ \t]*const backToAnalyticsFromUsers = "
            r"\$\('#backToAnalyticsFromUsers'\);[^\r\n]*\r?\n"
        ),
    ),
    (
        "// Back button click handler",
        re.compile(r"[ \t]*// Back button click handler[ \t]*\r?\n"),
    ),
    (
        "backToAnalytics.on(click)",
        re.compile(
            r"[ \t]*backToAnalytics\.on\('click', function\(\) \{[ \t]*\r?\n"
            r"[ \t]*showSection\('analytics'\);[ \t]*\r?\n"
            r"[ \t]*\}\);[ \t]*\r?\n"
        ),
    ),
    (
        "backToAnalyticsFromUsers.on(click)",
        re.compile(
            r"[ \t]*backToAnalyticsFromUsers\.on\('click', function\(\) \{"
            r"[^\r\n]*\r?\n"
            r"[ \t]*showSection\('analytics'\);[ \t]*\r?\n"
            r"[ \t]*\}\);[ \t]*\r?\n"
        ),
    ),
]


# One comment in the template's inline script explained the screen-restore rule
# by naming the button as the way out of Uploads. The button is gone and the
# sidebar is the way out now; a comment citing a control that no longer exists
# sends the next reader hunting for it. Only the clause naming the control is
# rewritten — the reasoning after the dash is still true.
STALE_COMMENT = re.compile(
    r'// leaving Uploads via "Back to Overview" and then reloading'
)
STALE_COMMENT_FIX = "// leaving Uploads via the sidebar and then reloading"


def ids_in(text: str) -> list[str]:
    """The id of every back button present, for the report."""

    return re.findall(r"<button[^>]*class=\"back-button\"[^>]*id=\"([^\"]+)\"", text)


def main() -> int:
    check_only = "--check" in sys.argv

    html = TEMPLATE.read_text(encoding="utf-8")
    present = ids_in(html)
    stripped, removed = BUTTON.subn("", html)

    print(f"{TEMPLATE.relative_to(ROOT)}")
    print(f"  back buttons found:   {len(present)}")
    for name in present:
        print(f"    - #{name}")
    print(f"  blocks removed:       {removed}")

    if check_only:
        remaining = len(ids_in(stripped if removed else html))
        print(f"\ncheck: {remaining} remaining")
        return 1 if remaining else 0

    stripped, comments = STALE_COMMENT.subn(STALE_COMMENT_FIX, stripped)
    print(f"  stale comments reworded: {comments}")

    if removed or comments:
        TEMPLATE.write_text(stripped, encoding="utf-8")


    # Verify nothing was left half-removed: no orphan label, no orphan class.
    after = TEMPLATE.read_text(encoding="utf-8")
    leftovers = {
        'class="back-button"': after.count('class="back-button"'),
        "Back to Overview": after.count("Back to Overview"),
    }
    print("  leftovers in template:")
    for needle, count in leftovers.items():
        print(f"    {needle!r}: {count}")

    js = SCRIPT.read_text(encoding="utf-8")
    js_out = js
    print(f"\n{SCRIPT.relative_to(ROOT)}")
    for label, pattern in JS_PATTERNS:
        js_out, hits = pattern.subn("", js_out)
        status = f"removed {hits}" if hits else "not present (left alone)"
        print(f"  {label}: {status}")

    if js_out != js:
        SCRIPT.write_text(js_out, encoding="utf-8")
        delta = js.count("\n") - js_out.count("\n")
        print(f"  dashboard.js: {delta} lines removed")
    else:
        print("  dashboard.js: unchanged")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
