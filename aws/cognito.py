import base64
import hashlib
import hmac
import os
import re
import tempfile

import boto3
from botocore.exceptions import ClientError
from jose import jwt

# Ensure you add COGNITO_CLIENT_SECRET to your config.py and .env
from config import (
    AWS_REGION,
    COGNITO_CLIENT_ID,
    COGNITO_CLIENT_SECRET,
    COGNITO_USER_POOL_ID,
)

cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)


def signup_mutation_lock(username):
    """Serialize signup changes across workers without unbounded lock files."""
    from rag.store import _file_transaction
    pool = hashlib.sha256(COGNITO_USER_POOL_ID.encode()).hexdigest()[:12]
    stripe = int(hashlib.sha256(username.lower().encode()).hexdigest()[:8], 16) % 32
    return _file_transaction(os.path.join(tempfile.gettempdir(), f"sc-signup-{pool}-{stripe}"))


def _pending_account_error(username, expected_email):
    user = cognito_client.admin_get_user(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
    attrs = {item["Name"]: item["Value"] for item in user.get("UserAttributes", [])}
    if user.get("UserStatus") != "UNCONFIRMED" or attrs.get("email_verified") == "true":
        return {"success": False, "message": "This account is already confirmed or cannot be changed here. Please sign in."}
    if (attrs.get("email") or "").lower() != expected_email.lower():
        return {"success": False, "message": "The signup email changed in another request. Refresh the page before continuing."}
    return None

COGNITO_ERROR_MESSAGES = {
    "UsernameExistsException": "This username or email is already registered. Please try logging in.",
    "AliasExistsException": "This email is already registered. Please sign in to the existing account.",
    "CodeDeliveryFailureException": "The verification email could not be delivered. Please try resending the code.",
    "TooManyFailedAttemptsException": "Too many verification attempts. Please wait before trying again.",
    "InvalidPasswordException": "Your password is not strong enough. It must be at least 8 characters long and include an uppercase letter, a lowercase letter, a number, and a special character (e.g., !@#$%).",
    # Reached only for genuinely malformed input: `resolve_cognito_username`
    # translates an email to its username first, and misconfiguration is split
    # off into CONFIG_ERROR_MESSAGE by handle_cognito_error below. The old copy
    # ("Usernames cannot be email addresses") was shown to students who typed
    # the only identifier they know, their email.
    "InvalidParameterException": "That doesn't look like a valid email address or username. Please check what you entered and try again.",

    "NotAuthorizedException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotFoundException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotConfirmedException": "Your account is not confirmed yet. Please check your email for a confirmation link.",
    "TooManyRequestsException": "You've made too many requests. Please wait a moment and try again.",
    "InternalErrorException": "An internal server error occurred. Please try again later.",
    "CodeMismatchException": "The verification code is incorrect. Please check the code and try again.",
    "ExpiredCodeException": "The verification code has expired. Please request a new one.",
    "LimitExceededException": "You have exceeded the limit for password reset attempts. Please try again later."
}

def get_secret_hash(username):
    """Generates the SecretHash required for Cognito App Clients with a Client Secret."""
    msg = username + COGNITO_CLIENT_ID
    dig = hmac.new(
        str(COGNITO_CLIENT_SECRET).encode('utf-8'),
        msg.encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(dig).decode()

def get_user_role_from_claims(id_token):
    """Decodes the user role from the JWT token."""
    decoded = jwt.get_unverified_claims(id_token)
    return decoded.get("custom:role", "user")

# InvalidParameterException is Cognito's catch-all for "this request is wrong",
# and it covers both bad user input AND server-side misconfiguration. Mapping the
# whole code to one sentence about the email address blames the student for
# problems they cannot fix and hides the real cause from us: the pool answered
# "USER_PASSWORD_AUTH flow not enabled for this client", which no amount of
# retyping an address will resolve. These substrings are matched against AWS's
# own message so operator errors read as operator errors.
COGNITO_CONFIG_ERROR_HINTS = (
    "auth flow not enabled",
    "flow not enabled",
    "not enabled for this client",
)

CONFIG_ERROR_MESSAGE = (
    "Sign-in is temporarily unavailable due to a server configuration issue. "
    "This is not a problem with your account — please contact the administrator."
)


def handle_cognito_error(e):
    """Provides a user-friendly error message for Cognito exceptions."""
    err = e.response.get("Error", {})
    err_code = err.get("Code")
    raw_message = (err.get("Message") or "").lower()

    if err_code == "InvalidParameterException" and any(
        hint in raw_message for hint in COGNITO_CONFIG_ERROR_HINTS
    ):
        # Logged in full because the student-facing copy deliberately omits the
        # detail, and without this the only trace of a broken pool is a support
        # ticket that says "it says my email is invalid".
        print(f"[cognito] CONFIGURATION ERROR — {err.get('Message')}")
        return CONFIG_ERROR_MESSAGE

    return COGNITO_ERROR_MESSAGES.get(err_code, "An unexpected error occurred. Please try again.")



def _looks_like_email(identifier):
    """Cheap check: is this an email rather than a Cognito username?

    Usernames in this pool can never contain "@" (sign_up_user rejects them),
    so a single "@" with something either side of it is enough to tell the two
    apart without a round trip to AWS.
    """
    if not identifier or "@" not in identifier:
        return False
    local, _, domain = identifier.rpartition("@")
    return bool(local) and "." in domain


def resolve_cognito_username(identifier):
    """Return the Cognito *username* for an email address, or the input as-is.

    The pool has AliasAttributes=['email'], so an email *is* accepted in the
    USERNAME slot — but only for accounts whose email was verified at the time
    the alias was assigned, and the alias silently fails to resolve otherwise.
    Looking the username up by the `email` attribute removes that dependency:
    initiate_auth always receives the canonical username, which is the one
    identifier that cannot be ambiguous.

    Students should not have to know their generated username, so we accept the
    email and translate it. Failure is deliberately silent: we return the
    original identifier, which the alias will usually still resolve, so a
    missing ListUsers IAM permission degrades to the pool's own behaviour
    instead of a new opaque failure.
    """

    if not _looks_like_email(identifier):
        return identifier

    identifier = identifier.lower()
    try:
        escaped = identifier.replace("\\", "\\\\").replace('"', '\\"')
        params = {"UserPoolId": COGNITO_USER_POOL_ID, "Filter": f'email = "{escaped}"', "Limit": 60}
        fallback, confirmed = identifier, None
        for _ in range(10):
            resp = cognito_client.list_users(**params)
            for user in resp.get("Users", []):
                username = user.get("Username")
                if not username:
                    continue
                if fallback == identifier:
                    fallback = username
                if user.get("UserStatus") == "CONFIRMED":
                    confirmed = confirmed or username
                    attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                    if attributes.get("email_verified") == "true":
                        return username
            token = resp.get("PaginationToken")
            if not token or token == params.get("PaginationToken"):
                break
            params["PaginationToken"] = token
        # An abandoned, unconfirmed signup with the same email must not shadow
        # the actual verified account during email sign-in.
        return confirmed or fallback
    except ClientError as e:
        print(f"[cognito] email lookup failed ({e.response.get('Error', {}).get('Code')}); trying the alias.")
    except Exception as e:  # noqa: BLE001 - a verified email alias can still authenticate
        print(f"[cognito] email lookup failed ({type(e).__name__}); trying the alias.")

    return identifier

def normalize_signup_email(value):
    """Validate a verification address without restricting its provider/domain."""
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")  # noqa: TRY004 - missing form input is a validation error
    email = value.strip().lower()
    local, separator, domain = email.partition("@")
    domain_pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    if (not separator or not re.fullmatch(domain_pattern, domain) or len(email) > 254
            or not 1 <= len(local) <= 64 or local.startswith(".") or local.endswith(".")
            or ".." in local or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)):
        raise ValueError("Enter a valid email address.")
    return email


def sign_up_user(username, email, password, *, student_id, display_name=None):
    """Create an unconfirmed account with an independent, self-reported student ID."""
    try:
        email = normalize_signup_email(email)
    except ValueError as error:
        return {"success": False, "message": str(error)}
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        return {"success": False, "message": "Use 1-64 letters, numbers, periods, underscores or hyphens for your username."}
    if not isinstance(student_id, str) or not re.fullmatch(r"[0-9]{1,64}", student_id):
        return {"success": False, "message": "Enter your ID number using 1-64 digits (0-9) only."}

    try:
        secret_hash = get_secret_hash(username)
        resp = cognito_client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=secret_hash,
            Username=username,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": display_name or username},
                {"Name": "custom:student_id", "Value": student_id},
            ]
        )
        
        requires_confirmation = not resp.get("UserConfirmed", False)
        return {"success": True, "user_sub": resp.get("UserSub"),
                "requires_confirmation": requires_confirmation,
                "delivery_pending": requires_confirmation and not resp.get("CodeDeliveryDetails")}
    except ClientError as e:
        error = e.response.get("Error", {})
        message = (error.get("Message") or "").lower()
        if error.get("Code") in ("InvalidParameterException", "NotAuthorizedException") and (
            "custom:student_id" in message or "unauthorized attribute" in message
            or "attribute does not exist in the schema" in message
        ):
            print("[cognito] Signup profile schema or app-client attribute write access is not configured.")
            return {"success": False, "configuration_error": True,
                    "message": "Account creation requires a server configuration update. Please contact the school administrator."}
        # Cognito can create the UNCONFIRMED user even if delivery is throttled.
        # Keep a confirmation/resend path, never an authenticated app session.
        if e.response.get("Error", {}).get("Code") in ("CodeDeliveryFailureException", "LimitExceededException"):
            return {"success": True, "requires_confirmation": True, "delivery_pending": True,
                    "message": "Your signup needs verification, but the code could not be sent. Please try resending it."}
        return {"success": False, "message": handle_cognito_error(e)}


