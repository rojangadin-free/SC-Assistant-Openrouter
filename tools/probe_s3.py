"""Why is S3 answering 403?

`head_bucket` returns a bare 403 for two very different situations — the bucket
belongs to another account, or it is ours but the IAM policy does not allow the
call. `list_objects_v2` and `list_buckets` separate them:

    list_objects_v2 OK          -> bucket is ours and readable; head_bucket just
                                   is not covered by the policy (s3:ListBucket on
                                   the bucket ARN is what head_bucket needs)
    list_objects_v2 AccessDenied -> the bucket is not reachable by this credential
    list_buckets    AccessDenied -> no s3:ListAllMyBuckets; normal, not a problem

    python tools/probe_s3.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION, S3_BUCKET_NAME


def attempt(label, fn):
    try:
        return label, fn(), None
    except ClientError as e:
        return label, None, e.response["Error"]["Code"]


def main():
    s3 = boto3.client("s3", region_name=AWS_REGION)

    print(f"bucket : {S3_BUCKET_NAME}")
    print(f"region : {AWS_REGION}")
    print("-" * 60)

    _, resp, code = attempt("head_bucket", lambda: s3.head_bucket(Bucket=S3_BUCKET_NAME))
    print(f"  head_bucket      {code or 'OK'}")

    _, resp, code = attempt(
        "list_objects_v2",
        lambda: s3.list_objects_v2(Bucket=S3_BUCKET_NAME, MaxKeys=3),
    )
    if code:
        print(f"  list_objects_v2  {code}")
    else:
        keys = [o["Key"] for o in resp.get("Contents", [])]
        print(f"  list_objects_v2  OK ({resp.get('KeyCount', 0)} keys: {keys})")

    _, resp, code = attempt("get_bucket_location", lambda: s3.get_bucket_location(Bucket=S3_BUCKET_NAME))
    if code:
        print(f"  bucket location  {code}")
    else:
        # us-east-1 is reported as None for historical reasons.
        print(f"  bucket location  {resp.get('LocationConstraint') or 'us-east-1'}")

    _, resp, code = attempt("list_buckets", lambda: s3.list_buckets())
    if code:
        print(f"  list_buckets     {code} (expected: not in the IAM policy)")
    else:
        print(f"  list_buckets     OK {[b['Name'] for b in resp.get('Buckets', [])]}")


if __name__ == "__main__":
    main()
