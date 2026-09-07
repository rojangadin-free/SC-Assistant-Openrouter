"""Ask the pool which auth flows this app client will actually accept.

DescribeUserPoolClient is the direct way to read ExplicitAuthFlows, but the
sc-assistant IAM user is not allowed to call it, so instead this probes each
flow with a deliberately wrong password and reads the error:

    NotAuthorizedException      -> flow IS enabled (only the password was wrong)
    InvalidParameterException   -> flow is NOT enabled for this client
    UserNotFoundException       -> flow enabled, that username does not exist

    python tools/probe_auth_flows.py [username]
"""
import sys

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from botocore.exceptions import ClientError

from aws.cognito import cognito_client, get_secret_hash
from config import COGNITO_CLIENT_ID, COGNITO_USER_POOL_ID

WRONG_PASSWORD = "__probe_wrong_password__"

VERDICT = {
    "NotAuthorizedException": "ENABLED  (password refused, flow accepted)",
    "UserNotFoundException": "ENABLED  (no such user, flow accepted)",
    "InvalidParameterException": "NOT ENABLED for this client",
    "AccessDeniedException": "IAM: this credential may not call it",
}


def probe(label, call):
    try:
        call()
        print(f"  {label:<28} SIGNED IN (password was right?)")
    except ClientError as e:
        err = e.response.get("Error", {})
        code = err.get("Code", "?")
        print(f"  {label:<28} {VERDICT.get(code, code)}")
        if code not in VERDICT:
            print(f"  {'':<28} raw: {err.get('Message')}")
    except Exception as e:  # noqa: BLE001 - diagnostic script
        print(f"  {label:<28} {type(e).__name__}: {e}")


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "rojangadin"
    secret_hash = get_secret_hash(username)
    params = {
        "USERNAME": username,
        "PASSWORD": WRONG_PASSWORD,
        "SECRET_HASH": secret_hash,
    }

    print(f"pool     : {COGNITO_USER_POOL_ID}")
    print(f"username : {username}")
    print(f"secret   : {'computed' if secret_hash else 'EMPTY'}")
    print("-" * 60)

    probe(
        "USER_PASSWORD_AUTH",
        lambda: cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters=params,
        ),
    )
    probe(
        "ADMIN_USER_PASSWORD_AUTH",
        lambda: cognito_client.admin_initiate_auth(
            UserPoolId=COGNITO_USER_POOL_ID,
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters=params,
        ),
    )
    probe(
        "ADMIN_NO_SRP_AUTH (legacy)",
        lambda: cognito_client.admin_initiate_auth(
            UserPoolId=COGNITO_USER_POOL_ID,
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="ADMIN_NO_SRP_AUTH",
            AuthParameters=params,
        ),
    )

    # Proves the client id itself is real. An unknown/wrong client id fails with
    # ResourceNotFoundException here regardless of flows, so a *different* error
    # means .env points at a client that genuinely exists on this pool — which
    # narrows "flow not enabled" down to a console setting rather than a typo.
    probe(
        "client id exists?",
        lambda: cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={
                "REFRESH_TOKEN": "not-a-real-token",
                "SECRET_HASH": secret_hash,
            },
        ),
    )


if __name__ == "__main__":
    main()
