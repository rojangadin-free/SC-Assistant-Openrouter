"""
link_students.py

Creates a Cognito account for each dummy student and updates their
StudentRecords entry so student_id matches the Cognito sub (uid).

Run ONCE after seed_students.py:
    python link_students.py

What it does per student:
  1. Creates a Cognito user (email + temp password)
  2. Sets a permanent password immediately (no change required on login)
  3. Confirms the account and marks email as verified
  4. Reads back the Cognito sub
  5. Updates StudentRecords: student_id = Cognito sub
     (also keeps old id so nothing breaks if run twice)

Default password for ALL dummy students: SC_Student@2024
Change it below before running if needed.
"""

import boto3
import time
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import os

load_dotenv()

AWS_REGION         = os.getenv("AWS_REGION", "us-east-1")
USER_POOL_ID       = os.getenv("COGNITO_USER_POOL_ID")
TABLE_NAME         = "StudentRecords"
DEFAULT_PASSWORD   = "SC_Student@2024"   # ← change if you want a different default

if not USER_POOL_ID:
    raise RuntimeError("COGNITO_USER_POOL_ID not set in .env")

cognito  = boto3.client("cognito-idp", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb",  region_name=AWS_REGION)
table    = dynamodb.Table(TABLE_NAME)

# ── The same list from seed_students.py ─────────────────────
# (student_id_seed, student_number, full_name, email, program_code, year, section)
DUMMY_STUDENTS = [
    # (seed_id, student_number, full_name, email, gender)
    ("uid-bsit-3a-001", "2023-00101", "Carl Anthony Unay",   "carl.unay@spc.edu.ph",         "Male"),
    ("uid-bsit-3a-002", "2023-00102", "Rojan Gadin",          "rojan.gadin@spc.edu.ph",        "Male"),
    ("uid-bsit-3a-003", "2023-00103", "Rose Ann Gabon",       "roseann.gabon@spc.edu.ph",      "Female"),
    ("uid-bsit-3a-004", "2023-00104", "Jennie Rose Abayan",   "jennierose.abayan@spc.edu.ph",  "Female"),
    ("uid-bsit-3a-005", "2023-00105", "Chris Diocton",        "chris.diocton@spc.edu.ph",      "Male"),
    ("uid-bsit-3a-006", "2023-00106", "Hannah Joy Nacario",   "hannahjoy.nacario@spc.edu.ph",  "Female"),
    ("uid-bsit-3a-007", "2023-00107", "Rajeth Jamorawon",     "rajeth.jamorawon@spc.edu.ph",   "Male"),
    ("uid-bsit-3a-008", "2023-00108", "Mariz Berber",         "mariz.berber@spc.edu.ph",       "Male"),
    ("uid-bsit-3a-009", "2023-00109", "Philip Brian Alvarez", "philipbrian.alvarez@spc.edu.ph","Male"),
    ("uid-bsit-3a-010", "2023-00110", "Patrick Dacles",       "patrick.dacles@spc.edu.ph",     "Male"),
]


def get_sub_by_email(email: str) -> str | None:
    """Find a Cognito user's sub by scanning for their email attribute."""
    try:
        resp = cognito.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'email = "{email}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if not users:
            return None
        return next(
            (a["Value"] for a in users[0]["Attributes"] if a["Name"] == "sub"),
            None,
        )
    except ClientError as e:
        print(f"  [ERROR]   list_users({email}): {e}")
        return None


def get_or_create_cognito_user(student_number: str, full_name: str, email: str) -> str | None:
    """
    Returns the Cognito sub (uid) for this student.
    Uses the full name with spaces removed as the Cognito Username (e.g. "juandelacruz").
    Falls back to email lookup if the user already exists under any username.
    """
    cognito_username = full_name.replace(" ", "").lower()  # e.g. "juandelacruz"

    # ── Check if user already exists by username ──
    try:
        resp = cognito.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=cognito_username,
        )
        sub = next(
            (a["Value"] for a in resp["UserAttributes"] if a["Name"] == "sub"),
            None,
        )
        print(f"  [EXISTS]  {email} (username: {cognito_username})  →  sub={sub}")
        return sub
    except cognito.exceptions.UserNotFoundException:
        pass  # will try to create below
    except ClientError as e:
        print(f"  [ERROR]   admin_get_user({cognito_username}): {e}")
        return None

    # ── Create user ──
    try:
        resp = cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=[
                {"Name": "email",          "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name",           "Value": full_name},
                {"Name": "custom:role",    "Value": "user"},
            ],
            TemporaryPassword=DEFAULT_PASSWORD,
            MessageAction="SUPPRESS",
        )
        sub = next(
            (a["Value"] for a in resp["User"]["Attributes"] if a["Name"] == "sub"),
            None,
        )
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=cognito_username,
            Password=DEFAULT_PASSWORD,
            Permanent=True,
        )
        print(f"  [CREATED] {email} (username: {cognito_username})  →  sub={sub}")
        return sub

    except cognito.exceptions.UsernameExistsException:
        # User exists under a different username — find them by email
        print(f"  [WARN]    Username '{cognito_username}' taken, looking up by email...")
        sub = get_sub_by_email(email)
        if sub:
            print(f"  [FOUND]   {email}  →  sub={sub}")
            # Make sure the password is set correctly
            try:
                cognito.admin_set_user_password(
                    UserPoolId=USER_POOL_ID,
                    Username=cognito_username,
                    Password=DEFAULT_PASSWORD,
                    Permanent=True,
                )
            except ClientError:
                pass  # best effort
            return sub
        print(f"  [ERROR]   Could not find existing user for {email}")
        return None

    except ClientError as e:
        print(f"  [ERROR]   admin_create_user({cognito_username} / {email}): {e}")
        return None


