import boto3
from botocore.exceptions import ClientError
from jose import jwt
from config import COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, AWS_REGION

cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)

COGNITO_ERROR_MESSAGES = {
    "UsernameExistsException": "This username or email is already registered. Please try logging in.",
    "InvalidPasswordException": "Your password is not strong enough. It must be at least 8 characters long and include an uppercase letter, a lowercase letter, a number, and a special character (e.g., !@#$%).",
    "InvalidParameterException": "Please provide a valid email address and username. Usernames cannot be email addresses.",
    "NotAuthorizedException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotFoundException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotConfirmedException": "Your account is not confirmed yet. Please check your email for a confirmation link.",
    "TooManyRequestsException": "You've made too many requests. Please wait a moment and try again.",
    "InternalErrorException": "An internal server error occurred. Please try again later.",
    "CodeMismatchException": "The verification code is incorrect. Please check the code and try again.",
    "ExpiredCodeException": "The verification code has expired. Please request a new one.",
    "LimitExceededException": "You have exceeded the limit for password reset attempts. Please try again later."
}

def get_user_role_from_claims(id_token):
    """Decodes the user role from the JWT token."""
    decoded = jwt.get_unverified_claims(id_token)
    return decoded.get("custom:role", "user")

def handle_cognito_error(e):
    """Provides a user-friendly error message for Cognito exceptions."""
    err_code = e.response.get("Error", {}).get("Code")
    return COGNITO_ERROR_MESSAGES.get(err_code, "An unexpected error occurred. Please try again.")

def sign_up_user(username, email, password):
    """Signs up a new user in Cognito."""
    if "@" in username or ' ' in username:
        return {"success": False, "message": "Username cannot be an email address or contain spaces."}

    try:
        resp = cognito_client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": username},
                {"Name": "custom:role", "Value": "user"}
            ]
        )
        # Auto-confirm user for simplicity
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
    """Logs in a user and returns authentication tokens."""
    try:
        resp = cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": identifier, "PASSWORD": password}
        )
        return {"success": True, "auth_result": resp["AuthenticationResult"]}
    except ClientError as e:
        return {"success": False, "message": handle_cognito_error(e)}

def forgot_password(email):
    """Initiates the forgot password flow for a user."""
    try:
        cognito_client.forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email
        )
        return {"success": True, "message": "If an account with that email exists, you will receive a code to reset your password."}
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            return {"success": True, "message": "If an account with that email exists, you will receive a code to reset your password."}
        return {"success": False, "message": handle_cognito_error(e)}

def reset_password(email, code, new_password):
    """Resets a user's password with a confirmation code."""
    try:
        cognito_client.confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password
        )
        return {"success": True, "message": "Password has been reset successfully!"}
    except ClientError as e:
        return {"success": False, "message": handle_cognito_error(e)}