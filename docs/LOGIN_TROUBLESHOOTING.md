# Login troubleshooting

> **Status: resolved.** `ALLOW_USER_PASSWORD_AUTH` is now enabled on the app
> client and `tools/probe_login.py` reaches the password check. Kept as the
> record of how it was diagnosed. Setup reference: `docs/COGNITO_SETUP.md`.

## Symptom

Signing in returns an error even with the correct password:

- Old copy: *"Please provide a valid email address and username. Usernames cannot be email addresses."*
- Then: *"That doesn't look like a valid email address or username."*
- Now: *"Sign-in is temporarily unavailable due to a server configuration issue."*

All three were the same underlying failure, described three different (and for the
first two, misleading) ways.

## Root cause

Cognito returns `InvalidParameterException` for both bad user input **and**
server-side misconfiguration. `handle_cognito_error()` mapped the whole error
*code* to one sentence about the email address, so a broken pool read as a typo.

The pool's actual message, surfaced by `tools/probe_login.py`:

```
CODE   : InvalidParameterException
MESSAGE: USER_PASSWORD_AUTH flow not enabled for this client
```

`aws/cognito.py` calls `initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ...)`, but
that flow is not in the app client's **ExplicitAuthFlows**. No login can ever
succeed in this state, with any identifier and any password.

Confirmed with `tools/probe_auth_flows.py`:

```
USER_PASSWORD_AUTH           NOT ENABLED for this client
ADMIN_USER_PASSWORD_AUTH     IAM: this credential may not call it
ADMIN_NO_SRP_AUTH (legacy)   IAM: this credential may not call it
client id exists?            ENABLED  (password refused, flow accepted)
```

The last line matters: `REFRESH_TOKEN_AUTH` reached the same client id and got a
token error rather than `ResourceNotFoundException`, so **`COGNITO_CLIENT_ID` is
valid**. This is a console setting, not a wrong id in `.env`.

Full pool/app-client requirements, and how to build one from scratch, are in
`docs/COGNITO_SETUP.md`.

## Not the cause: `custom:data_consent`

Adding the attribute is correct (Settings needs it) but cannot affect sign-in.
`custom:data_consent` is read from the **ID token claims** in `auth.login()`,
which only exist after `initiate_auth` succeeds. The request is rejected before
any claim is read, so the same error persists.

## Not the cause: the working us-east-1 pool

That pool is in a **different AWS account** — `225119180951` ("nonchalant") vs
`731372490374` ("Carlito"), which is what `.env` points at. Its user attributes
already match; the difference is one unticked checkbox on the app client, and
that is not visible on a user's attribute page.

## The fix (AWS console — cannot be done from this codebase)

The `sc-assistant` IAM user has no `UpdateUserPoolClient` permission, so this had
to be done by hand:

1. Cognito → **User pool `ap-southeast-1_VIlM510r8`** ("User pool - wkau-j")
2. **App integration** → App clients → the client starting `6ql3g39e`
3. **Edit** → *Authentication flows*
4. Tick **ALLOW_USER_PASSWORD_AUTH** (`ALLOW_REFRESH_TOKEN_AUTH` must stay ticked)
5. Save, then re-run `python tools/probe_login.py <email>`

Result afterwards, with a deliberately wrong password — this is what the pool
returns now:

```
CODE   : NotAuthorizedException
MESSAGE: Incorrect username or password.
```

`NotAuthorizedException` is the goal — it proves the flow and the SecretHash were
accepted and only the password was refused.

## Sign-in identifier

`tools/probe_pool.py` reports:

```
UsernameAttributes: None
AliasAttributes   : ['email']
```

So the pool accepts an email *as an alias* for the username. `resolve_cognito_username()`
still translates email → username before `initiate_auth`, because alias resolution
depends on `email_verified` having been set when the alias was assigned, whereas the
canonical username is never ambiguous. It degrades silently to the raw input if the
`ListUsers` lookup fails.

Verified against the live pool:

| input                      | resolved to     |
| -------------------------- | --------------- |
| `gadinrojanorg@gmail.com`  | `rojangadin`    |
| `patrick.dacles@spc.edu.ph`| `patrickdacles` |
| `rojangadin`               | `rojangadin`    |
| `nobody@nowhere.com`       | unchanged       |

## IAM gaps found while debugging

`docs/AWS IAM Policies.txt` grants `ListUsers`, `DescribeUserPool` and
`InitiateAuth`, which is enough for the app. These are **not** granted, and each
blocked a diagnostic:

| action                                | blocked                          |
| ------------------------------------- | -------------------------------- |
| `cognito-idp:DescribeUserPoolClient`  | reading ExplicitAuthFlows directly |
| `cognito-idp:ListUserPoolClients`     | listing clients on the pool      |
| `cognito-idp:AdminInitiateAuth`       | admin auth flow as a fallback    |

Adding the two read-only ones would make this class of problem diagnosable in one
call instead of by inference. `AdminInitiateAuth` is only needed if you decide to
switch to the admin flow instead of enabling `USER_PASSWORD_AUTH`.

## Diagnostics

| script                          | answers                                       |
| ------------------------------- | --------------------------------------------- |
| `tools/probe_login.py`          | What is the *raw* Cognito error?              |
| `tools/probe_auth_flows.py`     | Which auth flows does this client accept?     |
| `tools/probe_pool.py`           | Is sign-in by username, email, or alias?      |
| `tools/probe_pool_clients.py`   | Is `COGNITO_CLIENT_ID` even on this pool?     |
