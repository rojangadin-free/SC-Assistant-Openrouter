"""Check that the inline-style refactor did not break the template.

Three things can go wrong when style attributes are rewritten into class
attributes, and all three are silent in a browser:

  1. Two class attributes on one tag - the second is ignored, so half the
     styling vanishes with no error anywhere.
  2. A mangled tag, e.g. a lost quote, which turns markup into text.
  3. Broken JavaScript, because most of these attributes live inside JS string
     concatenation and a bad edit there breaks a whole admin section.

This checks 1 and 2 directly and extracts the inline script so `node --check`
can rule out 3.

    python tools/verify_admin_styles.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "sc_assistant" / "templates" / "dashboard.html"
OUT = ROOT / "_inline_dashboard.js"

text = TPL.read_text(encoding="utf-8")
problems = 0

# 1. Duplicate class attributes on a single tag.
#    [^<>]* keeps the match inside one tag, so a class= in a later tag can't
#    pair with an earlier one and raise a false alarm.
dupes = list(re.finditer(r'<[A-Za-z][^<>]*?class="[^"]*"[^<>]*?class="', text))
print("duplicate class attributes : %d" % len(dupes))
for m in dupes[:10]:
    line = text.count("\n", 0, m.start()) + 1
    print("   line %d: %s" % (line, m.group(0)[:100].replace("\n", " ")))
problems += len(dupes)

# 2. Leftover empty or malformed attributes from a bad splice.
for pattern, label in (
    (r'style=""', 'empty style=""'),
    (r'class=""', 'empty class=""'),
    (r'class="\s', 'class starting with whitespace'),
    # Only a *mid-line* run of spaces indicates a bad splice. A newline plus
    # indentation before class= is just an attribute on its own line, which is
    # what a replaced multi-line style attribute legitimately leaves behind.
    (r'[^\s]  +class=', 'double space before class'),

    (r'""[A-Za-z]', 'quote run-on'),
):
    hits = len(re.findall(pattern, text))
    if hits:
        print("%-27s: %d" % (label, hits))
        problems += hits

# 3. Extract the page's own inline script for node --check.
#    The block after the dashboard.js include is the one the refactor touched.
start = text.index("<script>", text.index("js/dashboard.js"))
end = text.rindex("</script>")
OUT.write_text(text[start + len("<script>"):end], encoding="utf-8")
print("inline script extracted    : %d chars -> %s" % (end - start, OUT.name))

# A quick sanity figure so a silently-empty run is obvious.
print("remaining inline styles    : %d" % text.count('style="'))
print("class attributes           : %d" % text.count('class="'))

print("\n%s" % ("FAIL: %d problem(s)" % problems if problems else "STRUCTURE OK"))
sys.exit(1 if problems else 0)
