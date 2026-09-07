from flask import session
from jose import jwt
import uuid

def is_admin():
    """Checks if current user is an admin."""
    return session.get("role") == "admin"

def get_session_id():
    """Gets or creates a unique session ID."""
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]

def get_cognito_username():
    """
    Get the correct username for Cognito admin_* operations.

    Order matters: this pool signs in by username, not email, so an email in the
    Username slot is rejected with InvalidParameterException. The ID token's
    'cognito:username' claim is the authoritative value, and login() copies it
    into the session, so both are tried before falling back to the email.
    """
    # Try to get the actual username from the ID token if available
    if "id_token" in session:
        try:
            claims = jwt.get_unverified_claims(session["id_token"])
            # Cognito uses 'cognito:username' for the actual username
            cognito_username = claims.get("cognito:username")
            if cognito_username:
                return cognito_username
        except Exception:
            pass

    # Copied out of the token at login, so it survives even if the token itself
    # is no longer in the session.
    cognito_username = session.get("cognito_username")
    if cognito_username:
        return cognito_username

    # Last resorts. Neither is a real Cognito username in this pool, but a
    # wrong value produces a clear AWS error rather than silently acting on
    # somebody else's account.
    return session.get("user") or session.get("username", "")
