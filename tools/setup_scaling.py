"""Plan retention settings; modify existing AWS resources only with --apply."""

import argparse
import json
import os


def apply(plan):
    import boto3
    ddb = boto3.client("dynamodb", region_name=plan["region"])
    ttl = ddb.describe_time_to_live(TableName=plan["table"])["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
        if ttl.get("AttributeName") != "expires_at":
            raise ValueError("Existing table TTL uses another attribute; refusing to replace it")
    else:
        ddb.update_time_to_live(TableName=plan["table"],
                               TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
    if plan["on_demand"]:
        description = ddb.describe_table(TableName=plan["table"])["Table"]
        if description.get("BillingModeSummary", {}).get("BillingMode") != "PAY_PER_REQUEST":
            ddb.update_table(TableName=plan["table"], BillingMode="PAY_PER_REQUEST")
    s3 = boto3.client("s3", region_name=plan["region"])
    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=plan["bucket"]).get("Rules", [])
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    identifier = "sc-assistant-index-staging"
    for rule in rules:
        if rule.get("ID") == identifier and rule.get("Filter", {}).get("Prefix") != plan["prefix"]:
            raise ValueError("Lifecycle ID belongs to another prefix; refusing to overwrite")
    rules = [rule for rule in rules if rule.get("ID") != identifier]
    rules.append({"ID": identifier, "Status": "Enabled", "Filter": {"Prefix": plan["prefix"]},
                  "Expiration": {"Days": plan["staging_days"]},
                  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}})
    s3.put_bucket_lifecycle_configuration(Bucket=plan["bucket"], LifecycleConfiguration={"Rules": rules})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--on-demand", action="store_true", help="Opt in to changing table billing to PAY_PER_REQUEST")
    parser.add_argument("--staging-days", type=int, default=7)
    args = parser.parse_args(argv)
    if not 2 <= args.staging_days <= 30:
        parser.error("Use 2-30 days of staged-input retention")
    plan = {"table": os.getenv("STORE_TABLE_NAME", "SCAssistantStores"),
            "bucket": os.getenv("S3_BUCKET_NAME", "sc-assistant-bucket2"),
            "region": os.getenv("AWS_REGION", "ap-southeast-1"),
            "ttl_attribute": "expires_at", "prefix": "_sc_assistant/indexing/",
            "staging_days": args.staging_days, "on_demand": args.on_demand}
    if args.apply:
        apply(plan)
    print(json.dumps({"applied": args.apply, "plan": plan,
                      "note": "No resources are created. Existing lifecycle rules are preserved."}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
