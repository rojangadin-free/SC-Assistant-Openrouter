# Cognito setup

What this app needs from a user pool, in the order the console asks for it.
"Current state of this project" at the end records how `ap-southeast-1_VIlM510r8`
compares.

## What the code actually requires

Read off `aws/cognito.py` and `sc_assistant/auth.py`, not off the console:

| Requirement | Where it comes from | Why |
| --- | --- | --- |
| `ALLOW_USER_PASSWORD_AUTH` on the app client | `login_user()` calls `initiate_auth(AuthFlow="USER_PASSWORD_AUTH")` | Server-side password auth. Without it **every** sign-in fails. |
| `ALLOW_REFRESH_TOKEN_AUTH` on the app client | Cognito requires it | Cannot be unticked. |
| A **client secret** | `get_secret_hash()` sends `SECRET_HASH` | If the client has no secret, every call fails `NotAuthorizedException`. |
| `AliasAttributes: ['email']` or email sign-in | `resolve_cognito_username()` | Students type an email; the pool must accept the account. |
| `AutoVerifiedAttributes: ['email']` | `forgot_password()` | The reset code is mailed to the verified address. |
| `custom:role` (string, mutable) | `get_user_role_from_claims()` | `"admin"` unlocks the console; missing reads as `"user"`. |
| `custom:data_consent` (string, mutable) | `settings.update_consent()`, `auth.login()` | Stores the agreement answer. Value is the string `"true"`/`"false"`. |
| `name` attribute required | `sign_up_user()` sends it | Cognito rejects sign-up if a required attribute is missing. |

Both custom attributes must be **mutable**. Cognito fixes mutability when the
attribute is created and there is no way to change it afterwards — an immutable
`custom:data_consent` makes the Settings toggle fail permanently, and the only
remedy is a new attribute under a new name.

## 1. Create the pool

Cognito → Create user pool.

- Sign-in options: **User name**, and tick **Email** under "user name
  requirements" so an email works as an alias.
- Required attributes: `name`.
- Email: "Send email with Cognito" is fine for a school project.
- Self-registration: enabled (the app calls `sign_up`).

## 2. Add the two custom attributes

User pool → **Sign-up** → Custom attributes → Add custom attribute. Twice:

| Name | Type | Mutable | Min/Max |
| --- | --- | --- | --- |
| `role` | String | **yes** | 1 / 16 |
| `data_consent` | String | **yes** | 1 / 8 |

Type the name as `role`, not `custom:role`. Cognito adds the `custom:` prefix
itself; typing it yields `custom:custom:role`.

## 3. Create the app client

Applications → App clients → Create app client.

- Type: **Confidential client** — this is what produces the client secret.
- **Generate a client secret**: yes.
- Authentication flows: tick **ALLOW_USER_PASSWORD_AUTH** and
  **ALLOW_REFRESH_TOKEN_AUTH**.

A "Public client" has no secret, and `get_secret_hash()` would then be signing
with an empty string. That failure looks identical to a wrong password.

## 4. Fill in `.env`

```
AWS_REGION=ap-southeast-1
COGNITO_USER_POOL_ID=ap-southeast-1_XXXXXXXXX
COGNITO_CLIENT_ID=<app client id>
COGNITO_CLIENT_SECRET=<app client secret>
```

`AWS_REGION` has to match the pool's region. `config.py` defaults it to
`us-east-1`, so a missing value silently points at the wrong region and the pool
reads as nonexistent.

The region also decides which **DynamoDB** tables the app sees: `aws/dynamodb.py`
and `aws/students.py` both build their clients with the same `AWS_REGION`. Moving
regions moves the database too.

## 5. IAM

`docs/AWS IAM Policies.txt` covers what the app calls at runtime. Two read-only
additions make an auth failure diagnosable in one call instead of by inference:

```
cognito-idp:DescribeUserPoolClient
cognito-idp:ListUserPoolClients
```

Without `DescribeUserPoolClient` you cannot read `ExplicitAuthFlows`, which is
why `tools/probe_auth_flows.py` has to infer it from error codes.

## 6. Verify before touching the UI

```
python tools/probe_aws_wiring.py    # every resource the app touches, one line each
python tools/probe_pool.py          # sign-in identifier: username or alias?
python tools/probe_auth_flows.py    # which flows does the client accept?
python tools/probe_login.py <email> # the raw Cognito error, unmapped
```

`probe_aws_wiring.py` is the one to run after any region or account change. It
checks the pool, the client id/secret, all five DynamoDB tables and the S3 bucket
together, because they all read the same `AWS_REGION` and so all move at once.

`probe_login.py` with a deliberately wrong password should say:

