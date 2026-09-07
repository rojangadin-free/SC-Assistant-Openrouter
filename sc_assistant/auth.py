from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
from jose import jwt
from config import COGNITO_USER_POOL_ID
from aws.cognito import (
    sign_up_user, login_user, forgot_password, reset_password,
    get_user_role_from_claims, cognito_client
)
from .utils import is_admin

# Create a Blueprint
bp = Blueprint('auth', __name__)

@bp.route("/auth")
def auth_page():
    user = session.get("user")
    is_guest = session.get("is_guest", False) or user == "guest"
    
    # Only redirect if they have an active session AND they are not a guest
    if user and not is_guest:
        return redirect(url_for("chat.index"))
        
    return render_template("auth.html")

@bp.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email")
    password = request.form.get("password")
    username = request.form.get("username")
    # 🚀 Set on the auth page now: the student accepts (or doesn't) the Data
    # Access Agreement modal before the account is created, instead of being
    # asked again later in Settings.
    data_consent = request.form.get("data_consent", "false").lower() == "true"

    if not all([email, password, username]):
        return jsonify({"success": False, "message": "All fields are required."})

    if not data_consent:
        return jsonify({"success": False, "message": "You must accept the Data Access Agreement to create an account."})

    result = sign_up_user(username, email, password)
    if result["success"]:
        # Best-effort: persist the agreed-to preference to Cognito right away,
        # the same way settings.update_consent does post-login, so it's
        # already correct the first time they log in (login() below reads
        # custom:data_consent back out of the token claims).
        try:
            cognito_client.admin_update_user_attributes(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=username,
                UserAttributes=[
                    {'Name': 'custom:data_consent', 'Value': str(data_consent).lower()}
                ]
            )
        except Exception:
            # Non-fatal: the session value below still reflects the user's
            # choice for their current session even if this call fails.
            pass

        session.update({
            "user": email, "uid": result.get("user_sub"),
            "username": username, "role": "user",
            "data_consent": data_consent
        })
        return jsonify({"success": True, "redirect": url_for("chat.chat_page")})
    return jsonify(result)

@bp.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("email")
    password = request.form.get("password")
    if not all([identifier, password]):
        return jsonify({"success": False, "message": "Both identifier and password are required."})

    result = login_user(identifier, password)
    if result["success"]:
        auth_result = result["auth_result"]
        id_token = auth_result["IdToken"]
        claims = jwt.get_unverified_claims(id_token)
        
        # 🚀 Fetch the universally saved setting from Cognito (defaults to "true" if they are a brand new user)
        consent_claim = claims.get("custom:data_consent", "true")
        
        session.update({
            "user": identifier,
            "uid": claims["sub"],
            "id_token": id_token,
            "username": claims.get("name", identifier.split("@")[0]),
            "cognito_username": claims.get("cognito:username", identifier),
            "role": get_user_role_from_claims(id_token),
            "is_guest": False,
            "data_consent": consent_claim.lower() == "true" # 🚀 Apply their saved preference
        })
        
        session['start_new_chat'] = True
        redirect_url = url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page")
        return jsonify({"success": True, "redirect": redirect_url})
        
    return jsonify(result)

@bp.route("/guest", methods=["POST"])
def guest_login():
    """Allow unenrolled visitors to use the general inquiry chatbot without an account."""
    session.clear()
    session["user"] = "guest"
    session["uid"] = None
    session["username"] = "Guest"
    session["role"] = "guest"
    session["is_guest"] = True
    session["start_new_chat"] = True
    return jsonify({"success": True, "redirect": url_for("chat.chat_page")})

@bp.route("/forgot-password", methods=["POST"])
def handle_forgot_password():
    email = request.form.get("email")
    if not email:
        return jsonify({"success": False, "message": "An email address is required."})
    return jsonify(forgot_password(email))

@bp.route("/reset-password", methods=["POST"])
def handle_reset_password():
    email = request.form.get("email")
    code = request.form.get("code")
    new_password = request.form.get("password")

    if not all([email, code, new_password]):
        return jsonify({"success": False, "message": "Email, code, and new password are required."})
    return jsonify(reset_password(email, code, new_password))

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.auth_page"))