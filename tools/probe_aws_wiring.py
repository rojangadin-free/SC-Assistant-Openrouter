"""One call that answers "is this AWS account wired up for the app?".

Moving regions moves more than Cognito: `aws/dynamodb.py`, `aws/students.py` and
`aws/s3.py` all build their clients from the same `AWS_REGION`, so a region switch
silently leaves the database and bucket behind. That failure surfaces far from its
cause — a missing Conversations table shows up as "Failed to load chats" in the
sidebar, with the real ResourceNotFoundException only in the server log.

Checks every resource the app touches at runtime and prints one line each.

    python tools/probe_aws_wiring.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import boto3
from botocore.exceptions import ClientError

from config import (
    AWS_REGION,
    COGNITO_CLIENT_ID,
    COGNITO_CLIENT_SECRET,
    COGNITO_USER_POOL_ID,
    S3_BUCKET_NAME,
)

# Table -> the feature that breaks when it is missing.
TABLES = {
    "Conversations": "chat history sidebar",
    "Files": "admin document uploads",
    "StudentRecords": "role-aware answers",
    "SCAssistantReports": "student reports / feedback",
    "SCAssistantStores": "shared analytics stores",
}


def check_identity():
    try:
        ident = boto3.client("sts").get_caller_identity()
        print(f"  account            {ident['Account']}")
        print(f"  identity           {ident['Arn'].rsplit('/', 1)[-1]}")
    except ClientError as e:
        print(f"  account            FAILED: {e.response['Error']['Code']}")


def check_cognito():
    if not COGNITO_USER_POOL_ID:
        print("  cognito pool       MISSING from .env")
        return
    try:
        pool = boto3.client("cognito-idp", region_name=AWS_REGION).describe_user_pool(
            UserPoolId=COGNITO_USER_POOL_ID
        )["UserPool"]
        print(f"  cognito pool       OK ({pool.get('Name')})")
    except ClientError as e:
        print(f"  cognito pool       {e.response['Error']['Code']}")

    # A client id without a secret is the "confidential vs public app client"
    # mistake, and it fails as NotAuthorizedException — indistinguishable from a
    # wrong password unless you know to look here.
    print(f"  cognito client id  {'set' if COGNITO_CLIENT_ID else 'MISSING'}")
    print(f"  cognito secret     {'set' if COGNITO_CLIENT_SECRET else 'MISSING'}")


def check_tables():
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    for name, feature in TABLES.items():
        try:
            desc = ddb.describe_table(TableName=name)["Table"]
            keys = "+".join(k["AttributeName"] for k in desc["KeySchema"])
            gsis = [i["IndexName"] for i in desc.get("GlobalSecondaryIndexes", [])]
            extra = f", gsi: {','.join(gsis)}" if gsis else ""
            print(f"  ddb {name:<19}OK (key: {keys}{extra})")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            print(f"  ddb {name:<19}{code}  -> breaks: {feature}")


def check_bucket():
    try:
        boto3.client("s3", region_name=AWS_REGION).head_bucket(Bucket=S3_BUCKET_NAME)
        print(f"  s3 {S3_BUCKET_NAME:<20}OK")
    except ClientError as e:
        # head_bucket answers 404 for "no such bucket" and 403 for "exists but
        # not yours / no permission", which are different problems.
        code = e.response["Error"]["Code"]
        print(f"  s3 {S3_BUCKET_NAME:<20}{code}  -> breaks: document uploads")


def main():
    print(f"region: {AWS_REGION}")
    print("-" * 60)
    check_identity()
    check_cognito()
    check_tables()
    check_bucket()


if __name__ == "__main__":
    main()
