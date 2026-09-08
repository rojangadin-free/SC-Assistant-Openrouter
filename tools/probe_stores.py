"""What is actually persisted, and what is only in a local file.

Admin settings that "go back to empty after a redeploy" are stores still on the
`file` backend: a container's writable layer is discarded on every deploy, so
`calendar_periods.json` and friends are born empty in the new container. This
lists every `JsonBlobStore` in the codebase next to the row in
`SCAssistantStores`, so a store that is missing from the table is visible before
someone loses a semester of calendar periods to it.

    python tools/probe_stores.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import json
import os

import boto3
from botocore.exceptions import ClientError

# config, not just AWS_REGION: importing it loads .env AND applies the
# STORE_BACKEND default, so this reports the backend the app would actually use
# rather than the bare env var. Without it this script says "file" on a
# correctly-configured machine, which is the opposite of its job.
import config  # noqa: F401
from config import AWS_REGION


# name -> (local file default, what the admin loses if it is not shared)
STORES = {
    "announcements": ("announcements.json", "posted announcements"),
    "calendar": ("calendar_periods.json", "academic calendar periods"),
    "conflicts": ("conflict_resolutions.json", "pinned correct values"),
    "escalations": ("escalations.json", "student questions sent to an office"),
    "feedback": ("answer_feedback.json", "answer votes"),
    "freshness": ("doc_freshness.json", "document effective dates"),
    "gaps": ("content_gaps.json", "unanswered-question log"),
    "index_jobs": ("index_jobs.json", "indexing job history"),
}

TABLE = os.getenv("STORE_TABLE_NAME", "SCAssistantStores")


def rows_in(payload: str) -> str:
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return "unreadable"
    counts = {k: len(v) for k, v in data.items() if isinstance(v, (dict, list))}
    return ", ".join(f"{k}={n}" for k, n in counts.items()) or "empty"


def local_rows(path: str) -> str:
    if not os.path.exists(path):
        return "no file"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return "unreadable"
    counts = {k: len(v) for k, v in data.items() if isinstance(v, (dict, list))}
    return ", ".join(f"{k}={n}" for k, n in counts.items()) or "empty"


def main():
    backend = (os.getenv("STORE_BACKEND") or "file").strip().lower()
    print(f"STORE_BACKEND : {backend}")
    print(f"table         : {TABLE} ({AWS_REGION})")
    if backend != "dynamodb":
        print("\n  WARNING: on the 'file' backend nothing here survives a redeploy.")
    print("-" * 78)

    items = {}
    try:
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE)
        resp = table.scan()
        items = {i["store"]: i for i in resp.get("Items", [])}
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.update({i["store"]: i for i in resp.get("Items", [])})
    except ClientError as e:
        print(f"  could not read {TABLE}: {e.response['Error']['Code']}")

    print(f"  {'store':<15}{'in table':<10}{'table contents':<26}local file")
    print("-" * 78)
    missing = []
    for name, (path, _loses) in STORES.items():
        item = items.get(name)
        if item:
            print(f"  {name:<15}{'yes':<10}{rows_in(item.get('payload', '')):<26}{local_rows(path)}")
        else:
            missing.append(name)
            print(f"  {name:<15}{'NO':<10}{'—':<26}{local_rows(path)}")

    extra = sorted(set(items) - set(STORES))
    if extra:
        print(f"\n  rows in the table with no matching store: {extra}")

    if missing:
        print("\n  Not persisted — these reset on every redeploy:")
        for name in missing:
            print(f"    {name:<15} loses: {STORES[name][1]}")
        print("\n  Fix: python tools/create_stores_table.py  (creates + migrates)")
    else:
        print("\n  All stores are persisted in DynamoDB.")


if __name__ == "__main__":
    main()
