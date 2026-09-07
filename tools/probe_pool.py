"""Print the pool's sign-in configuration.

Confirms which identifier the pool actually signs users in with — UsernameAttributes
(email/phone as the username) vs AliasAttributes (email usable as an alias) vs
neither (username only). That decides whether resolving an email to a username is
necessary at all.

    python tools/probe_pool.py
"""
import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from botocore.exceptions import ClientError

from aws.cognito import cognito_client
from config import COGNITO_USER_POOL_ID


def main():
    try:
        pool = cognito_client.describe_user_pool(
            UserPoolId=COGNITO_USER_POOL_ID
        )["UserPool"]
    except ClientError as e:
        err = e.response.get("Error", {})
        print(f"describe_user_pool failed: {err.get('Code')} — {err.get('Message')}")
        return

    print(f"pool id          : {COGNITO_USER_POOL_ID}")
    print(f"name             : {pool.get('Name')}")
    print(f"UsernameAttributes: {pool.get('UsernameAttributes')}")
    print(f"AliasAttributes   : {pool.get('AliasAttributes')}")
    print(f"AutoVerified      : {pool.get('AutoVerifiedAttributes')}")
    print(f"est. users        : {pool.get('EstimatedNumberOfUsers')}")
    print("-" * 60)

    if pool.get("UsernameAttributes"):
        print("Sign-in identifier: EMAIL (the email IS the username).")
    elif pool.get("AliasAttributes"):
        print(f"Sign-in identifier: username OR alias {pool['AliasAttributes']}.")
    else:
        print("Sign-in identifier: USERNAME only — an email must be resolved to")
        print("the username before initiate_auth, which is what")
        print("aws.cognito.resolve_cognito_username() does.")


if __name__ == "__main__":
    main()
