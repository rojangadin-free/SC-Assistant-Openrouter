"""
create_stores_table.py — create the one DynamoDB table that backs `rag/store.py`.

Run this once before switching STORE_BACKEND to `dynamodb`:

    python tools/create_stores_table.py
    # then in .env:  STORE_BACKEND=dynamodb

Why one table for every store
-----------------------------
`conflicts`, `gaps`, `feedback`, `escalations`, `announcements`, `calendar`,
`freshness` and `index_jobs` are separate concerns, and in a bigger system they
would be separate tables. They share one here because the partition key already
separates them and none of them is ever queried across stores: every read is
"give me this store", never "find all rows where…". Eight tables would mean eight
things to provision, eight IAM resource ARNs, and eight ways for a deployment to
be half-finished — for no gain.

`billing = PAY_PER_REQUEST` for the same reason the rest of the project uses it:
this table sees a few writes a day, and provisioning capacity for that is paying
a monthly fee to avoid a cost that rounds to zero.
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import os
import sys

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION

TABLE = os.getenv("STORE_TABLE_NAME", "SCAssistantStores")

# Windows' console defaults to cp1252, which cannot encode the emoji below — so
# the "table already exists" branch died with a UnicodeEncodeError *while
# reporting success*, and took the migration with it. Reconfiguring stdout is a
# one-liner; the alternative (dropping the emoji) loses the at-a-glance status in
# every other script that already prints them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")



def create_stores_table():
    session = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=AWS_REGION,
    )
    dynamodb = session.client("dynamodb")

    try:
        print(f"Creating {TABLE} table in {AWS_REGION}...")
        dynamodb.create_table(
            TableName=TABLE,
            # `store` is the store name: "conflicts" | "gaps" | "feedback" |
            # "escalations". No sort key: each store is exactly one item, so
            # there is nothing to range over.
            AttributeDefinitions=[{"AttributeName": "store", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "store", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=TABLE)
        print(f"✅ {TABLE} created.")
        print("   Now set STORE_BACKEND=dynamodb in .env to start using it.")

    except dynamodb.exceptions.ResourceInUseException:
        print(f"ℹ️  Table '{TABLE}' already exists — skipping.")
    except ClientError as e:
        print(f"❌ AWS Client Error: {e.response['Error']['Message']}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")


def migrate_local_files():
    """
    Copy whatever is already in the local JSON files into the table.

    Without this step, switching the backend looks like data loss: every decision
    an admin already made would vanish from the UI, because the code would start
    reading a table nobody has written to yet. Idempotent — re-running just
    overwrites with the same content.

    Every store gets migrated, not just the first four that existed when this
    script was written. `announcements`, `calendar`, `freshness` and `index_jobs`
    were added later and were never listed here, so they stayed file-only: the
    admin's calendar periods and posted announcements lived in a container's
    writable layer and were discarded on the next deploy. A missing entry in this
    dict is silent — the feature works perfectly until the redeploy — so the list
    is asserted against `rag/` below rather than trusted.

    Empty stores are skipped. Writing `{"periods": {}}` over a table row that
    already holds real periods is the one way this script could destroy data,
    and it would happen on any machine whose local JSON was never populated.
    """
    os.environ["STORE_BACKEND"] = "file"
    from rag import announcements, calendar, conflicts, escalation, feedback, freshness, gaps, jobs
    from rag import store as store_mod

    snapshots = {
        "announcements": announcements._store().load(),
        "calendar": calendar._load(),
        "conflicts": conflicts._load_store(),
        "escalations": escalation._load(),
        "feedback": feedback._load(),
        "freshness": freshness._store().load(),
        "gaps": gaps._load(),
        "index_jobs": jobs._store().load(),
    }

    os.environ["STORE_BACKEND"] = "dynamodb"
    store_mod.reset_cache()

    for name, data in sorted(snapshots.items()):
        rows = sum(len(v) for v in data.values() if isinstance(v, (dict, list)))
        blob = store_mod.JsonBlobStore(name, "", lambda: {})
        if not rows:
            # Nothing local to copy. Leaving the row untouched is the safe
            # choice: it might already hold what another machine wrote.
            existing = blob.load()
            have = sum(len(v) for v in existing.values() if isinstance(v, (dict, list)))
            print(f"   skipped  {name}: nothing local ({have} row(s) already in the table)")
            continue
        blob.save(data)
        print(f"   migrated {name}: {rows} row(s)")

    _warn_about_unmigrated_stores(set(snapshots))


def _warn_about_unmigrated_stores(known: set):
    """
    Grep `rag/` for every `JsonBlobStore("name", ...)` and complain about any this
    script does not migrate.

    This is the check that would have caught the original bug the day it was
    introduced. A store missing from `snapshots` above produces no error and no
    log line — the feature works perfectly in development and loses its data on
    the next deploy, weeks later, to someone who will not connect the two events.
    A grep is crude, but the alternative (a registry every store must remember to
    join) is the same class of omission one level up.
    """
    import glob
    import re

    found = set()
    for path in glob.glob(os.path.join("rag", "*.py")):
        with open(path, "r", encoding="utf-8") as fh:
            found.update(re.findall(r'JsonBlobStore\(\s*(?:name=)?["\'](\w+)["\']', fh.read()))

    missing = sorted(found - known)
    if missing:
        print(
            f"\n⚠️  These stores exist in rag/ but are NOT migrated by this script:"
            f" {', '.join(missing)}"
            "\n    They will reset on every redeploy. Add them to snapshots above."
        )


if __name__ == "__main__":
    create_stores_table()
    print("\nMigrating existing local JSON into the table...")
    try:
        migrate_local_files()
        print("✅ Migration done.")
    except Exception as e:
        print(f"⚠️  Migration skipped: {e}")

