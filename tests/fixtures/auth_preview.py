"""Real signup routes/templates with a fake Cognito service. Never contacts AWS."""

import argparse
import ast
import base64
import hashlib
import hmac
import math
import os
import re
import secrets
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from flask import (
    Blueprint,
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": code}}
        super().__init__(code)


class FakeCognito:
    def __init__(self):
        self.users = {}
        self.calls = []
        self.fail_next = {}
        self.challenges = {}
        self.otp_enabled = True
        self.select_challenge = False
        self.clock = time.time
        self.password_reset_writes = 0

    def record(self, operation, kwargs):
        self.calls.append((operation, kwargs))
        failure = self.fail_next.pop(operation, None)
        if failure:
            raise ClientError(failure)

    def sign_up(self, **kwargs):
        self.record("sign_up", kwargs)
        key = kwargs["Username"].lower()
        if key in self.users:
            raise ClientError("UsernameExistsException")
        self.users[key] = {"attributes": {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]},
                           "password": kwargs["Password"], "confirmed": False}
        return {"UserConfirmed": False, "UserSub": f"test-sub-{key}",
                "CodeDeliveryDetails": {"DeliveryMedium": "EMAIL", "Destination": "test mailbox"}}

    def confirm_sign_up(self, **kwargs):
        self.record("confirm_sign_up", kwargs)
        key = kwargs["Username"].lower()
        if key not in self.users:
            raise ClientError("UserNotFoundException")
        if self.users[key]["confirmed"]:
            raise ClientError("NotAuthorizedException")
        if kwargs["ConfirmationCode"] != "123456":
            raise ClientError("CodeMismatchException")
        email = self.users[key]["attributes"]["email"]
        if any(k != key and u["confirmed"] and u["attributes"]["email"] == email
               for k, u in self.users.items()):
            raise ClientError("AliasExistsException")
        self.users[key]["confirmed"] = True
        self.users[key]["attributes"]["email_verified"] = "true"
        return {}

    def resend_confirmation_code(self, **kwargs):
        self.record("resend_confirmation_code", kwargs)
        if kwargs["Username"].lower() not in self.users:
            raise ClientError("UserNotFoundException")
        return {"CodeDeliveryDetails": {"DeliveryMedium": "EMAIL"}}

    def admin_confirm_sign_up(self, **kwargs):
        raise AssertionError("Signup must not bypass ownership verification")

    def admin_get_user(self, **kwargs):
        self.record("admin_get_user", kwargs)
        key = kwargs["Username"].lower()
        user = self.users.get(key)
        if not user:
            raise ClientError("UserNotFoundException")
        return {"Username": key, "UserStatus": "CONFIRMED" if user["confirmed"] else "UNCONFIRMED",
                "UserAttributes": [{"Name": k, "Value": v} for k, v in user["attributes"].items()]
                + [{"Name": "sub", "Value": f"test-sub-{key}"}]}

    def admin_update_user_attributes(self, **kwargs):
        self.record("admin_update_user_attributes", kwargs)
        attrs = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        if attrs.get("email_verified") == "true":
            raise AssertionError("Signup must not force email_verified")
        self.users[kwargs["Username"].lower()]["attributes"].update(attrs)
        return {}

    def list_users(self, **kwargs):
        self.record("list_users", kwargs)
        email = None
        if "Filter" in kwargs:
            if not re.fullmatch(r'email = ".*"', kwargs["Filter"]):
                raise AssertionError("FakeCognito supports only exact email filters")
            email = kwargs["Filter"].removeprefix('email = "').removesuffix('"')
        users = [
            {"Username": key, "UserStatus": "CONFIRMED" if user["confirmed"] else "UNCONFIRMED",
             "Attributes": [{"Name": k, "Value": v} for k, v in user["attributes"].items()]}
            for key, user in self.users.items() if email is None or user["attributes"].get("email") == email
        ]
        start = int(kwargs.get("PaginationToken", "0"))
        end = start + kwargs.get("Limit", 60)
        response = {"Users": users[start:end]}
        if end < len(users):
            response["PaginationToken"] = str(end)
        return response

    def get_paginator(self, operation):
        if operation != "list_users":
            raise AssertionError("FakeCognito supports only the list_users paginator")

        def paginate(**kwargs):
            while True:
                # Dispatch each page through the public method so mocks can supply
                # empty pages, stale snapshots, and mid-pagination failures.
                response = self.list_users(**kwargs)
                yield response
                token = response.get("PaginationToken")
                if not token:
                    break
                if token == kwargs.get("PaginationToken"):
                    raise RuntimeError("Repeated pagination token")
                kwargs["PaginationToken"] = token

        return SimpleNamespace(paginate=paginate)

    def initiate_auth(self, **kwargs):
        self.record("initiate_auth", kwargs)
        params = kwargs["AuthParameters"]
        key = params["USERNAME"].lower()
        user = self.users.get(key)
        if not user:
            raise ClientError("UserNotFoundException")
        if kwargs["AuthFlow"] == "USER_AUTH":
            if not self.otp_enabled:
                raise ClientError("OperationNotEnabledException")
            if not user["confirmed"] or user["attributes"].get("email_verified") != "true":
                raise ClientError("UserNotConfirmedException")
            token = secrets.token_urlsafe(32)
            self.challenges[token] = key
            if self.select_challenge:
                return {"ChallengeName": "SELECT_CHALLENGE", "AvailableChallenges": ["PASSWORD", "EMAIL_OTP"], "Session": token}
            return {"ChallengeName": "EMAIL_OTP", "Session": token}
        if not user["confirmed"]:
            raise ClientError("UserNotConfirmedException")
        if params["PASSWORD"] != user["password"]:
            raise ClientError("NotAuthorizedException")
        return {"AuthenticationResult": {"IdToken": key}}

    def respond_to_auth_challenge(self, **kwargs):
        self.record("respond_to_auth_challenge", kwargs)
        token = kwargs["Session"]
        key = self.challenges.get(token)
        if not key or kwargs["ChallengeResponses"]["USERNAME"].lower() != key:
            raise ClientError("NotAuthorizedException")
        if kwargs["ChallengeName"] == "SELECT_CHALLENGE":
            if kwargs["ChallengeResponses"].get("ANSWER") != "EMAIL_OTP":
                raise ClientError("InvalidParameterException")
            return {"ChallengeName": "EMAIL_OTP", "Session": token}
        if kwargs["ChallengeResponses"].get("EMAIL_OTP_CODE") != "12345678":
            raise ClientError("CodeMismatchException")
        self.challenges.pop(token)
        return {"AuthenticationResult": {"IdToken": key, "AccessToken": "SECRET_ACCESS_TOKEN",
                                         "RefreshToken": "SECRET_REFRESH_TOKEN"}}

    def admin_set_user_password(self, **kwargs):
        self.record("admin_set_user_password", kwargs)
        user = self.users.get(kwargs["Username"].lower())
        if not user:
            raise ClientError("UserNotFoundException")
        if len(kwargs["Password"]) < 8:
            raise ClientError("InvalidPasswordException")
        user["password"] = kwargs["Password"]
        return {}

    def claims(self, token):
        user = self.users[token]
        attrs = user["attributes"]
        return {"sub": f"test-sub-{token}", "cognito:username": token,
                "iss": "https://cognito-idp.offline-region.amazonaws.com/offline-pool",
                "aud": "offline-client", "token_use": "id",
                "email": attrs["email"], "email_verified": attrs.get("email_verified") == "true",
                "name": attrs.get("name", token), "custom:role": attrs.get("custom:role", "user"),
                "custom:data_consent": attrs.get("custom:data_consent", "true")}

    def forgot_password(self, **kwargs):
        self.record("forgot_password", kwargs)
        user = self.users.get(kwargs["Username"].lower())
        if not user:
            raise ClientError("UserNotFoundException")
        if not user["confirmed"] or user["attributes"].get("email_verified") != "true":
            raise ClientError("InvalidParameterException")
        generation = user.get("reset_generation", 0) + 1
        user.update(reset_generation=generation, reset_code=f"{654320 + generation:06d}",
                    reset_expires=self.clock() + 3600)
        return {"CodeDeliveryDetails": {"DeliveryMedium": "EMAIL"}}

    def confirm_forgot_password(self, **kwargs):
        self.record("confirm_forgot_password", kwargs)
        user = self.users.get(kwargs["Username"].lower())
        if not user:
            raise ClientError("UserNotFoundException")
        if not user.get("reset_code") or self.clock() >= user.get("reset_expires", 0):
            raise ClientError("ExpiredCodeException")
        if kwargs["ConfirmationCode"] != user["reset_code"]:
            raise ClientError("CodeMismatchException")
        if len(kwargs["Password"]) < 8:
            raise ClientError("InvalidPasswordException")
        if kwargs["Password"] == user["password"]:
            raise ClientError("PasswordHistoryPolicyViolationException")
        user["password"] = kwargs["Password"]
        user.pop("reset_code")
        user.pop("reset_expires")
        self.password_reset_writes += 1
        return {}


