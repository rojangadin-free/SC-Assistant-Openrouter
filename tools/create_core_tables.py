"""Create the DynamoDB tables the chat UI needs: Conversations and Files.

Why this exists
---------------
`AWS_REGION` selects the DynamoDB region as well as the Cognito region
(`aws/dynamodb.py` builds its resource from it), so moving the project from
us-east-1/account 225119180951 to ap-southeast-1/account 731372490374 left the
pool behind *and* the database. `ap-southeast-1` had only StudentRecords,
SCAssistantStores and SCAssistantReports, so the sidebar's `/chat/conversations`
call raised ResourceNotFoundException and the UI showed "Failed to load chats".

The schemas are not guesses — they are read back off the callers:

  Conversations
    get_conversation()             get_item(Key={"conv_id", "uid"})  -> composite key
    list_conversations()           query(IndexName="uid-index", uid=…) -> GSI on uid
    upsert_conversation()          writes updated_at, so the GSI sorts by it
  Files
    delete_file_from_db()          delete_item(Key={"filename"})     -> single key

`uid-index` is queried with ScanIndexForward=False and no explicit sort key, so
the index needs a RANGE attribute for "newest first" to mean anything; the item
already carries `updated_at`.

Idempotent: ResourceInUseException is treated as success, so it is safe to run
against a region that is already half set up.

    python tools/create_core_tables.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import os

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION

TABLES = [
    {
        "TableName": "Conversations",
        "AttributeDefinitions": [
            {"AttributeName": "conv_id", "AttributeType": "S"},
            {"AttributeName": "uid", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"},
        ],
        # Composite key because get_conversation/delete_conversation_from_db both
        # pass conv_id AND uid; a HASH-only table rejects that call outright.
        "KeySchema": [
            {"AttributeName": "conv_id", "KeyType": "HASH"},
            {"AttributeName": "uid", "KeyType": "RANGE"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "uid-index",
                "KeySchema": [
                    {"AttributeName": "uid", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                # ALL: list_conversations() renders titles straight from the query
                # result, so a KEYS_ONLY index would cost one extra read per row.
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": "Files",
        "AttributeDefinitions": [
            {"AttributeName": "filename", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "filename", "KeyType": "HASH"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
]


def main():
    session = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=AWS_REGION,
    )
    dynamodb = session.client("dynamodb")

    print(f"region: {AWS_REGION}")
    print("-" * 60)

    for spec in TABLES:
        name = spec["TableName"]
        try:
            dynamodb.create_table(**spec)
            dynamodb.get_waiter("table_exists").wait(TableName=name)
            print(f"  {name:<16} created")
        except dynamodb.exceptions.ResourceInUseException:
            print(f"  {name:<16} already exists — skipped")
        except ClientError as e:
            print(f"  {name:<16} FAILED: {e.response['Error']['Message']}")

    existing = dynamodb.list_tables().get("TableNames", [])
    print("-" * 60)
    print(f"tables in {AWS_REGION}: {existing}")


if __name__ == "__main__":
    main()
