import hmac
import math
import re
import secrets
import time

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from jose import jwt

from aws.cognito import (
    cognito_client,
    confirm_signup_user,
    forgot_password,
    get_user_role_from_claims,
    login_user,
    normalize_signup_email,
    resend_signup_code,
    reset_password,
    sign_up_user,
    signup_mutation_lock,
)
from config import COGNITO_USER_POOL_ID
from rag.login_activity import record_login

from .reset_state import ResetFlowError, reset_flows
from .utils import is_admin

# Create a Blueprint
bp = Blueprint('auth', __name__)


@bp.after_request
def prevent_auth_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _signup_csrf_token():
    token = session.get("signup_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["signup_csrf"] = token
    return token


def _valid_signup_csrf():
    expected = session.get("signup_csrf")
    supplied = request.headers.get("X-Auth-CSRF", "")
    return bool(isinstance(expected, str) and supplied
                and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")))


def _pending_signup():
    pending = session.get("pending_signup")
    if (not isinstance(pending, dict) or not pending.get("username")
            or not pending.get("email") or not isinstance(pending.get("created_at"), (int, float))
            or time.time() - pending["created_at"] > 86400):
        session.pop("pending_signup", None)
        return None
    return pending


def _signup_client_state(pending):
    now = time.time()
    last_sent = pending.get("last_sent", 0) if pending else 0
    return {"email": pending["email"] if pending else "",
            "resend_available_at": (last_sent + 30) * 1000 if last_sent else 0,
            "server_now": now * 1000}

@bp.route("/auth")
def auth_page():
    user = session.get("user")
    is_guest = session.get("is_guest", False) or user == "guest"
    
    # Only redirect if they have an active session AND they are not a guest
    if user and not is_guest:
        return redirect(url_for("chat.chat_page"))
        
    return render_template("auth.html", initial_view="login", auth_csrf=_signup_csrf_token())

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    signed_in = session.get("user") and not (session.get("is_guest") or session.get("user") == "guest")
    if request.method == "GET":
        if signed_in:
            return redirect(url_for("chat.chat_page"))
        pending = _pending_signup()
        return render_template("auth.html", initial_view="verify" if pending else "signup",
                               signup_email=pending["email"] if pending else "", auth_csrf=_signup_csrf_token(),
                               signup_state=_signup_client_state(pending))
    if signed_in:
        return jsonify({"success": False, "message": "Sign out before creating another account."}), 409
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh this page before creating an account."}), 403
    request.max_content_length = 16384
    try:
        if request.form.get("signup_method", "id").strip().lower() != "id":
            raise ValueError("Signup requires an ID number.")
        username = (request.form.get("username") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
            raise ValueError("Use 1-64 letters, numbers, periods, underscores or hyphens for your username.")
        student_id = (request.form.get("student_id") or "").strip()
        if not re.fullmatch(r"[0-9]{1,64}", student_id):
            raise ValueError("Enter your ID number using 1-64 digits (0-9) only.")
        email = normalize_signup_email(request.form.get("email"))
        password = request.form.get("password") or ""
        if not 8 <= len(password) <= 256:
            raise ValueError("Enter a password between 8 and 256 characters.")
        confirmation = request.form.get("confirm_password")
        if confirmation and confirmation != password:
            raise ValueError("The passwords do not match.")
        if request.form.get("data_consent", "false").lower() != "true":
            raise ValueError("You must accept the Data Access Agreement to create an account.")
    except ValueError as error:
        return jsonify({"success": False, "message": str(error)}), 400

    result = sign_up_user(username, email, password, student_id=student_id)
    if not result.get("success"):
        return jsonify(result), 503 if result.get("configuration_error") else 400
    if result.get("requires_confirmation", True):
        now = time.time()
        session["pending_signup"] = {"username": username, "email": email,
                                     "created_at": now,
                                     "last_sent": 0 if result.get("delivery_pending") else now}
        return jsonify({"success": True, "requires_confirmation": True, "email": email,
                        "signup_state": _signup_client_state(session["pending_signup"]),
                        "delivery_pending": bool(result.get("delivery_pending")),
                        "message": result.get("message", "Check your email for your verification code.")})
    session.pop("pending_signup", None)
    return jsonify({"success": True, "requires_confirmation": False,
                    "message": "Account created. Please sign in.", "redirect": url_for("auth.auth_page")})


@bp.route("/confirm-signup", methods=["POST"])
def confirm_signup():
    request.max_content_length = 16384
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh this page before verifying your email."}), 403
    pending = _pending_signup()
    code = (request.form.get("code") or "").strip()
    if not pending:
        return jsonify({"success": False, "message": "Your signup session expired. Please start signup again."}), 400
    if not re.fullmatch(r"[0-9]{6}", code):
        return jsonify({"success": False, "message": "Enter the 6-digit verification code from your email."}), 400
    try:
        with signup_mutation_lock(pending["username"]):
            result = confirm_signup_user(pending["username"], code, expected_email=pending["email"])
    except OSError:
        return jsonify({"success": False, "message": "Signup is busy. Please try again shortly."}), 503
    if not result.get("success"):
        return jsonify(result), 400
    try:
        cognito_client.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID, Username=pending["username"],
            UserAttributes=[{"Name": "custom:data_consent", "Value": "true"}],
        )
    except Exception as error:  # noqa: BLE001 - agreement persistence must not undo email verification
        # The agreement was already accepted; the existing login default is true.
        print(f"[auth] Agreement preference could not be saved ({type(error).__name__}).")
    session.pop("pending_signup", None)
    return jsonify({**result, "redirect": url_for("auth.auth_page")})


@bp.route("/resend-signup-code", methods=["POST"])
def resend_signup():
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh this page before requesting a code."}), 403
    pending = _pending_signup()
    if not pending:
        return jsonify({"success": False, "message": "Your signup session expired. Please start signup again."}), 400
    wait = 30 - (time.time() - pending.get("last_sent", 0))
    if wait > 0:
        return jsonify({"success": False, "message": "Please wait before requesting another code.",
                        "signup_state": _signup_client_state(pending)}), 429, {"Retry-After": str(math.ceil(wait))}
    try:
        with signup_mutation_lock(pending["username"]):
            result = resend_signup_code(pending["username"], expected_email=pending["email"])
    except OSError:
        return jsonify({"success": False, "message": "Signup is busy. Please try again shortly."}), 503
    now = time.time()
    session["pending_signup"] = {**pending, "last_sent": now,
                                 "created_at": now if result.get("success") else pending["created_at"]}
    return jsonify({**result, "signup_state": _signup_client_state(session["pending_signup"])}), 200 if result.get("success") else 400


@bp.route("/restart-signup", methods=["POST"])
def restart_signup():
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh this page before restarting signup."}), 403
    if session.get("user") and not (session.get("is_guest") or session.get("user") == "guest"):
        return jsonify({"success": False, "message": "Sign out before creating another account."}), 409
    # Returning to a blank form is not permission to delete or rewrite a
    # Cognito account. New details go through normal signup validation again.
    session.pop("pending_signup", None)
    return jsonify({"success": True, "redirect": url_for("auth.signup"),
                    "signup_state": _signup_client_state(None)})

@bp.route("/login", methods=["POST"])
def login():
    # Keep the existing form key; login_user resolves email or student ID to the
    # canonical username before authenticating with the supplied password.
    identifier = (request.form.get("email") or "").strip()
    password = request.form.get("password")
    if not all([identifier, password]):
        return jsonify({"success": False, "message": "Please enter your student ID, email, or username and your password."})

    result = login_user(identifier, password)
    if result["success"]:
        auth_result = result["auth_result"]
        id_token = auth_result["IdToken"]
        claims = jwt.get_unverified_claims(id_token)
        
        # 🚀 Fetch the universally saved setting from Cognito (defaults to "true" if they are a brand new user)
        consent_claim = claims.get("custom:data_consent", "true")

        # session["user"] is treated as the email everywhere downstream — it is
        # what matches a student to their DynamoDB record and what Settings
        # renders as "Email Address". Take the verified token's email, never the
        # typed student ID or username, so account record lookups stay unchanged.
        email = claims.get("email")
        if not email:
            return jsonify({"success": False, "message": "Sign-in could not read your account email. Please contact the school administrator."}), 503
        if claims.get("email_verified") not in (True, "true"):
            return jsonify({"success": False, "message": "Verify your email before signing in. Contact the school administrator if your account is already confirmed."}), 403
        
        session.clear()
        session.update({
            "user": email,
            "uid": claims["sub"],
            "id_token": id_token,
            "username": claims.get("name") or claims.get("cognito:username") or email.split("@")[0],
            "cognito_username": claims.get("cognito:username", identifier),
            "role": get_user_role_from_claims(id_token),
            "is_guest": False,
            "data_consent": consent_claim.lower() == "true" # 🚀 Apply their saved preference
        })
        
        session['start_new_chat'] = True
        redirect_url = url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page")
        try:
            record_login(uid=session["uid"], display_name=session["username"], email=session["user"])
        except Exception:  # noqa: BLE001 - reporting must not undo a successful sign-in
            print("[auth] Sign-in activity could not be recorded.")
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
    request.max_content_length = 16384
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh the page before requesting a reset code."}), 403
    flow_id = None
    try:
        email = normalize_signup_email(request.form.get("email"))
        flow_id = reset_flows.begin(email, session.get("password_reset_id"))
        result = forgot_password(email)
        if not result.get("success"):
            reset_flows.discard(flow_id)
            return jsonify(result), 503
        reset_flows.mark_code_sent(flow_id, result["username"])
        session["password_reset_id"] = flow_id
        return jsonify({"success": True,
                         "message": result["message"],
                        "reset_state": reset_flows.public_state(flow_id)})
    except ValueError as error:
        return jsonify({"success": False, "message": str(error)}), 400
    except ResetFlowError as error:
        return _reset_flow_error(error)
    except Exception as error:  # noqa: BLE001 - never expose recovery data or credentials
        if flow_id:
            try:
                reset_flows.discard(flow_id)
            except Exception:  # noqa: BLE001 - a failed sending record expires without granting access
                print("[auth] Unfinished recovery state will expire without granting access.")
        print(f"[auth] Reset request failed ({type(error).__name__}).")
        return jsonify({"success": False, "message": "A reset code could not be requested. Please try again later."}), 503


def _reset_flow_error(error):
    try:
        state = reset_flows.public_state(session.get("password_reset_id"))
    except Exception:  # noqa: BLE001 - do not turn a storage failure into a traceback response
        return jsonify({"success": False, "message": "Password recovery is temporarily unavailable."}), 503
    if error.retry_after:
        state["resend_available_at"] = max(state.get("resend_available_at", 0),
                                           state["server_now"] + error.retry_after * 1000)
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after else {}
    return jsonify({"success": False, "message": str(error), "reset_state": state}), error.status, headers


@bp.route("/password-reset")
def password_reset_page():
    state = reset_flows.public_state(session.get("password_reset_id"))
    return render_template("auth.html", initial_view="reset", reset_state=state, auth_csrf=_signup_csrf_token())


@bp.route("/verify-reset-code", methods=["POST"])
def handle_verify_reset_code():
    # An already-open code-first page must reload, never obtain an admin-write grant.
    return jsonify({"success": False,
                    "message": "Password recovery has changed. Refresh the page and submit the code with your new password.",
                    "reset_state": {"stage": "expired", "email": "", "resend_available_at": 0,
                                    "server_now": time.time() * 1000}}), 410


@bp.route("/reset-password", methods=["POST"])
def handle_reset_password():
    request.max_content_length = 16384
    if not _valid_signup_csrf():
        return jsonify({"success": False, "message": "Refresh the page before changing your password."}), 403
    code = (request.form.get("code") or "").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        return jsonify({"success": False, "message": "Enter the 6-digit reset code from your email."}), 400
    password = request.form.get("password") or ""
    if not 8 <= len(password) <= 256 or password != request.form.get("confirmPassword"):
        return jsonify({"success": False, "message": "Enter matching passwords between 8 and 256 characters."}), 400
    flow_id = session.get("password_reset_id")
    try:
        email = normalize_signup_email(request.form.get("email")) if "email" in request.form else None
        flow = reset_flows.claim_password_change(flow_id, email=email)
    except ValueError as error:
        return jsonify({"success": False, "message": str(error)}), 400
    except ResetFlowError as error:
        return _reset_flow_error(error)
    except Exception:  # noqa: BLE001 - no request without a valid, atomically claimed reset context
        return jsonify({"success": False, "message": "Password recovery is temporarily unavailable."}), 503
    try:
        result = reset_password(flow["username"], code, password)
    except Exception as error:  # noqa: BLE001 - uncertain writes consume the request, never retry blindly
        print(f"[auth] Password change failed ({type(error).__name__}).")
        result = {"success": False, "message": "The password change could not be confirmed. Try signing in or request a new code."}
    try:
        reset_flows.finish_password_change(flow_id, retry=not result.get("success") and result.get("retry_allowed", False))
    except Exception as error:  # noqa: BLE001 - a writing request cannot be replayed if cleanup fails
        print(f"[auth] Reset request cleanup failed ({type(error).__name__}).")
        session.pop("password_reset_id", None)
        if not result.get("success"):
            return jsonify({"success": False, "message": "The password change could not be completed. Request a new code."}), 503
    if result.get("success"):
        session.pop("password_reset_id", None)
        return jsonify({**result, "reset_state": {"stage": "expired", "email": "", "resend_available_at": 0,
                                                  "server_now": time.time() * 1000}})
    return jsonify({**result, "reset_state": reset_flows.public_state(flow_id)}), 400

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.auth_page"))
