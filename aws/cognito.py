import boto3
import hmac
import hashlib
import base64
from botocore.exceptions import ClientError
from jose import jwt
from config import COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, AWS_REGION

# Ensure you add COGNITO_CLIENT_SECRET to your config.py and .env
from config import COGNITO_CLIENT_SECRET 

cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)

COGNITO_ERROR_MESSAGES = {
    "UsernameExistsException": "This username or email is already registered. Please try logging in.",
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

    try:
        resp = cognito_client.list_users(
            UserPoolId=COGNITO_USER_POOL_ID,
            # Cognito's filter syntax needs the value in double quotes; it only
            # supports prefix ("^=") and exact ("=") matching on one attribute.
            Filter=f'email = "{identifier}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if users:
            return users[0].get("Username") or identifier
    except ClientError as e:
        print(f"[cognito] resolve_cognito_username lookup failed (non-fatal): {e}")
    except Exception as e:
        print(f"[cognito] resolve_cognito_username unexpected error (non-fatal): {e}")

    return identifier

def sign_up_user(username, email, password):
    """Signs up a new user in Cognito using SecretHash."""
    if "@" in username or ' ' in username:
        return {"success": False, "message": "Username cannot be an email address or contain spaces."}

    try:
        secret_hash = get_secret_hash(username)
        resp = cognito_client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            SecretHash=secret_hash,
            Username=username,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": username},
                {"Name": "custom:role", "Value": "user"}
            ]
        )
        
        # Admin actions do NOT require SecretHash, but require Admin IAM permissions
        cognito_client.admin_confirm_sign_up(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username
        )
        cognito_client.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            UserAttributes=[{'Name': 'email_verified', 'Value': 'true'}]
        )
        return {"success": True, "user_sub": resp.get("UserSub")}
    except ClientError as e:
        return {"success": False, "message": handle_cognito_error(e)}

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
        return {"success": True, "message": "If an account with that email exists, you will receive a code to reset your password."}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        # Same non-committal reply for a bad address as for an unknown account:
        # neither should tell an outsider whether an email is registered here.
        if code in ("UserNotFoundException", "InvalidParameterException"):
            return {"success": True, "message": "If an account with that email exists, you will receive a code to reset your password."}
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
        return {"success": False, "message": handle_cognito_error(e)}
