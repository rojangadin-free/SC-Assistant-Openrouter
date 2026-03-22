"""
debug_student.py
Run: python debug_student.py
"""
import boto3
from dotenv import load_dotenv
import os

load_dotenv()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table("StudentRecords")


def list_all():
    resp = table.scan(
        ProjectionExpression="student_id, full_name, email, balance, total_tuition, payment_status"
    )
    items = resp.get("Items", [])
    print(f"\nFound {len(items)} records:\n")
    print(f"  {'student_id':<36} {'full_name':<28} {'total_tuition':<15} {'balance':<12} status")
    print(f"  {'-'*105}")
    for item in sorted(items, key=lambda x: x.get("full_name", "")):
        print(
            f"  {item.get('student_id',''):<36} "
            f"{item.get('full_name',''):<28} "
            f"PHP {item.get('total_tuition','MISSING'):<12} "
            f"PHP {item.get('balance','MISSING'):<9} "
            f"{item.get('payment_status','MISSING')}"
        )


def deep_check(email):
    resp = table.scan(
        FilterExpression=boto3.dynamodb.conditions.Attr("email").eq(email)
    )
    items = resp.get("Items", [])
    if not items:
        print(f"No record found for {email}")
        return
    for item in items:
        print(f"\n{'='*60}")
        print(f"student_id    : {item.get('student_id')}")
        print(f"full_name     : {item.get('full_name')}")
        print(f"total_tuition : {item.get('total_tuition', '*** MISSING ***')}")
        print(f"total_paid    : {item.get('total_paid',    '*** MISSING ***')}")
        print(f"balance       : {item.get('balance',       '*** MISSING ***')}")
        print(f"payment_status: {item.get('payment_status','*** MISSING ***')}")
        has_breakdown = 'tuition_breakdown' in item
        has_history   = 'payment_history'   in item
        print(f"tuition_breakdown: {'present (' + str(len(item['tuition_breakdown'])) + ' items)' if has_breakdown else '*** MISSING ***'}")
        print(f"payment_history  : {'present (' + str(len(item['payment_history']))   + ' items)' if has_history   else '*** MISSING ***'}")
        print(f"\nAll keys: {sorted(item.keys())}")


if __name__ == "__main__":
    list_all()
    print("\n\nDeep check — Eduardo Navarro:")
    deep_check("eduardo.navarro@spc.edu.ph")
    print("\n\nDeep check — Angeline Castillo:")
    deep_check("angeline.castillo@spc.edu.ph")