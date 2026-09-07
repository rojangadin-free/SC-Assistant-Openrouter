"""List the app clients on the pool and flag the one .env points at.

"USER_PASSWORD_AUTH flow not enabled for this client" has two possible causes,
and they need different fixes: the flow was never enabled on the client we are
using, or COGNITO_CLIENT_ID points at a *different* client than the one that was
configured. This tells them apart.

    python tools/probe_pool_clients.py
"""
import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from botocore.exceptions import ClientError

from aws.cognito import cognito_client
from config import COGNITO_CLIENT_ID, COGNITO_USER_POOL_ID


def main():
    print(f"pool                : {COGNITO_USER_POOL_ID}")
    print(f"COGNITO_CLIENT_ID   : {COGNITO_CLIENT_ID}")
    print("-" * 70)

    try:
        resp = cognito_client.list_user_pool_clients(
            UserPoolId=COGNITO_USER_POOL_ID, MaxResults=30
        )
    except ClientError as e:
        err = e.response.get("Error", {})
        print(f"could not list clients: {err.get('Code')} — {err.get('Message')}")
        return

    clients = resp.get("UserPoolClients", [])
    if not clients:
        print("no app clients returned")
        return

    found = False
    for c in clients:
        is_ours = c["ClientId"] == COGNITO_CLIENT_ID
        found = found or is_ours
        print(f"  {c['ClientId']}  {c.get('ClientName', '?'):<28}"
              f"{'  <== the one in .env' if is_ours else ''}")

    print("-" * 70)
    if found:
        print("The configured client EXISTS on this pool, so the client id is fine;")
        print("USER_PASSWORD_AUTH simply is not in its ExplicitAuthFlows.")
        print("Fix: Cognito console -> this pool -> App clients -> that client ->")
        print("     Authentication flows -> tick ALLOW_USER_PASSWORD_AUTH -> save.")
    else:
        print("The configured client id is NOT on this pool — .env points somewhere")
        print("else. Fix COGNITO_CLIENT_ID (and CLIENT_SECRET) to match one above.")


if __name__ == "__main__":
    main()
