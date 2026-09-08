"""Prove an admin setting survives the process that created it.

"It goes back to empty when I redeploy" cannot be reproduced by reading code —
the file backend looks identical to the DynamoDB one until the container is
replaced. So this simulates the redeploy: write a period/announcement in one
process, then re-exec a *fresh* interpreter with the local JSON files renamed out
of the way, and see whether the value is still there.

Renaming the files is what makes this a real test. A second process on the same
machine would find `calendar_periods.json` sitting next to it and pass even on the
file backend, which is exactly the false negative that let this bug ship. A new
container has no such file, and neither does the child process here.

    python tools/verify_settings_persist.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import json
import os
import subprocess
import sys
import uuid

# `.env` holds STORE_BACKEND, and `rag/store.py` reads it from the environment
# rather than from config.py — so a script that never imports config sees the
# default ("file") and would report the bug as still present on a machine that
# has already fixed it. importing config for its side effect is how every other
# tool here picks up .env.
import config  # noqa: F401,E402  (imported for its load_dotenv side effect)

MARKER = f"persist-probe-{uuid.uuid4().hex[:8]}"

# The local files a "redeploy" would take with it.
LOCAL_FILES = [
    "announcements.json",
    "calendar_periods.json",
    "conflict_resolutions.json",
    "doc_freshness.json",
    "escalations.json",
    "index_jobs.json",
    "answer_feedback.json",
]

CHILD = r"""
import _bootstrap  # noqa: F401
import config  # noqa: F401  (loads .env, incl. STORE_BACKEND)
import json, sys

from rag import announcements, calendar
marker = sys.argv[1]
found_period = any(p.get("label") == marker for p in calendar.list_periods())
found_ann = any(a.get("title") == marker for a in announcements.list_announcements())
print(json.dumps({"period": found_period, "announcement": found_ann}))
"""


passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


def main():
    backend = (os.getenv("STORE_BACKEND") or "file").strip().lower()
    print(f"  backend: {backend}")
    if backend != "dynamodb":
        print("  (on 'file' this SHOULD fail — that is the bug being tested for)")

    from rag import announcements, calendar

    # 1. Write, the way the admin dashboard writes.
    saved_period = calendar.set_period(
        MARKER,
        "2026-06-01",
        "2026-06-15",
        note="Persistence probe",
        set_by="verify_settings_persist.py",
    )
    saved_ann = announcements.save_announcement(
        MARKER,
        "Written by tools/verify_settings_persist.py — safe to delete.",
        posted_by="verify_settings_persist.py",
    )
    check("period written in this process", saved_period is not None, saved_period)
    check("announcement written in this process", saved_ann is not None)

    # 2. Hide the local files, then read from a brand-new interpreter. This is
    #    the redeploy: same code, same env, no writable layer from last time.
    renamed = []
    for name in LOCAL_FILES:
        if os.path.exists(name):
            hidden = f"{name}.persistprobe"
            os.replace(name, hidden)
            renamed.append((hidden, name))

    try:
        child = os.path.join("tools", "_persist_child.py")
        with open(child, "w", encoding="utf-8") as fh:
            fh.write(CHILD)
        try:
            out = subprocess.run(
                [sys.executable, child, MARKER],
                capture_output=True, text=True, timeout=120,
            )
            try:
                result = json.loads((out.stdout or "").strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                result = {}
                print(f"        child stdout: {out.stdout!r}")
                print(f"        child stderr: {(out.stderr or '')[-800:]}")
        finally:
            if os.path.exists(child):
                os.remove(child)

        check("calendar period survives a fresh process with no local files",
              result.get("period") is True, result)
        check("announcement survives a fresh process with no local files",
              result.get("announcement") is True, result)
    finally:
        for hidden, name in renamed:
            os.replace(hidden, name)

        # Clean up after ourselves in the shared table.
        try:
            if saved_period:
                calendar.delete_period(saved_period["key"])

            if saved_ann:
                announcements.delete_announcement(saved_ann["key"])
        except Exception as e:  # noqa: BLE001 - cleanup must not mask a failure
            print(f"  note: cleanup failed: {e}")

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
