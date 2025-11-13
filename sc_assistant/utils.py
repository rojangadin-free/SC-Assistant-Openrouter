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
    Get the correct username for Cognito operations.
    Try to use the user's email first (most reliable),
    then fall back to session username.
    """
    # Try to get the actual username from the ID token if available
    if "id_token" in session:
        try:
            claims = jwt.get_unverified_claims(session["id_token"])
            # Cognito uses 'cognito:username' for the actual username
            cognito_username = claims.get("cognito:username")
            if cognito_username:
                return cognito_username
        except:
            pass
    
    # Fall back to email (works as username in Cognito)
    email = session.get("user")
    if email:
        return email
    
    # Last resort: session username
    return session.get("username", "")