def confirm_signup_user(username, code, *, expected_email):
    try:
        error = _pending_account_error(username, expected_email)
        if error:
            return error
        cognito_client.confirm_sign_up(
            ClientId=COGNITO_CLIENT_ID, Username=username,
            SecretHash=get_secret_hash(username), ConfirmationCode=code,
            ForceAliasCreation=False,
        )
        return {"success": True, "message": "Your email is verified. You can now sign in."}
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NotAuthorizedException":
            return {"success": False, "message": "Verification could not be completed. If you already verified your email, sign in instead."}
        return {"success": False, "message": handle_cognito_error(error)}


def resend_signup_code(username, *, expected_email):
    try:
        error = _pending_account_error(username, expected_email)
        if error:
            return error
        cognito_client.resend_confirmation_code(
            ClientId=COGNITO_CLIENT_ID, Username=username,
            SecretHash=get_secret_hash(username),
        )
        return {"success": True, "message": "A new verification code was sent to your email."}
    except ClientError as error:
        return {"success": False, "message": handle_cognito_error(error)}


def login_user(identifier, password):
    """Logs in a user by email OR username and returns authentication tokens.

    Students sign in with their email; Cognito wants the username. Resolving
    here (rather than in the route) means every caller benefits — the login
    page, and settings.change_password which re-authenticates with the email
    stored in the session.

    The SecretHash must be computed over the SAME value sent as USERNAME, or
    Cognito rejects the request as unauthorized, so both are derived from the
    resolved name.
    """
    try:
        username = resolve_cognito_username(identifier)
        secret_hash = get_secret_hash(username)
        resp = cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": secret_hash
            }
        )
        return {"success": True, "auth_result": resp["AuthenticationResult"]}
    except ClientError as e:
        return {"success": False, "message": handle_cognito_error(e)}

