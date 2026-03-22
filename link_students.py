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
    # (seed_id, student_number, full_name, email, program_code, year, section)
    ("uid-bsit-001", "2021-00101", "Juan Dela Cruz",        "juan.delacruz@spc.edu.ph",    "BSIT",   3, "BSIT-3A"),
    ("uid-bsit-002", "2022-00202", "Maria Santos",          "maria.santos@spc.edu.ph",     "BSIT",   2, "BSIT-2B"),
    ("uid-bsit-003", "2020-00303", "Carlo Reyes",           "carlo.reyes@spc.edu.ph",      "BSIT",   4, "BSIT-4A"),
    ("uid-bsit-004", "2023-00404", "Ana Lim",               "ana.lim@spc.edu.ph",          "BSIT",   1, "BSIT-1A"),
    ("uid-bsit-005", "2022-00505", "Jose Garcia",           "jose.garcia@spc.edu.ph",      "BSIT",   2, "BSIT-2A"),
    ("uid-bscs-001", "2021-00601", "Rizalina Cruz",         "rizalina.cruz@spc.edu.ph",    "BSCS",   3, "BSCS-3A"),
    ("uid-bscs-002", "2022-00702", "Miguel Flores",         "miguel.flores@spc.edu.ph",    "BSCS",   2, "BSCS-2A"),
    ("uid-bsed-001", "2021-00801", "Luisa Villanueva",      "luisa.villanueva@spc.edu.ph", "BSEd",   3, "BSEd-3A"),
    ("uid-bsed-002", "2022-00902", "Ramon Aquino",          "ramon.aquino@spc.edu.ph",     "BSEd",   2, "BSEd-2A"),
    ("uid-beed-001", "2021-01001", "Pia Mendoza",           "pia.mendoza@spc.edu.ph",      "BEEd",   3, "BEEd-3A"),
    ("uid-beed-002", "2023-01102", "Felix Bautista",        "felix.bautista@spc.edu.ph",   "BEEd",   1, "BEEd-1A"),
    ("uid-crim-001", "2020-01201", "Andres Tan",            "andres.tan@spc.edu.ph",       "BSCrim", 4, "BSCrim-4A"),
    ("uid-crim-002", "2022-01302", "Celia Ramos",           "celia.ramos@spc.edu.ph",      "BSCrim", 2, "BSCrim-2A"),
    ("uid-bsba-001", "2021-01401", "Nathaniel Ocampo",      "nathaniel.ocampo@spc.edu.ph", "BSBA",   3, "BSBA-3A"),
    ("uid-bsba-002", "2023-01502", "Sophia Torres",         "sophia.torres@spc.edu.ph",    "BSBA",   1, "BSBA-1A"),
    ("uid-bsn-001",  "2021-01601", "Angeline Castillo",     "angeline.castillo@spc.edu.ph","BSN",    3, "BSN-3A"),
    ("uid-bsn-002",  "2022-01702", "Eduardo Navarro",       "eduardo.navarro@spc.edu.ph",  "BSN",    2, "BSN-2A"),
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
    Update the StudentRecords row so student_id = cognito_sub.
    If the seed record exists under seed_id, copy it under cognito_sub
    and delete the old seed entry.
    """
    if seed_id == cognito_sub:
        return  # already linked (e.g. script run twice)

    # Fetch existing record by seed id
    resp = table.get_item(Key={"student_id": seed_id})
    record = resp.get("Item")

    if not record:
        # Try fetching by email (GSI) in case it was already re-keyed
        try:
            resp2 = table.query(
                IndexName="email-index",
                KeyConditionExpression=boto3.dynamodb.conditions.Key("email").eq(email),
            )
            items = resp2.get("Items", [])
            if items:
                record = items[0]
                if record["student_id"] == cognito_sub:
                    print(f"    Already linked: {email}")
                    return
        except Exception:
            pass

    if not record:
        print(f"    [WARN] No DynamoDB record found for seed_id={seed_id} / email={email}")
        return

    # Write under the real Cognito sub
    record["student_id"] = cognito_sub
    table.put_item(Item=record)

    # Remove the old seed-id entry only if it was different
    if seed_id != cognito_sub:
        table.delete_item(Key={"student_id": seed_id})

    print(f"    DynamoDB student_id updated: {seed_id} → {cognito_sub}")


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
    for seed_id, student_number, full_name, email, *_ in DUMMY_STUDENTS:
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