```
CODE   : NotAuthorizedException
MESSAGE: Incorrect username or password.
```

`NotAuthorizedException` is the goal. It proves the flow, the client id and the
SecretHash were all accepted and only the password was refused.

## Current state of this project

Resolved. `ALLOW_USER_PASSWORD_AUTH` has been ticked on client `6ql3g39e…`:

```
USER_PASSWORD_AUTH           ENABLED  (password refused, flow accepted)
client id exists?            ENABLED  (password refused, flow accepted)
```

and `probe_login.py` now reaches the password check instead of being rejected
outright:

```
identifier : gadinrojanorg@gmail.com
resolved   : rojangadin
CODE   : NotAuthorizedException
MESSAGE: Incorrect username or password.
```

For reference, the failure this replaced was `USER_PASSWORD_AUTH NOT ENABLED for
this client`, fixed in the console because the `sc-assistant` IAM user has no
`UpdateUserPoolClient` permission:

1. Cognito, **region Asia Pacific (Singapore)** → user pool `ap-southeast-1_VIlM510r8`
2. Applications → App clients → the client starting `6ql3g39e`
3. **Edit** → Authentication flows
4. Tick **ALLOW_USER_PASSWORD_AUTH**, leave `ALLOW_REFRESH_TOKEN_AUTH` ticked


### Why adding `custom:data_consent` did not help

It could not have. `custom:data_consent` is read *after* authentication, from the
ID token claims:

```python
consent_claim = claims.get("custom:data_consent", "true")   # sc_assistant/auth.py
```

There is no ID token yet — `initiate_auth` is rejected before any claim is read.
The attribute is still worth having (without it the Settings toggle returns
`AWS Error: … Ensure 'custom:data_consent' exists in Cognito`), it just has
nothing to do with sign-in.

### The us-east-1 pool is in a different AWS account

| | working | broken |
| --- | --- | --- |
| account | `225119180951` ("nonchalant") | `731372490374` ("Carlito") |
| region | us-east-1 | ap-southeast-1 |
| pool | `jep9g5` | `wkau-j` / `ap-southeast-1_VIlM510r8` |

`.env` points at Carlito. The two pools are not copies of each other, so
comparing attributes between them will not surface the difference — the
attributes match already, and the difference is one unticked checkbox on the app
client.

Also carried over from the old account: `docs/AWS IAM Policies.txt` still scopes
DynamoDB to `arn:aws:dynamodb:us-east-1:225119180951:table/*`.

### The tables the region change left behind

Signing in worked and the sidebar then said **"Failed to load chats"**. That was
the same region move, one layer down: `ap-southeast-1` had only `StudentRecords`,
`SCAssistantStores` and `SCAssistantReports`, so `list_conversations()` raised
`ResourceNotFoundException` and `/chat/conversations` answered 500.

Created with `python tools/create_core_tables.py`:

| Table | Key | GSI | Needed by |
| --- | --- | --- | --- |
| `Conversations` | `conv_id` (HASH) + `uid` (RANGE) | `uid-index` on `uid` + `updated_at`, project ALL | chat history sidebar |
| `Files` | `filename` (HASH) | — | admin document uploads |

The schemas come from the callers, not from preference. `get_conversation()` and
`delete_conversation_from_db()` both pass `conv_id` **and** `uid` to
`get_item`/`delete_item`, which a HASH-only table rejects outright; and
`list_conversations()` queries `IndexName="uid-index"` with
`ScanIndexForward=False`, so the index needs a RANGE key for "newest first" to
mean anything — `updated_at`, which `upsert_conversation()` already writes.

Verified through the route itself with `python tools/verify_chat_history.py`:

```
PASS  route answers 200
PASS  the stored conversation is listed
PASS  title derived from the first user message
PASS  guest gets an empty list
```

### Still open: the S3 bucket

`AWS_REGION` sets the S3 client's region too, and `sc-assistant-bucket` is not
reachable from this account:

```
head_bucket      403
list_objects_v2  AccessDenied
bucket location  AccessDenied
```

`list_objects_v2` failing as well is the tell. A 403 on `head_bucket` alone would
just mean the IAM policy omits that one call; `AccessDenied` on the object list
and on `get_bucket_location` means the credential cannot reach the bucket at all
— the name is taken by the old account, or by someone else entirely, since S3
bucket names are globally unique. Creating `sc-assistant-bucket` in
`ap-southeast-1` will therefore fail with `BucketAlreadyExists`; the new account
needs its own name (for example `sc-assistant-bucket-apse1`) set in
`S3_BUCKET_NAME`. Until then admin document uploads and `serve_document()`
fall back to the local `data/` folder only.