def forgot_password(email):
    """Initiates the forgot password flow with SecretHash.

    Same email -> username translation as login: the reset form asks for an
    email address, but Cognito needs the username to find the account (and to
    know which address to mail the code to).
    """
    try:
        username = resolve_cognito_username(email)
        secret_hash = get_secret_hash(username)
        cognito_client.forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=secret_hash,
            Username=username
        )
        return {"success": True, "username": username,
                "message": "If an account with that email exists, you will receive a code to reset your password."}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        # Same non-committal reply for a bad address as for an unknown account:
        # neither should tell an outsider whether an email is registered here.
        if code in ("UserNotFoundException", "InvalidParameterException"):
            return {"success": True, "username": username,
                    "message": "If an account with that email exists, you will receive a code to reset your password."}
        return {"success": False, "message": handle_cognito_error(e)}

def reset_password(email, code, new_password):
    """Resets a user's password with a confirmation code and SecretHash.

    The code was issued against the resolved username in forgot_password, so
    confirm_forgot_password has to be given that same username.
    """
    try:
        username = resolve_cognito_username(email)
        secret_hash = get_secret_hash(username)
        cognito_client.confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=secret_hash,
            Username=username,
            ConfirmationCode=code,
            Password=new_password
        )
        return {"success": True, "message": "Password has been reset successfully!"}
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code in ("CodeMismatchException", "ExpiredCodeException", "UserNotFoundException", "UserNotConfirmedException"):
            return {"success": False, "retry_allowed": error_code != "ExpiredCodeException",
                    "message": "The verification code is incorrect or expired."}
        return {"success": False,
                "retry_allowed": error_code in ("InvalidPasswordException", "PasswordHistoryPolicyViolationException"),
                "message": handle_cognito_error(e)}