def create_test_app(cognito=None, clock=None):
    """AST selection skips production config/client/model initialization only."""
    cognito = cognito or FakeCognito()
    jwt = SimpleNamespace(get_unverified_claims=cognito.claims)
    if clock is None:
        class PreviewClock:
            offset = 0

            def time(self):
                return time.time() + self.offset

            def advance(self, seconds):
                self.offset += seconds
        clock = PreviewClock()
    cognito.clock = clock.time
    namespace = {"cognito_client": cognito, "ClientError": ClientError, "re": re, "os": os,
                 "tempfile": tempfile, "math": math,
                 "base64": base64, "hashlib": hashlib, "hmac": hmac, "jwt": jwt,
                 "AWS_REGION": "offline-region", "COGNITO_USER_POOL_ID": "offline-pool", "COGNITO_CLIENT_ID": "offline-client",
                 "COGNITO_CLIENT_SECRET": "offline-secret"}
    tree = ast.parse((ROOT / "aws/cognito.py").read_text(encoding="utf-8"))
    constants = {"COGNITO_ERROR_MESSAGES", "COGNITO_CONFIG_ERROR_HINTS", "CONFIG_ERROR_MESSAGE"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) or (
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)
    )]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "aws/cognito.py", "exec"), namespace)  # noqa: S102
    app = Flask(__name__, template_folder=str(ROOT / "sc_assistant/templates"),
                static_folder=str(ROOT / "sc_assistant/static"), static_url_path="/static")
    app.secret_key = "offline-preview-not-a-production-secret"
    app.testing = True
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    bp = Blueprint("auth", __name__)
    # Import the real local reset store without loading operator config/.env.
    with patch.dict(sys.modules, {"config": SimpleNamespace(FLASK_SECRET_KEY=app.secret_key)}):
        from sc_assistant.reset_state import ResetFlowError, ResetFlowStore
    reset_tmp = tempfile.TemporaryDirectory(prefix="sc_reset_preview_")
    reset_store = ResetFlowStore(os.path.join(reset_tmp.name, "reset.sqlite3"), clock=clock.time)
    login_events = []

    def record_login(*, uid, display_name, email):
        event = {"id": uuid4().hex, "at": datetime.fromtimestamp(clock.time(), timezone.utc).isoformat(),
                 "uid": uid, "display_name": display_name, "email": email}
        login_events.append(event)
        return event

    namespace.update(bp=bp, render_template=render_template, jsonify=jsonify, request=request,
                     session=session, redirect=redirect, url_for=url_for, time=clock or time, secrets=secrets,
                     is_admin=lambda: session.get("role") == "admin", reset_flows=reset_store,
                      ResetFlowError=ResetFlowError, record_login=record_login,
                      dummy_students_enabled=lambda: False,
                      link_dummy_student_record=lambda *args, **kwargs: "disabled")
    tree = ast.parse((ROOT / "sc_assistant/auth.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "sc_assistant/auth.py", "exec"), namespace)  # noqa: S102
    app.register_blueprint(bp)
    app.add_url_rule("/chat", endpoint="chat.chat_page", view_func=lambda: jsonify(
        offline_fixture=True, user=session.get("user"), username=session.get("username"),
    ))
    app.add_url_rule("/dashboard", endpoint="admin.dashboard", view_func=lambda: "Offline admin page")
    app.add_url_rule("/__auth_test__/health", view_func=lambda: jsonify(
        offline_fixture=True, operations=dict(Counter(op for op, _ in cognito.calls)),
        password_reset_writes=cognito.password_reset_writes, login_events=len(login_events),
    ))
    if hasattr(clock, "advance"):
        def advance_clock():
            clock.advance(min(86401, max(0, float(request.get_json().get("seconds", 0)))))
            return jsonify(offline_fixture=True)
        app.add_url_rule("/__auth_test__/advance-time", methods=["POST"], view_func=advance_clock)
    app.extensions["auth_test"] = {"cognito": cognito, "namespace": namespace,
                                  "reset_store": reset_store, "reset_tmp": reset_tmp,
                                  "login_events": login_events}
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    print("OFFLINE SIGNUP PREVIEW: real UI/routes, fake Cognito; verification code 123456.", flush=True)
    create_test_app().run(host="127.0.0.1", port=args.port, debug=False)
