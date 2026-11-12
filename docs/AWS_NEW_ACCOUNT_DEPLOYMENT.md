# AWS Deployment Guide

**SC-Assistant | New AWS account | Single-instance deployment**

Last checked against the repository: **2026-11-09**.

> **Start here:** Follow the phases in order. Every phase says where to work,
> what to do, and what must be true before continuing. This file does not create
> resources or apply the application changes described below.

> **Cost warning:** EC2, EBS, the load balancer, public IPv4 addresses, Secrets
> Manager, ECR, email delivery, and the external AI providers can incur charges.
> This is not a guaranteed free-tier deployment. Budget alerts are not spending caps.

## Roadmap

| Phase | Where you work | What you finish |
|---|---|---|
| [01. Prepare code](#01-prepare-code) | VS Code / your Windows PC | A release that can target the new account safely |
| [02. Account access](#02-account-access) | AWS Console / Windows PowerShell | Correct account, region, and deployment identity |
| [03. Data stores](#03-data-stores) | AWS Console | Private S3 bucket and five DynamoDB tables |
| [04. Cognito](#04-cognito) | AWS Console | User pool, app client, and email verification |
| [05. External providers](#05-external-providers) | Pinecone / OpenRouter | Vector index and funded model access |
| [06. Runtime secret](#06-runtime-secret) | AWS Console | Application configuration in Secrets Manager |
| [07. Registry and role](#07-registry-and-role) | AWS Console | ECR repository and EC2 runtime permissions |
| [08. Build image](#08-build-image) | Windows PowerShell | A versioned Docker image in ECR |
| [09. Launch EC2](#09-launch-ec2) | AWS Console | One instance with restricted network access |
| [10. Configure HTTPS](#10-configure-https) | AWS Console / your DNS provider | A trusted HTTPS application URL |
| [11. Start application](#11-start-application) | EC2 Session Manager terminal | Running container with persistent local state |
| [12. Test and launch](#12-test-and-launch) | Browser / AWS Console | Verified accounts, documents, and persistence |
| [13. Future deployments](#13-future-deployments) | GitHub / AWS | Safe updates, rollback, and eventual automation |

**Reference sections:** [Troubleshooting](#troubleshooting) |
[Migration cautions](#migration-cautions) | [Official references](#official-references).

### What You Are Building

```text
Your browser
    |
    | HTTPS :443
    v
Application Load Balancer + ACM certificate
    |
    | HTTP :8080, allowed only from the load balancer
    v
One EC2 instance -> Docker -> Gunicorn -> Flask
    |
    +-> Cognito: user accounts
    +-> DynamoDB: conversations, reports, academic records, admin settings
    +-> S3: document originals and extracted images
    +-> Pinecone: document vectors
    +-> OpenRouter: language/vision models

ECR stores the application image.
Secrets Manager stores application secrets.
An EC2 instance role supplies AWS credentials automatically.
```

**Why one instance?** Guest history, chat coordination, and password-reset request
coordination use local SQLite. Multiple workers in one container can share it;
independent instances cannot. Do not add an Auto Scaling Group or multiple app
replicas yet.

Request-frequency counters also use a private SQLite file shared by Gunicorn
workers on this **one container/host**, not independent replicas. The DynamoDB
admin-store backend does not make request quotas distributed; scaling out needs
a shared network-backed limiter as well as shared coordination state.

### Command Locations

| Label | Meaning |
|---|---|
| **AWS Console** | Your browser, signed into the new AWS account |
| **Windows PowerShell** | Your own PC, in the project directory unless stated otherwise |
| **EC2 Bash** | The Linux terminal opened through EC2 Session Manager |
| **JSON template** | Configuration for an AWS console editor, not a terminal command |
| **Python change** | An application source change, not an AWS command |

Never paste an EC2 Bash block into Windows PowerShell. Stop at the first error;
do not keep running later commands against a partially configured environment.

### Deployment Worksheet

Record resource identifiers here or in your own deployment notes. **Do not record
passwords, access-key secrets, client secrets, or provider API keys in this file.**

| Identifier | Value to use |
|---|---|
| New AWS account ID | Your new 12-digit account ID |
| AWS region | `ap-southeast-1` throughout this guide |
| Local AWS CLI profile | `sc-new` |
| Application hostname | A subdomain you control, for example `assistant.example.edu` |
| S3 bucket | A globally unique name, for example `sc-assistant-docs-ACCOUNT_ID-ap-southeast-1` |
| Cognito pool ID | Record after Phase 04 |
| Cognito app-client ID | Record after Phase 04; this is not the client secret |
| Pinecone index | `rag-database-2026`, or the name explicitly configured in Phase 01 |
| Runtime secret name | `sc-assistant/prod` |
| Runtime secret ARN | Record the full ARN after Phase 06 |
| ECR repository | `sc-assistant` |
| EC2 runtime role | `SC-Assistant-Runtime` |
| Application image URI | Record the complete URI after Phase 08 |

Every `REPLACE_WITH_...` value below is a placeholder. Replace it with the
corresponding value from the new account. Do not change shell variable names such
as `$AccountId` or `$REGION`.

---

## 01. Prepare Code

**Where:** VS Code and your Windows PC.

**Goal:** Remove the account-specific and deployment assumptions that would make
the later environment settings ineffective or unsafe.

### Current Code Status

The following were observed locally on 2026-11-09. They are not claims about what
has already been created in AWS.

| File | Observed behavior | Required preparation |
|---|---|---|
| [config.py](../config.py) | S3 bucket is hard-coded as `sc-assistant-bucket3` | Read `S3_BUCKET_NAME` from the environment |
| [config.py](../config.py) | Pinecone index is hard-coded as `rag-database-2026` | Read `PINECONE_INDEX_NAME`, or consistently retain that index name |
| [App factory](../sc_assistant/__init__.py) | Reads the `SESSION_COOKIE_SECURE` setting | Set it true for HTTPS deployment; retain HttpOnly and SameSite protections |
| [S3 helper](../aws/s3.py) | Presigned downloads create another default S3 client | Reuse the existing regional SigV4 client |
| [Cognito helper](../aws/cognito.py) | Self-service signup now omits `custom:role` | Keep roles read-only to app clients and verify the ordinary-user default |
| [Deployment workflow](../.github/workflows/cicd.yaml) | Host-wide container removal and Docker pruning | Disable it for the first deployment; replace it before automation |

### 1A. Disable Old Deployment

1. Open the repository on GitHub.
2. Open **Actions** and select the current deployment workflow.
3. Use the workflow menu to disable it temporarily.
4. Do not update its credentials and push `main` while it can still target an old
   self-hosted runner.

Disabling the workflow does not require stopping the old application. Keep any
old resources intact until you have decided whether their data must be migrated.

### 1B. Configure Resource Names

In `config.py`, replace the two hard-coded assignments with:

```python
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "sc-assistant-bucket3")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "rag-database-2026")
```

These defaults preserve the current local settings. **The new deployment must
explicitly supply its own bucket and index values.** Setting an environment
variable without changing the original hard-coded assignment does nothing.

### 1C. Configure Secure Cookies

The local app factory now has this wiring. For an older release that lacks it,
add the following in `sc_assistant/__init__.py`, immediately after
`app.secret_key = FLASK_SECRET_KEY`:

```python
app.config.update(
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
```

Local HTTP development can keep this setting false. The HTTPS deployment below
sets it to true. Never use the default `your_default_secret_key` in a deployment.

### 1D. Keep S3 Signing Consistent

In `aws/s3.py`, remove the following local assignment from inside
`get_s3_presigned_url()`:

```python
s3_client = boto3.client('s3')
```

Keep the module-level `s3_client` unchanged. That client already specifies
`AWS_REGION` and `Config(signature_version='s3v4')`.

### 1E. Protect Administrative Roles

The current `aws/cognito.py` helper no longer sends a role during signup. When
deploying an older release, remove this entry from `sign_up_user()`'s `UserAttributes`:

```python
{"Name": "custom:role", "Value": "user"}
```

Keep the `email`, `name`, and `custom:student_id` entries. The application already
treats a missing role as `user`. Administrative roles must be assigned by a
trusted administrator, not written by a user-facing app client.

The signup tests verify that the role attribute is not sent and that the default
effective role remains `user`. Keep those checks when deploying a different
release. Do not make `custom:role` writable to bypass an unauthorized-attribute error.

### 1F. Test the Release

Use the project's Python environment, Node.js, and a clean release checkout:

```powershell
npm ci --ignore-scripts
npm run build:ui
python run_tests.py
```

Run each command only after the preceding one succeeds. Include the generated
`sc_assistant/static/css/ui.utilities.css` and intended source changes in the
reviewed release commit.

Inspect the Docker build context. `.env` files are excluded, but that is not a
guarantee that private student exports or old application JSON files are excluded.
Do not build a release containing credentials or unapproved personal data.

> **Checkpoint 01:** Required source changes and their tests are complete, the old
> deployment workflow is disabled, and the release contains no secrets or private
> exports. This guide does not apply these changes for you.

---

## 02. Account Access

**Where:** AWS Console, then Windows PowerShell.

**Goal:** Make every later action target the new account, not the old account.

1. Sign into the new AWS account and record its 12-digit account ID.
2. Enable MFA on the AWS root account. Do not create root access keys.
3. Use an administrative IAM Identity Center identity or administrative role for
   provisioning. The application will receive a different, restricted role.
4. Select **Asia Pacific (Singapore) - ap-southeast-1** in the console.
5. Create an AWS Budget with email alerts you will monitor.
6. Install AWS CLI v2 on your PC and configure the profile `sc-new` for an
   authorized deployment identity in this account.

If your administrator has configured IAM Identity Center, use its start URL and
SSO region with the interactive CLI setup:

```powershell
aws configure sso --profile sc-new
aws sso login --profile sc-new
aws sts get-caller-identity --profile sc-new
```

If your organization uses another approved role-based authentication method,
configure that method instead. Do not reuse the old application's access keys.

**Success looks like:** `get-caller-identity` returns your new account ID.

### Which Credentials Are Which?

| Credential | Purpose | Where it belongs |
|---|---|---|
| Your setup/deployment identity | Create resources and push images | AWS Console / local AWS CLI profile |
| EC2 instance role | Let the running app access AWS | Attached to EC2; SDKs obtain credentials automatically |
| Cognito client secret | Authenticate this backend app client | Secrets Manager, then the private runtime environment |
| Pinecone/OpenRouter API keys | Access external providers | Secrets Manager, then the private runtime environment |
| Student username/password | Sign into the application | The application's authentication flow, not AWS configuration |

The deployed container must not receive static `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, or `AWS_SESSION_TOKEN` values. Its instance role provides
and refreshes the complete temporary credential set automatically.

> **Checkpoint 02:** You can identify the new account through the CLI, have
> administrative console access, and have budget alerts configured.

---

## 03. Data Stores

**Where:** AWS Console in `ap-southeast-1`.

**Goal:** Create resource schemas without accidentally importing old or demo data.

### 3A. Create the Bucket

1. Open **S3 -> Create bucket**.
2. Choose a globally unique name from your worksheet.
3. Choose `ap-southeast-1`.
4. Keep **Block all public access** enabled.
5. Use **Bucket owner enforced** object ownership.
6. Keep default encryption enabled. SSE-S3 is sufficient for this baseline guide.
7. Enable versioning if you need recovery from accidental document replacement.
8. Create the bucket and record its exact name.

If you already created `sc-assistant-bucket3` in the new account, it can be your
chosen bucket. Verify ownership and region rather than creating duplicates.

The app uses server-side uploads and presigned GET URLs. It does not need public
ACLs, website hosting, or a broad S3 CORS policy for those operations.

### 3B. Create the Tables

For each row below, open **DynamoDB -> Tables -> Create table**, enter the exact
keys, and select **On-demand** capacity. Every listed key attribute is **String**.

| Table | Partition key | Sort key |
|---|---|---|
| `Conversations` | `conv_id` | `uid` |
| `Files` | `filename` | None |
| `SCAssistantReports` | `report_id` | None |
| `StudentRecords` | `student_id` | None |
| `SCAssistantStores` | `store` | None |

Do not create `Conversations` with only `conv_id`. The application supplies both
`conv_id` and `uid` when accessing an item.

After the tables exist, open each relevant table's **Indexes** tab and create:

| Table | Index name | Partition key | Sort key | Projection |
|---|---|---|---|---|
| `Conversations` | `uid-index` | `uid` | `updated_at` | ALL |
| `SCAssistantReports` | `status-created-index` | `status` | `created_at` | ALL |
| `StudentRecords` | `email-index` | `email` | None | ALL |
| `StudentRecords` | `student-number-index` | `student_number` | None | ALL |

Only key/index attributes need schema declarations. Other item attributes are
written by the application. Wait for every table and index to become **ACTIVE**.
Enable point-in-time recovery and deletion protection for production data as
appropriate to your retention policy and budget.

> **Do not run demo imports:** `seed_students.py` inserts synthetic records;
> `link_students.py` manipulates dummy accounts and uses a shared demo password;
> `create_stores_table.py` also imports local JSON. They are not safe substitutes
> for schema-only setup. Some of these scripts ignore arguments such as
> `--help` or `--dry-run` and still perform their normal work.

> **Checkpoint 03:** The private bucket and five correctly keyed tables exist in
> the new account and region. All four indexes are ACTIVE with ALL projection.
> Empty tables and missing unused admin-store rows are normal at this stage.

---

## 04. Cognito

**Where:** AWS Console in `ap-southeast-1`.

**Goal:** Configure accounts for the application's own sign-in/signup forms.

### 4A. Create the User Pool

1. Open **Amazon Cognito -> User pools** and create the application's user pool.
2. Choose a **Traditional web application** when asked for application type.
3. Enable user-chosen **usernames with email aliases**, not an email-only pool.
4. Enable self-registration and automatic email verification.
5. Choose email verification by **code** rather than verification link.
6. Configure the standard `email` and `name` attributes; the app supplies both.
7. Keep the password policy aligned with the application's validation and your
   institution's security requirements. Do not lower an existing requirement.
8. Record the user-pool ID.

The desired API configuration uses `AliasAttributes` containing `email`, rather
than an email-only `UsernameAttributes` configuration. Sign-in options and required
attributes are important creation-time choices. Do not delete a pool with real
users merely to correct an initial configuration mistake.

This is a **User Pool**. No Cognito Identity Pool is needed by the current backend.

### 4B. Add Custom Attributes

Open the pool's custom-attribute settings. Add the following missing attributes:

| Name entered in the console | Type | Mutable | Minimum | Maximum |
|---|---|---|---:|---:|
| `role` | String | Yes | 1 | 16 |
| `data_consent` | String | Yes | 1 | 8 |
| `student_id` | String | Yes | 1 | 64 |

Enter `student_id`, not `custom:student_id`; Cognito adds the prefix. Do not use
Number for the school ID, because that can lose leading zeros. Do not mark a
custom attribute as a required standard attribute; the app validates the ID.
Custom-attribute definitions cannot simply be deleted or renamed afterward.

### 4C. Configure the Client

Open **Applications -> App clients** and select the backend app client:

| Setting | Required value |
|---|---|
| Client type | Confidential client with a generated client secret |
| Password sign-in | `ALLOW_USER_PASSWORD_AUTH` |
| Refresh-token settings | Preserve a compatible configuration; do not enable conflicting rotation/legacy refresh options |
| Prevent user existence errors | Enabled where available |

The app calls `InitiateAuth`. Enabling only the administrative password flow or
only SRP is not sufficient. It uses its own pages and Boto3, so managed login,
an OAuth callback, and a Cognito hosted-login domain are not required for this path.

Record the client ID. Store the client secret directly in the runtime secret in
Phase 06, not in this document or browser JavaScript.

### 4D. Set Attribute Permissions

In the app client's attribute permissions, explicitly configure:

| Attribute | Read | Write |
|---|---|---|
| `email` | Yes | Yes |
| `email_verified` | Yes | No |
| `name` | Yes | Yes |
| `custom:data_consent` | Yes | Yes |
| `custom:student_id` | Yes | Yes |
| `custom:role` | Yes | **No** |

Cognito returns `sub` as the stable user identifier; never make it user-writable.
Keep `custom:role` read-only on **every user-facing client** in the pool, not just
one client. Administrative changes use the trusted backend/admin APIs instead.

> **STOP if Phase 01E is incomplete in your deployed release:** Older signup
> helpers send `custom:role`. The release must omit that attribute before this
> client permission configuration can work. Do not grant users role-write access
> as a shortcut.

### 4E. Configure Email Recovery

Signup verification and native password recovery use separate Cognito operations:

| Feature | Email requirement |
|---|---|
| Signup email confirmation | Cognito's built-in sender can work, subject to limits |
| Password recovery with `ForgotPassword` / `ConfirmForgotPassword` | Verified recovery email and self-service email recovery enabled; Cognito's built-in sender can work, subject to limits |

The user selects **Send OTP** beside the email field to request a code with
`ForgotPassword`, then submits the six-digit code, new password, and password
confirmation together on the same page using **Reset password**. The same email
button becomes **Resend OTP** after a request; changing the email requires an OTP
for that address. Password drafts are not included in the OTP request. After local form
validation, `ConfirmForgotPassword` validates the code and resets the password
atomically. There is no separate `/verify-reset-code` step, `USER_AUTH` /
`EMAIL_OTP` authentication proof, or recovery `AdminSetUserPassword` call.

1. Confirm that self-service recovery allows verified email, rather than being
   admin-only, and that the account has a verified, eligible recovery address.
2. Check delivery quotas for the configured sender. Cognito's default sender is
   supported; native recovery does not require SES or an Essentials/Plus tier.
3. If SES is already configured, its sender-verification requirements and sandbox
   restrictions still apply. While in the sandbox, use only SES-verified
   recipients for controlled testing; ordinary unverified recipients require SES
   production access. Do not assume successful signup alone proves recovery is
   configured correctly.

Already configured SES and authentication flows may remain. Keep
`ALLOW_USER_PASSWORD_AUTH` for login and compatible existing refresh settings;
native recovery does not require `ALLOW_USER_AUTH` or `EMAIL_OTP`. Restoring this
flow is not a reason to automatically change AWS resources, sender, tier, or MFA.

**MFA boundary:** The current sign-in forms do not implement Cognito MFA
challenges. Native recovery does not change that login limitation or bypass
recovery-channel restrictions under the pool's MFA settings. If application-user
MFA is required by your security policy, implement compatible login challenge
handling before public launch. Do not disable an established requirement to make
a test pass. AWS root and administrator MFA must remain enabled regardless.

> **Checkpoint 04:** The pool/client IDs are recorded, the three attributes exist,
> the client has a secret and correct flows, and role writes are not exposed to
> users. Email delivery prerequisites and any MFA deployment blocker are understood.

---

## 05. External Providers

**Where:** Pinecone and OpenRouter dashboards.

**Goal:** Make the application's non-AWS dependencies available.

### Pinecone

Create or deliberately reuse an authorized Pinecone project/index:

| Setting | Value for the current embedding model |
|---|---|
| Index name | `rag-database-2026`, unless changed consistently |
| Dimension | `384` |
| Metric | `dotproduct` |
| Deployment | Serverless |
| Existing builder's region | Pinecone AWS `us-east-1`, independent of the app's `AWS_REGION` |

Pinecone is not a table in your AWS account. Reusing its old key/project also
reuses the existing vector corpus; creating a new AWS account does not isolate it.
An empty compatible index is sufficient to start the app, not to answer useful
document-grounded questions.

### OpenRouter

Create an authorized API key, configure a spending limit, add credit if needed,
and confirm access to the models named in `config.py`. Current chat/vision paths
use OpenRouter. Do not buy another provider solely because an unused key variable
still appears in the repository.

The Docker build downloads the embedding/reranker model assets. Do not skip those
downloads or replace the embedding model without a compatible index plan.

> **Checkpoint 05:** A compatible Pinecone index exists and both provider keys are
> available for Secrets Manager. No private student exports have been uploaded to
> the general knowledge corpus.

---

## 06. Runtime Secret

**Where:** AWS Console -> Secrets Manager.

**Goal:** Store backend configuration without embedding credentials in the image.

1. Choose **Store a new secret -> Other type of secret**.
2. Use the default managed encryption key for this baseline, unless your
   organization requires a customer-managed key.
3. Use a flat JSON object with single-line string values, as shown below.
4. Name the secret **`sc-assistant/prod`**.
5. Record its full ARN for the EC2 runtime policy.

Generate a new, strong `FLASK_SECRET_KEY` in a private local terminal if necessary:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Put that output directly into Secrets Manager. Do not paste secrets into chat,
commit them, or fill them into this guide. Keep the Flask secret stable across
workers and normal releases; changing it invalidates existing sessions.

**JSON template - replace every applicable placeholder in the AWS editor:**

```json
{
  "AWS_REGION": "ap-southeast-1",
  "AWS_DEFAULT_REGION": "ap-southeast-1",
  "S3_BUCKET_NAME": "REPLACE_WITH_BUCKET_NAME",
  "PINECONE_INDEX_NAME": "rag-database-2026",
  "PINECONE_API_KEY": "REPLACE_WITH_PINECONE_KEY",
  "OPENROUTER_API_KEY": "REPLACE_WITH_OPENROUTER_KEY",
  "COGNITO_USER_POOL_ID": "REPLACE_WITH_POOL_ID",
  "COGNITO_CLIENT_ID": "REPLACE_WITH_CLIENT_ID",
  "COGNITO_CLIENT_SECRET": "REPLACE_WITH_CLIENT_SECRET",
  "FLASK_SECRET_KEY": "REPLACE_WITH_NEW_RANDOM_SECRET",
  "STORE_BACKEND": "dynamodb",
  "STORE_TABLE_NAME": "SCAssistantStores",
  "SC_RUNTIME_DB": "/var/lib/sc-assistant/runtime.sqlite3",
  "PASSWORD_RESET_STATE_PATH": "/var/lib/sc-assistant/password-reset.sqlite3",
  "RATE_LIMIT_ENABLED": "true",
  "RATE_LIMIT_STORAGE_PATH": "/var/lib/sc-assistant/request-limits.sqlite3",
  "RATE_LIMIT_PROXY_HOPS": "1",
  "SESSION_COOKIE_SECURE": "true",
  "FORWARDED_ALLOW_IPS": "*",
  "GUNICORN_CMD_ARGS": "--keep-alive 190",
  "RERANKER_ENABLED": "false",
  "RAG_DEBUG": "false",
  "WEB_INDEXING_ENABLED": "true"
}
```

Do not add AWS access keys, a session token, `AWS_PROFILE`, or
`AWS_EC2_METADATA_DISABLED=true`. EC2 role credentials must remain available.

Keep `PASSWORD_RESET_STATE_PATH` on the private `sc-assistant-state` volume.
`ResetFlowStore` still needs SQLite for **180-second** request coordination,
**30-second** resend limits, stable target-account binding, and duplicate-submit
protection across workers. It is not an OTP-verified admin-write grant, and stores
no passwords, codes, or provider tokens. Cognito validates the code and resets the
password atomically; the app's short request expiry is separate from Cognito's
code-validity rules. Losing the state file requires outstanding requests to start
again with a new code.

`WEB_INDEXING_ENABLED=true` enables administrator document uploads/indexing in
the running web application. It is required for Phase 12B and deliberately
overrides the Docker image's disabled default. This is not a public web-crawling
switch. Perform initial ingestion during the restricted pilot and avoid large
indexing batches during busy chat periods. Keep it true if administrators need
ongoing uploads; set it false only when deliberately pausing ingestion or using
an approved offline indexing process.

`FORWARDED_ALLOW_IPS=*` is safe for this topology only when port 8080 is restricted
to the trusted ALB security group as specified in Phase 09. It is not a safe
setting for an unrestricted, directly exposed Gunicorn server.

### Request-Frequency Limits

`sc_assistant/rate_limit.py` applies **default-on, fixed-window request-frequency
limits**, with each window starting at the first request for that rule/identity.
Keep `RATE_LIMIT_ENABLED=true`; `false` explicitly disables this layer. The
[exact defaults and rule scopes](../README.md#request-frequency-limits) cover
API reads, writes, chat, auth, feedback/reports, uploads/scans, and status polling.
Existing chat concurrency caps and password-recovery coordination/resend
cooldowns remain separate and unchanged.

The template sets
`RATE_LIMIT_STORAGE_PATH=/var/lib/sc-assistant/request-limits.sqlite3` on the
private `sc-assistant-state` volume mounted in Phase 11. Keep it private and
writable by the app; workers must share that file and the stable Flask secret.
The setting is optional: without it, the limiter uses `<stem>.rate-limits.sqlite3`
beside `SC_RUNTIME_DB`, else beside `PASSWORD_RESET_STATE_PATH`, or a private
system-temp directory if neither is set. The explicit persistent path above
avoids relying on temporary/container-local storage across replacement.

`RATE_LIMIT_PROXY_HOPS` defaults to `0`, which does not trust forwarded client IPs.
The template deliberately sets `"RATE_LIMIT_PROXY_HOPS": "1"` **only for this
guide's network-restricted, ALB-only backend traffic**. Keep the Phase 09 rule
allowing port 8080 only from the ALB security group, and keep the ALB's
`X-Forwarded-For` processing in append mode. The limiter trusts the **rightmost
address appended by the ALB**, not a caller-supplied leftmost address. Do not use
`1` for a directly exposed backend. `FORWARDED_ALLOW_IPS` handles Gunicorn's
forwarded-scheme trust; even `*` is not enough for client-IP attribution.

Guest requests and auth routes have IP-based quotas, which may need tuning for
many students behind a shared campus NAT. Authenticated activity outside auth
routes uses `uid` (or the signed-in user identifier if absent). Login additionally
has a normalized (trimmed, case-folded) username/email-target-plus-IP budget;
raising `login` alone leaves `login_target` unchanged. Resetting sessions does
not reset the guest IP quota.

`RATE_LIMIT_RULES` accepts a **partial JSON map** of rule names to
`[count, seconds]`, both positive integers. Omitted rules retain their defaults;
unknown names, malformed JSON, and invalid values fail startup. To illustrate
tuning only guest-chat and login IP budgets, merge this property into the full
runtime secret, **not as a replacement for the whole secret**. Its value must
remain a single-line JSON-encoded string for the Phase 11 environment conversion:

```json
{
  "RATE_LIMIT_RULES": "{\"guest_chat\":[12,60],\"login\":[120,60]}"
}
```

Quota exhaustion returns HTTP `429` JSON with `"success": false`, `message`,
`error`, `"code": "rate_limited"`, and integer `retry_after` seconds. Responses
include `Retry-After` and `Cache-Control: no-store`. A quota-storage failure
**fails closed with HTTP `503`** for protected work, with the same fields/headers,
`"code": "rate_limit_storage_unavailable"`, and a three-second retry. Rejections
happen before downstream provider/database/indexing calls. `/health`, static/PWA
resources, `OPTIONS`, and ordinary page navigation remain unaffected by this
limiter, so healthy ALB checks do not prove quota storage is writable. Persisted
quota keys are HMAC digests, never raw IPs, emails, or passwords.

This layer does not replace WAF/network protection, payload-size limits, or work
concurrency caps. Retain the ALB-only ingress restrictions when opening public
access, and retain the private named volume during controlled replacements.

> **Checkpoint 06:** The secret contains real deployment values, no unreplaced
> placeholders, no static AWS credentials, and a non-default Flask secret. Its
> full ARN is recorded without recording its secret values.

---

## 07. Registry and Role

**Where:** AWS Console -> ECR and IAM.

**Goal:** Separate the identity that publishes images from the identity that runs
the application.

### 7A. Create ECR

1. Open **ECR -> Private repositories -> Create repository**.
2. Name it **`sc-assistant`** in `ap-southeast-1`.
3. Enable immutable tags and appropriate vulnerability scanning.
4. Record the repository URI.

### 7B. Create the Runtime Role

1. Open **IAM -> Roles -> Create role**.
2. Choose trusted entity **AWS service**, use case **EC2**.
3. Attach **AmazonSSMManagedInstanceCore**.
4. Name the role **`SC-Assistant-Runtime`**.
5. Create a customer-managed permissions policy using the JSON below.
6. Replace all account, pool, bucket, and secret placeholders before saving.
7. Attach that policy to `SC-Assistant-Runtime`.

**This is an EC2 IAM policy, not a Cognito app-client permission document.**

<details>
<summary><strong>Expand: EC2 runtime policy JSON</strong></summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadApplicationSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "REPLACE_WITH_RUNTIME_SECRET_ARN"
    },
    {
      "Sid": "GetEcrLoginToken",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PullApplicationImage",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "arn:aws:ecr:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:repository/sc-assistant"
    },
    {
      "Sid": "ApplicationTables",
      "Effect": "Allow",
      "Action": [
        "dynamodb:DescribeTable",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
        "dynamodb:BatchWriteItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/Conversations",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/Conversations/index/uid-index",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/Files",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/SCAssistantReports",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/SCAssistantReports/index/status-created-index",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/StudentRecords",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/StudentRecords/index/email-index",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/StudentRecords/index/student-number-index",
        "arn:aws:dynamodb:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:table/SCAssistantStores"
      ]
    },
    {
      "Sid": "ListApplicationBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::REPLACE_WITH_BUCKET_NAME"
    },
    {
      "Sid": "ApplicationDocuments",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::REPLACE_WITH_BUCKET_NAME/*"
    },
    {
      "Sid": "ApplicationUserManagement",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:ListUsers",
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminUpdateUserAttributes",
        "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminDeleteUser"
      ],
      "Resource": "arn:aws:cognito-idp:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:userpool/REPLACE_WITH_POOL_ID"
    }
  ]
}
```

</details>

Keep `cognito-idp:AdminSetUserPassword` in this overall application policy:
`sc_assistant/settings.py` still uses it for signed-in password changes. Native
`ForgotPassword` / `ConfirmForgotPassword` recovery no longer needs that action.

The ECR login-token action requires `Resource: "*"`; image access is restricted
separately. This policy deliberately does not let the app create tables, change
pool configuration, or push images. Customer-managed KMS keys require additional
scoped key permissions and key-policy configuration; do not add broad KMS access.

Do not copy [AWS IAM Policies.txt](AWS%20IAM%20Policies.txt) unchanged: it contains
historical account/bucket references and mixes provisioning with runtime access.

### 7C. Allow Image Publishing

Your local `sc-new` identity needs image-push access. It must not obtain this by
attaching broad administrative permissions to the EC2 runtime role.

If the local identity is already an authorized setup administrator, it may already
have access. For a restricted publisher, use a policy like the following on its
IAM role or Identity Center permission set, according to your account design:

<details>
<summary><strong>Expand: ECR publisher policy JSON</strong></summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GetEcrLoginToken",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PublishApplicationImage",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:DescribeImages"
      ],
      "Resource": "arn:aws:ecr:ap-southeast-1:REPLACE_WITH_ACCOUNT_ID:repository/sc-assistant"
    }
  ]
}
```

</details>

> **Checkpoint 07:** The private ECR repository exists, the runtime role trusts
> EC2 and has scoped application access, and the separate publisher can push images.

---

## 08. Build Image

**Where:** Windows PowerShell, in the prepared release checkout.

**Goal:** Get a successfully built, versioned Linux image into ECR before paying
for an EC2 instance to run it.

Use Docker Desktop with Linux containers. Allow substantial free Docker disk
space and memory; the build installs ML packages and downloads model weights.
CloudShell is not an appropriate place for this large image build.

Replace the account-ID placeholder. Choose a new tag for each release; do not
overwrite an immutable tag or use `latest` as your only rollback reference.

```powershell
$Profile = "sc-new"
$Region = "ap-southeast-1"
$AccountId = "REPLACE_WITH_ACCOUNT_ID"
$Tag = "release-001"

if ($AccountId -notmatch '^\d{12}$') {
    throw "Replace AccountId with the new 12-digit account ID."
}

$ActualAccount = aws sts get-caller-identity --profile $Profile --query Account --output text
if ($LASTEXITCODE -ne 0 -or $ActualAccount.Trim() -ne $AccountId) {
    throw "AWS credentials failed or point to the wrong account. Stop here."
}

$Registry = "${AccountId}.dkr.ecr.${Region}.amazonaws.com"
$Image = "${Registry}/sc-assistant:${Tag}"

$EcrPassword = aws ecr get-login-password --region $Region --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "Could not obtain an ECR login token." }
$EcrPassword | docker login --username AWS --password-stdin $Registry
$LoginExitCode = $LASTEXITCODE
Remove-Variable EcrPassword
if ($LoginExitCode -ne 0) { throw "Docker login failed." }

docker build --platform linux/amd64 -t $Image .
if ($LASTEXITCODE -ne 0) { throw "Image build failed. Do not deploy." }

docker push $Image
if ($LASTEXITCODE -ne 0) { throw "Image push failed. Do not deploy." }

$Image
```

**Success looks like:** the build's dependency checks pass, push succeeds, and the
same tag appears in the new account's ECR repository. Record the full printed image
URI. Check scan findings before deployment.

> **Checkpoint 08:** A known release image exists in the correct ECR repository.
> A passing unit suite alone is not a substitute for a successful Docker build.

---

## 09. Launch EC2

**Where:** AWS Console -> VPC, then EC2.

**Goal:** Launch a single correctly sized host without exposing its backend port.

### 9A. Network and Security Groups

Use the default VPC if it exists and is appropriate, or create a VPC with:

- Two public subnets in different Availability Zones for the ALB.
- An internet gateway and public-subnet routes to it.
- DNS support enabled.
- Outbound connectivity for ECR, SSM, Cognito, S3, Pinecone, and OpenRouter.

For this baseline, EC2 occupies a public subnet and has a public IPv4 address for
outbound access. Its inbound rules still restrict the application to the ALB.
An isolated private subnet without NAT/endpoints and provider internet access
will not work for this app.

Create these security groups in the same VPC:

| Group | Inbound rule | Source |
|---|---|---|
| `sc-assistant-alb` | TCP 443 | Your approved tester public IP addresses during the pilot |
| `sc-assistant-alb` | TCP 80 | The same tester addresses, for HTTPS redirection |
| `sc-assistant-app` | TCP 8080 | **The `sc-assistant-alb` security group**, not a public CIDR |

The AWS console's **My IP** option can fill your current public IPv4 `/32` for
pilot access. Add a separate approved tester address if necessary. An
internet-facing ALB can still be restricted to these sources.

**Do not expose 8080 or SSH to the internet.** Use Session Manager for administration.
The ALB must be able to reach the app on 8080; EC2 needs outbound provider access.
Public launch access is a later, deliberate change in Phase 12.

### 9B. Instance Settings

Open **EC2 -> Launch instance** and use:

| Setting | Initial choice |
|---|---|
| Name | `sc-assistant-prod` |
| AMI | Amazon Linux 2023, **x86_64**, standard image |
| Instance type | `t3.large` or comparable, at least **8 GiB RAM** |
| Root storage | Approximately 80 GiB encrypted gp3; monitor actual usage |
| Network | The selected public subnet, with public IPv4 for outbound access |
| Security group | `sc-assistant-app` only |
| IAM instance profile | `SC-Assistant-Runtime` |
| Instance metadata | Enabled, IMDSv2 required, response hop limit **2** |

The current image runs three Gunicorn workers, each loading local ML components.
A 1 GiB micro instance is unsuitable. The suggested size is a starting point,
not a measured concurrency guarantee; load-test and monitor CPU-credit costs.

The metadata hop limit allows the Docker container to obtain role credentials.
Do not solve metadata problems by enabling IMDSv1 or injecting permanent keys.

After launch, wait for instance status checks to pass, then use
**Connect -> Session Manager** with your authorized setup identity.

> **Checkpoint 09:** Session Manager opens a shell on the new instance, its role
> is attached, and only the ALB security group can reach backend port 8080.

---

## 10. Configure HTTPS

**Where:** AWS Console and your DNS provider.

**Goal:** Give the application a trusted HTTPS URL before testing authentication.

1. Choose a hostname you control, such as `assistant.example.edu`.
2. In **ACM** in `ap-southeast-1`, request a public certificate for that hostname.
3. Add the DNS validation record at your DNS provider and wait for **Issued**.
   Keep that record for certificate renewal.
4. In **EC2 -> Target groups**, create an **Instances** target group with HTTP,
   port `8080`, and the same VPC.
5. Use health-check path **`/health`** and success code `200`.
6. Register the EC2 instance on port `8080`.
7. Create an **internet-facing Application Load Balancer** in the two public
   subnets and attach `sc-assistant-alb`.
8. Add HTTPS listener `443`, select the ACM certificate, and forward to the
   target group. Use a current TLS policy allowing TLS 1.2/1.3.
9. Make HTTP listener `80` **redirect to HTTPS**, not forward to the app.
10. Set the ALB connection idle timeout to **180 seconds** as an initial value.
11. Point the application hostname to the ALB using a Route 53 alias, or an
    appropriate subdomain CNAME at your DNS provider.

The target can remain unhealthy until Phase 11 starts the container. The runtime
secret sets Gunicorn's backend keep-alive to 190 seconds and trusts forwarded
scheme headers only because network access to the backend is restricted.

> **Do not substitute a raw IP:** You need a domain/subdomain you control for this
> certificate setup. The Flask `--https` development mode and self-signed
> certificates are not the production TLS solution.

> **Checkpoint 10:** ACM has issued the certificate, DNS points to the ALB, and
> the listeners/target group are configured. An unhealthy target is expected only
> until the app has started successfully.

---

## 11. Start Application

**Where:** The EC2 Session Manager terminal. These are **Linux/Bash** commands.

**Goal:** Retrieve secrets privately and run the image with durable local state.

### 11A. Prepare the Host

```bash
sudo dnf install -y docker jq
sudo systemctl enable --now docker
aws --version
aws sts get-caller-identity
```

Use AWS CLI v2. If it is missing from your chosen AMI, install it using the
[official CLI instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
The identity must belong to the new account and reflect the intended instance role.
Do not run `aws configure` with permanent application keys on this host.

### 11B. Retrieve the Runtime Secret

This block prints no secret values. It creates a private configuration file
outside the source checkout and Docker image.

```bash
set -euo pipefail
REGION="ap-southeast-1"

sudo install -d -m 700 /etc/sc-assistant
sudo install -m 600 /dev/null /etc/sc-assistant/runtime.env

aws secretsmanager get-secret-value \
  --secret-id sc-assistant/prod \
  --region "$REGION" \
  --query SecretString \
  --output text |
  jq -er 'to_entries[] | "\(.key)=\(.value)"' |
  sudo tee /etc/sc-assistant/runtime.env >/dev/null

sudo test -s /etc/sc-assistant/runtime.env
sudo stat -c '%a %U %n' /etc/sc-assistant/runtime.env
```

**Success looks like:** the commands succeed and `stat` shows mode `600`, owned
by root. Do not display the file with `cat` or include it in support logs.
If retrieval fails, stop; do not start a container with an empty configuration.

### 11C. Pull and Run

Replace the account-ID placeholder and use the exact tag pushed in Phase 08.
Run this block once for the first launch; it intentionally fails if a container
named `sc-assistant` already exists rather than silently deleting it.

```bash
set -euo pipefail
REGION="ap-southeast-1"
ACCOUNT_ID="REPLACE_WITH_ACCOUNT_ID"
IMAGE_TAG="release-001"

if [[ ! "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "Replace ACCOUNT_ID with the new 12-digit account ID." >&2
  exit 1
fi

ACTUAL_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$ACTUAL_ACCOUNT" != "$ACCOUNT_ID" ]]; then
  echo "Wrong AWS account. Stop and check the instance role." >&2
  exit 1
fi

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/sc-assistant:${IMAGE_TAG}"

aws ecr get-login-password --region "$REGION" |
  sudo docker login --username AWS --password-stdin "$REGISTRY"

sudo docker pull "$IMAGE"
sudo docker volume create sc-assistant-state

sudo docker run -d \
  --name sc-assistant \
  --restart unless-stopped \
  --init \
  --stop-timeout 130 \
  --publish 8080:8080 \
  --env-file /etc/sc-assistant/runtime.env \
  --mount type=volume,source=sc-assistant-state,target=/var/lib/sc-assistant \
  --log-driver local \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  "$IMAGE"
```

The image already starts Gunicorn. Do not replace its command with `python run.py`.
The named volume is for the SQLite paths configured in Phase 06, not a substitute
for S3, DynamoDB, or the matching BM25 artifact in the release.

### 11D. Check Startup

```bash
sudo docker ps --filter name=sc-assistant
sudo docker logs --tail 100 sc-assistant
curl --fail http://127.0.0.1:8080/health
```

Give the workers time to initialize. If the first health request fails during
startup, inspect logs and retry after initialization; do not blindly keep
restarting the container. Do not share raw logs if they contain personal data.

Wait for the ALB target to become **Healthy**, then open your application's
**HTTPS hostname** from an allowed tester network.

> **Secure-cookie reminder:** Do not test login at `http://EC2_IP:8080`. Backend
> HTTP is for the ALB and local health checks. Authentication uses the HTTPS URL.

> **Checkpoint 11:** The container stays running, local `/health` returns `OK`,
> the ALB target is Healthy, and the trusted HTTPS page loads from a permitted IP.
> This is web-process availability, not yet proof that every backend works.

---

## 12. Test and Launch

**Where:** Browser and AWS Console, with deliberate test accounts/documents.

**Goal:** Prove useful, private, persistent behavior before admitting real users.

### 12A. Create the Initial Administrator

1. Register your own account through the application's HTTPS signup page.
2. Choose a username distinct from your numeric school ID and verify the email
   normally. Do not bypass verification to fix a configuration error.
3. With an authorized AWS administrator identity, open that confirmed user in
   Cognito and set `custom:role` to `admin`.
4. Sign out of the application and sign back in so the new claim is loaded.
5. Confirm that the administrator dashboard is available.

Do not use the demo account-linking script or a shared temporary/demo password to
bootstrap real administrators. An ordinary self-signup must never grant admin access.

### 12B. Populate Approved Knowledge

1. Upload one small, approved, non-sensitive document through the admin dashboard.
2. Confirm that indexing completes, the `Files` catalog shows it, and the original
   object is present in the new S3 bucket.
3. Ask a question answered by that document and check the answer and citation.
4. Open the citation and confirm it shows the correct original.
5. Continue with the approved corpus only after this end-to-end test succeeds.

Keep the same `bm25_values.json` encoding artifact for indexing and retrieval.
Do not mix vectors from one encoder/corpus process with another encoder artifact.

Important limits of the existing paths:

- Admin upload performs indexing, catalog creation, and S3 upload as separate
  operations. A failed operation can leave partial state that needs inspection.
- Uploading a document directly to S3 does not index it.
- `python store_index.py` fits BM25 and upserts a local corpus; it does not upload
  originals, populate `Files`, or remove all obsolete vectors automatically.
- Plan a full corpus rebuild, encoder replacement, and image update together.
  Do not run the standalone builder casually against a populated live index.
- Private student exports do not belong in the general document corpus, even
  though the indexer's file formats include CSV.

### 12C. Complete the Acceptance Checks

- [ ] Signup stores the chosen username and preserves the numeric ID's leading zeros separately.
- [ ] Email confirmation works; unconfirmed accounts are not authenticated.
- [ ] Both username and email login work.
- [ ] Password recovery requests a `ForgotPassword` email and completes with the six-digit code, new password, and confirmation submitted together.
- [ ] Incorrect/expired reset codes and mismatched password confirmation do not change the password; no separate code-verification step is required.
- [ ] A normal user cannot access administrative pages or another student's records.
- [ ] Authenticated conversations survive a page reload and a controlled container restart.
- [ ] Calendar/announcement changes persist in DynamoDB after that restart.
- [ ] A document answer is grounded in the approved source and its citation opens.
- [ ] HTTPS, secure cookies, mobile layout, and streamed responses behave correctly.
- [ ] Startup/request logs show no credential, missing-table, missing-index, or configuration errors.
- [ ] Runtime storage, log rotation, backups, and spending alerts have been checked.
- [ ] Load tests support the intended initial audience; Docker defaults are not a capacity guarantee.
- [ ] The institution has approved the account security/MFA policy and data handling.

### 12D. Open Intended Access

Only after the checks and security approval, change the ALB security group's
tester-only rules to the intended audience. For an approved public service, that
can mean IPv4 `0.0.0.0/0` on 443 and 80. Configure IPv6 separately only if intended.
**The EC2 8080 rule must remain restricted to the ALB security group.**

Set appropriate CloudWatch alarms for instance status, unhealthy ALB targets,
and server errors. Review log collection/retention, provider spending limits,
and backups before increasing traffic.

> **Checkpoint 12:** Functional and security acceptance checks pass. `/health`
> alone is not a launch approval. One EC2 instance is still a single point of failure.

---

## 13. Future Deployments

**Where:** Windows build environment, EC2 Session Manager, and eventually GitHub.

### Manual Update

1. Build/test a new reviewed release using a new immutable tag.
2. Push it to ECR and record the previous known-good image URI and configuration.
3. Schedule the short maintenance window expected with this single-instance design.
4. Drain traffic as appropriate, allowing in-flight requests to finish.
5. Stop and remove only the application container:

```bash
sudo docker stop --time 130 sc-assistant
sudo docker rm sc-assistant
```

6. Use Phase 11C with the new image tag and the **same** named volume.
7. Repeat health and functional checks before considering the update complete.
8. If it fails, restore a tested compatible image/configuration using that volume.

Do not remove `sc-assistant-state` as part of an update. Named volumes persist
across container replacement, but require an appropriate backup/EBS recovery plan
to survive instance or disk loss. Do not run two memory-heavy copies on the same
small instance and assume you have a safe blue/green deployment.

### Runtime Setting Changes

Changing a value in Secrets Manager does not change an existing container:

1. Update the intended value in the runtime secret.
2. Successfully repeat Phase 11B to retrieve the updated environment file. Stop
   if retrieval fails; do not replace the running container with an empty file.
3. Follow the controlled replacement above, using the same image tag if only
   configuration changed and retaining the same named volume.
4. Repeat the affected functional checks.

`docker restart` alone reuses the container's original environment. This also
applies when enabling or pausing `WEB_INDEXING_ENABLED` after the first launch.

### CI/CD After the Manual Deployment

Replace the old workflow rather than merely changing its secrets:

1. Build/test on a GitHub-hosted runner with adequate disk space.
2. Use GitHub OIDC to assume a narrowly scoped deployment role.
3. Restrict trust to the repository's actual OIDC subject and the approved
   branch/environment. Newer repositories can include immutable IDs in that subject.
4. Push an immutable ECR tag.
5. Use SSM to deploy only to the new application's intended instance.
6. Perform graceful, app-specific replacement and verify health/rollback.
7. Require appropriate production approval before promotion.

Keep the runtime instance role separate from the publishing/deployment identity.
Never inject a GitHub job's temporary AWS credentials into the long-running app.

Avoid host-wide `docker stop`, `docker rm`, image removal, or broad pruning. On
current Docker versions, the existing prune flags primarily target unused
anonymous volumes rather than every named volume, but removing all containers
and their writable layers is unsafe regardless. Do not rely on Docker-version
differences to protect application data.

> **Checkpoint 13:** Updates affect only this app, preserve its data, and have a
> tested rollback. Do not re-enable the original generic self-hosted deployment job.

---

## Troubleshooting

Start with the symptom, not with broader permissions or weakened security.

| Symptom | What to check first | Avoid this shortcut |
|---|---|---|
| `InvalidClientTokenId` / `UnrecognizedClientException` | Verify the account and credential source. On EC2, check its role, IMDSv2/hop limit, and absence of stale AWS key variables. | Adding a made-up session token or `AdministratorAccess` |
| CLI identity is correct but an operation says `AccessDenied` | Check the specific action and new-account resource ARN, including DynamoDB index ARNs. | Reusing the old broad IAM policy |
| `custom:student_id` schema/configuration error | Check the pool attribute and app-client write permission. | Dropping the ID or using it as the username again |
| Unauthorized attribute during signup | Confirm Phase 01E removed the role write and the client can write required profile fields. | Making `custom:role` user-writable |
| Login fails immediately after signup | Complete email confirmation and verify the client ID/secret and password-auth flow. | Automatically authenticating unconfirmed users |
| Signup works but password recovery does not | Check verified recovery email, self-service recovery settings, sender quotas, any existing SES sandbox restrictions, MFA recovery-channel eligibility, and the app's 180-second request expiry. | Upgrading tier, enabling passwordless auth, or weakening MFA as a recovery workaround |
| Session Manager cannot connect | Check instance status, agent, instance profile, and outbound SSM connectivity. | Opening SSH to the world |
| Site times out only on a tester's phone/network | Check that network's public IP against the pilot ALB rules. | Opening backend port 8080 publicly |
| ALB target is unhealthy | Check container logs, local `/health`, target port/path, and ALB-to-EC2 security-group access. | Treating a running EC2 instance as a running app |
| Login loops or signup reports missing state | Use the HTTPS hostname and verify secure-cookie/forwarded-proto configuration. | Turning off cookie security in production |
| Requests return `429` / `rate_limited`, especially on campus Wi-Fi | Respect `Retry-After`; review the relevant guest/auth IP budget and ALB-only client-IP attribution in Phase 06. | Resetting guest sessions, trusting arbitrary forwarded headers, or disabling protection for everyone |
| Protected work returns `503` / `rate_limit_storage_unavailable` while `/health` works | Check the private SQLite path, named-volume mount, write permissions, and disk space. | Treating a healthy ALB target as proof of quota storage or bypassing fail-closed protection |
| Startup rejects `RATE_LIMIT_RULES` | Use recognized names and positive integer `[count, seconds]` pairs; preserve JSON string escaping in the runtime secret. | Assuming unknown or invalid rules are silently ignored |
| S3 upload/download fails | Check the configured bucket, both region variables, IAM object/bucket permissions, and reuse of the SigV4 client. | Making the bucket public |
| Admin upload returns "Document indexing is paused" | Set `WEB_INDEXING_ENABLED=true`, refresh the runtime environment file, and recreate the container as described in Phase 13. | Changing only Secrets Manager or restarting the same container |
| Dashboard is empty | Empty new tables may be normal. Also check backend selection and access errors, which can look like empty defaults. | Running demo seed/migration scripts blindly |
| Chat gives no grounded answers | Check index name, 384 dimensions, dotproduct metric, corpus population, encoder consistency, and provider access. | Assuming an empty index is a working knowledge base |
| Container exits with code 137 | Inspect memory pressure and instance sizing. | Deploying three model-loading workers on a micro instance |
| Local app works but deployment differs | Check the release tag, runtime secret, resource IDs, and persisted storage. Local `.env` is not baked into the image. | Assuming changes on your PC update a deployed container |

Safe first diagnostics on EC2:

```bash
aws sts get-caller-identity
sudo docker ps --filter name=sc-assistant
sudo docker logs --tail 100 sc-assistant
curl --fail http://127.0.0.1:8080/health
```

These check identity and process health, not every service or authorization rule.
Avoid publishing runtime environment files, raw container inspection output,
credentials, tokens, or logs containing student data when requesting help.

---

## Migration Cautions

This guide creates a fresh deployment. It is not an old-account data migration.

| Data | What a new account/pool does not do automatically |
|---|---|
| Cognito users/passwords | Does not copy old users, passwords, or stable `sub` identifiers |
| Conversations/reports | Does not remap their owner identifiers to new Cognito identities |
| Academic records | Does not verify enrollment or create records from signup fields |
| Admin decisions | Does not import calendar entries, notices, feedback, or conflict resolutions |
| S3 documents | Does not copy originals, object versions, or extracted table images |
| Pinecone | Is independent of the AWS account; reusing the project/index reuses its corpus |
| SQLite state | Named volumes do not provide multi-instance sharing or automatic host-replacement recovery |

In authoritative academic records, `StudentRecords.student_id` is the Cognito
`sub`; `student_number` is the school number. Cognito `custom:student_id` is a
self-reported signup value, not proof of ownership of an academic record.

Do not automatically link records solely from that self-entered number. Plan
authorized imports, identity mapping, duplicate-email handling, retention, and
backups separately. Keep old resources until any required migration is verified.

---

## Official References

Console labels can move. Match the resource names and API setting names in this
guide, and consult the official documentation when a screen differs.

- [AWS CLI installation](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [IAM Identity Center CLI configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
- [Cognito attribute permissions](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-attributes.html)
- [Cognito email configuration](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-email.html)
- [Cognito authentication methods](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-authentication-flow-methods.html)
- [EC2 instance metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html)
- [ECR image publishing](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)
- [ALB timeout settings](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html)
- [Gunicorn settings](https://docs.gunicorn.org/en/stable/settings.html)
- [Docker volumes](https://docs.docker.com/engine/storage/volumes/)
- [GitHub OIDC for AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)

**Completion means:** the preparation changes are applied, infrastructure is
verified, acceptance checks pass, and the intended audience is explicitly
approved. Creating this document, building an image, or receiving `OK` from
`/health` alone does not establish a completed or secure production deployment.