def link_cognito_sub_to_dynamo(seed_id: str, cognito_sub: str, email: str):
    """
    1. Fetch the canonical seed record (has all tuition data).
    2. Write it under the Cognito sub as the new student_id.
    3. Delete the seed record AND any stale UUID duplicates for this email.
    """
    # ── Always fetch the seed record by seed_id first ──────────
    resp = table.get_item(Key={"student_id": seed_id})
    seed_record = resp.get("Item")

    if not seed_record:
        print(f"    [WARN] Seed record '{seed_id}' not found — re-run seed_students.py first.")
        return

    # ── Write the full (tuition-included) record under Cognito sub ──
    new_record = dict(seed_record)
    new_record["student_id"] = cognito_sub
    table.put_item(Item=new_record)
    print(f"    Linked: {seed_id} → {cognito_sub}  "
          f"(balance: PHP {new_record.get('balance', 'N/A')})")

    # ── Delete ALL records for this email EXCEPT the new Cognito one ──
    # This removes the seed record + any previous stale UUID copies
    try:
        resp2 = table.query(
            IndexName="email-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("email").eq(email),
        )
        for item in resp2.get("Items", []):
            old_id = item["student_id"]
            if old_id != cognito_sub:
                table.delete_item(Key={"student_id": old_id})
                print(f"    Deleted stale record: {old_id}")
    except Exception as e:
        print(f"    [WARN] Could not clean up stale records: {e}")
        # Manual fallback: at least delete the seed record
        if seed_id != cognito_sub:
            try:
                table.delete_item(Key={"student_id": seed_id})
            except Exception:
                pass


def print_login_table(results: list):
    print("\n" + "=" * 80)
    print("DEMO LOGIN CREDENTIALS")
    print("=" * 80)
    print(f"{'Name':<28} {'Email':<38} {'Password'}")
    print("-" * 80)
    for name, email in results:
        print(f"{name:<28} {email:<38} {DEFAULT_PASSWORD}")
    print("=" * 80)
    print("\nAll accounts use the same password:", DEFAULT_PASSWORD)


def main():
    print(f"Linking {len(DUMMY_STUDENTS)} dummy students to Cognito pool: {USER_POOL_ID}\n")

    successes = []
    for seed_id, student_number, full_name, email, _gender in DUMMY_STUDENTS:
        print(f"\n→ {full_name} ({email})")

        cognito_sub = get_or_create_cognito_user(student_number, full_name, email)
        if not cognito_sub:
            print(f"  Skipping DynamoDB update — Cognito step failed.")
            continue

        link_cognito_sub_to_dynamo(seed_id, cognito_sub, email)
        successes.append((full_name, email))
        time.sleep(0.1)   # stay well under Cognito rate limits

    print_login_table(successes)
    print(f"\nDone. {len(successes)}/{len(DUMMY_STUDENTS)} students linked successfully.")


if __name__ == "__main__":
    main()