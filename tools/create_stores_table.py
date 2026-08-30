"""
create_stores_table.py — create the one DynamoDB table that backs `rag/store.py`.

Run this once before switching STORE_BACKEND to `dynamodb`:

    python tools/create_stores_table.py
    # then in .env:  STORE_BACKEND=dynamodb

Why one table for four stores
-----------------------------
`conflicts`, `gaps`, `feedback` and `escalations` are four different concerns, and
in a bigger system they would be four tables. They share one here because the
partition key already separates them and none of them is ever queried across
stores: every read is "give me this store", never "find all rows where…". Four
tables would mean four things to provision, four IAM resource ARNs, and four ways
for a deployment to be half-finished — for no gain.

`billing = PAY_PER_REQUEST` for the same reason the rest of the project uses it:
this table sees a few writes a day, and provisioning capacity for that is paying
a monthly fee to avoid a cost that rounds to zero.
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import os

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION

TABLE = os.getenv("STORE_TABLE_NAME", "SCAssistantStores")


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
    """
    os.environ["STORE_BACKEND"] = "file"
    from rag import conflicts, escalation, feedback, gaps
    from rag import store as store_mod

    snapshots = {
        "conflicts": conflicts._load_store(),
        "gaps": gaps._load(),
        "feedback": feedback._load(),
        "escalations": escalation._load(),
    }

    os.environ["STORE_BACKEND"] = "dynamodb"
    store_mod.reset_cache()

    for name, data in snapshots.items():
        rows = sum(len(v) for v in data.values() if isinstance(v, dict))
        blob = store_mod.JsonBlobStore(name, "", lambda: {})
        blob.save(data)
        print(f"   migrated {name}: {rows} row(s)")


if __name__ == "__main__":
    create_stores_table()
    print("\nMigrating existing local JSON into the table...")
    try:
        migrate_local_files()
        print("✅ Migration done.")
    except Exception as e:
        print(f"⚠️  Migration skipped: {e}")
