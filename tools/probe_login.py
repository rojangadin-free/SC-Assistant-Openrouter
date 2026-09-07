"""Show the RAW Cognito error for a sign-in attempt.

aws.cognito.handle_cognito_error() maps every Cognito error code to a sentence
for the student, which is right for the UI and useless for debugging: an
InvalidParameterException raised because the *pool* is misconfigured reads
exactly like one raised because the user mistyped their address. This prints the
code and AWS's own message so the two can be told apart.

    python tools/probe_login.py <email-or-username> [password]

The password defaults to a string no account could have, so the expected
"working" outcome is NotAuthorizedException (wrong password) — that proves the
USERNAME/SECRET_HASH pair was accepted and only the credential was refused.
"""
import sys

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from botocore.exceptions import ClientError

from aws.cognito import cognito_client, get_secret_hash, resolve_cognito_username
from config import COGNITO_CLIENT_ID, COGNITO_USER_POOL_ID


def main():
    identifier = sys.argv[1] if len(sys.argv) > 1 else "rojangadin"
    password = sys.argv[2] if len(sys.argv) > 2 else "__probe_wrong_password__"

    resolved = resolve_cognito_username(identifier)
    print(f"pool       : {COGNITO_USER_POOL_ID}")
    print(f"client id  : {COGNITO_CLIENT_ID[:10]}…")
    print(f"identifier : {identifier}")
    print(f"resolved   : {resolved}")
    print(f"secret hash: {'computed' if get_secret_hash(resolved) else 'EMPTY — no client secret configured'}")
    print("-" * 60)

    try:
        cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": resolved,
                "PASSWORD": password,
                "SECRET_HASH": get_secret_hash(resolved),
            },
        )
        print("RESULT: signed in (password was correct)")
    except ClientError as e:
        err = e.response.get("Error", {})
        print(f"CODE   : {err.get('Code')}")
        print(f"MESSAGE: {err.get('Message')}")
    except Exception as e:  # noqa: BLE001 - this is a diagnostic script
        print(f"NON-AWS {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
