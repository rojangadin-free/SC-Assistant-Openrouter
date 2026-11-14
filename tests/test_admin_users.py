"""Offline production admin-user routes: no app, AWS, model, or config imports."""

import ast
import os
import re
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

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

ROOT = Path(__file__).resolve().parents[1]
POOL = "offline-pool"
ADMIN = "Admin-Canonical-01"
TARGET = "User-Canonical-007"
UPSTREAM_DETAIL = "offline-sensitive-upstream-detail"
USERS_URL = "/api/dashboard/users"
UPDATE_URL = USERS_URL + "/update"


def load_functions(relative_path, namespace, names=None):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and (names is None or node.name in names)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102


class ClientError(Exception):
    def __init__(self, code, message=UPSTREAM_DETAIL):
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(f"{code}: {message}")


class FakeCognito:
    def __init__(self):
        self.users = {}
        self.pages = []
        self.calls = []
        self.failures = {}

    def record(self, operation, kwargs):
        self.calls.append((operation, deepcopy(kwargs)))
        if operation in self.failures:
            raise self.failures[operation]

    def get_paginator(self, operation):
        self.record("get_paginator", {"operation": operation})
        if operation != "list_users":
            raise AssertionError("Only user listing is allowed")
        return self

    def paginate(self, **kwargs):
        self.record("paginate", kwargs)
        return iter(deepcopy(self.pages))

    def admin_get_user(self, **kwargs):
        self.record("admin_get_user", kwargs)
        if kwargs["Username"] not in self.users:
            raise ClientError("UserNotFoundException")
        return {"Username": kwargs["Username"], "UserAttributes": [
            {"Name": key, "Value": value} for key, value in self.users[kwargs["Username"]].items()
        ]}

    def admin_update_user_attributes(self, **kwargs):
        self.record("admin_update_user_attributes", kwargs)
        if kwargs["Username"] not in self.users:
            raise ClientError("UserNotFoundException")
        attributes = self.users[kwargs["Username"]]
        for attribute in kwargs["UserAttributes"]:
            if attribute["Value"] == "":
                attributes.pop(attribute["Name"], None)
            else:
                attributes[attribute["Name"]] = attribute["Value"]
        return {}

    def admin_delete_user(self, **kwargs):
        self.record("admin_delete_user", kwargs)
        raise AssertionError("Admin user deletion must remain unavailable")


class AdminUsersTests(unittest.TestCase):
    def setUp(self):
        self.cognito = FakeCognito()
        for key, name, email, role in (
            (ADMIN, "Original Admin", "admin@example.test", "admin"),
            (TARGET, "Original User", "user@example.test", "user"),
        ):
            self.cognito.users[key] = {
                "sub": "sub-" + key, "name": name, "email": email,
                "email_verified": "true", "custom:role": role, "custom:student_id": "000123",
            }
        self.data_access = Mock(side_effect=AssertionError("Account edits must not touch stored data"))
        bp = Blueprint("admin", __name__)
        namespace = {
            "bp": bp, "cognito_client": self.cognito, "COGNITO_USER_POOL_ID": POOL,
            "ClientError": ClientError, "os": os, "re": re, "jsonify": jsonify,
            "request": request, "session": session, "redirect": redirect,
            "url_for": url_for, "render_template": render_template,
            "is_admin": lambda: session.get("role") == "admin",
            "get_cognito_username": Mock(return_value=ADMIN),
            "list_conversations": self.data_access,
            "delete_conversation_from_db": self.data_access,
            "delete_file_from_db": self.data_access, "delete_file_from_s3": self.data_access,
        }
        load_functions("aws/cognito.py", namespace, {"normalize_signup_email"})
        # Load every route, not an allowlist that would hide a restored delete endpoint.
        load_functions("sc_assistant/admin.py", namespace)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="offline-admin-route-tests")
        self.app.register_blueprint(bp)
        self.client = self.app.test_client()
        self.admin_session = {
            "user": "admin@example.test", "username": "Original Admin", "role": "admin",
            "uid": "sub-" + ADMIN, "id_token": "offline-id-token", "cognito_username": ADMIN,
            "chat_marker": "preserve-unrelated-session-data",
        }
        self.set_session(self.admin_session)
        self.form = {
            "cognito_username": TARGET, "username": "  Updated User  ",
            "email": "  New.User+tag@Example.NET  ", "student_id": " 0000987 ", "role": "admin",
        }

    def tearDown(self):
        self.data_access.assert_not_called()
        self.assertNotIn("admin_delete_user", [operation for operation, _ in self.cognito.calls])

    def set_session(self, state):
        with self.client.session_transaction() as saved:
            saved.clear()
            saved.update(state)

    def session_state(self):
        with self.client.session_transaction() as saved:
            return dict(saved)

    def assert_success(self, response, is_self=False):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertIs(payload["success"], True)
        self.assertTrue(payload["message"])
        self.assertIs(payload["is_self_update"], is_self)
        self.assertEqual(payload["new_username"], "Updated User")

    def assert_failure(self, response, status):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertIs(payload["success"], False)
        self.assertTrue(payload["message"])
        self.assertNotIn(UPSTREAM_DETAIL, response.get_data(as_text=True))
        return payload

    def assert_write(self, attributes, target=TARGET):
        self.assertEqual([operation for operation, _ in self.cognito.calls],
                         ["admin_get_user", "admin_update_user_attributes"])
        read_args, write_args = [kwargs for _, kwargs in self.cognito.calls]
        identity = {"UserPoolId": POOL, "Username": target}
        self.assertEqual(read_args, identity)
        self.assertEqual({key: value for key, value in write_args.items() if key != "UserAttributes"}, identity)
        self.assertCountEqual(write_args["UserAttributes"],
                              [{"Name": key, "Value": value} for key, value in attributes.items()])

    def test_list_and_update_require_a_signed_in_admin_before_cloud_access(self):
        states = ({}, {"role": "admin"}, {"user": "Guest", "role": "guest"},
                  {"user": "student@example.test", "role": "user"})
        for state in states:
            with self.subTest(session=state):
                self.set_session(state)
                self.assert_failure(self.client.get(USERS_URL), 403)
                self.assert_failure(self.client.post(UPDATE_URL, data=self.form), 403)
                self.assertEqual(self.cognito.calls, [])
                self.assertEqual(self.session_state(), state)

    def test_list_paginates_preserves_identity_and_supplies_missing_student_ids(self):
        older = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        newer = datetime.fromisoformat("2025-02-03T00:00:00+00:00")
        self.cognito.pages = [
            {"Users": [{"Username": TARGET, "UserStatus": "CONFIRMED", "UserCreateDate": older,
                        "Attributes": [{"Name": key, "Value": value}
                                       for key, value in self.cognito.users[TARGET].items()]}]},
            {},
            {"Users": [{"Username": "0000007", "UserStatus": "UNCONFIRMED", "UserCreateDate": newer,
                        "Attributes": [{"Name": "email", "Value": "legacy@example.test"}]}]},
        ]
        response = self.client.get(USERS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True, "users": [
            {"id": "0000007", "username": "legacy", "email": "legacy@example.test", "role": "user",
             "status": "UNCONFIRMED", "joined": newer.isoformat(), "student_id": ""},
            {"id": TARGET, "username": "Original User", "email": "user@example.test", "role": "user",
             "status": "CONFIRMED", "joined": older.isoformat(), "student_id": "000123"},
        ]})
        self.assertEqual(self.cognito.calls, [("get_paginator", {"operation": "list_users"}),
                                             ("paginate", {"UserPoolId": POOL})])
        self.assertEqual(self.session_state(), self.admin_session)

    def test_list_accepts_an_empty_pool(self):
        response = self.client.get(USERS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True, "users": []})

    def test_every_form_field_is_required_even_when_student_id_can_be_blank(self):
        for field in self.form:
            with self.subTest(missing=field):
                self.cognito.calls.clear()
                data = {key: value for key, value in self.form.items() if key != field}
                self.assert_failure(self.client.post(UPDATE_URL, data=data), 400)
                self.assertEqual(self.cognito.calls, [])
                self.assertEqual(self.session_state(), self.admin_session)
        self.assert_failure(self.client.post(UPDATE_URL, json=self.form), 400)
        self.assertEqual(self.cognito.calls, [])

    def test_malformed_fields_are_rejected_before_cloud_reads_or_writes(self):
        invalid = {
            "cognito_username": ["", " \t "],
            "username": ["", " \t "],
            "email": ["", "no-at-sign", "a@@example.test", "a@example", "a..b@example.test",
                      "a b@example.test", "a@-example.test", "a" * 65 + "@example.test"],
            "student_id": ["12a", "1-2", "1 2", "-1", "1.0", "1e3", "1\n2",
                           "\u0661\u0662", "\uff11\uff12", "1" * 65],
            "role": ["", "student", "guest", "ADMIN"],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.cognito.calls.clear()
                    self.assert_failure(self.client.post(UPDATE_URL, data={**self.form, field: value}), 400)
                    self.assertEqual(self.cognito.calls, [])
                    self.assertEqual(self.session_state(), self.admin_session)

    def test_other_update_normalizes_profile_and_admin_confirms_changed_email(self):
        self.cognito.users[TARGET]["email_verified"] = "false"
        before = deepcopy(self.cognito.users)
        response = self.client.post(UPDATE_URL, data=self.form)
        self.assert_success(response)
        self.assert_write({"name": "Updated User", "custom:role": "admin", "email": "new.user+tag@example.net",
                           "email_verified": "true", "custom:student_id": "0000987"})
        self.assertEqual(self.cognito.users[TARGET], {
            **before[TARGET], "name": "Updated User", "custom:role": "admin",
            "email": "new.user+tag@example.net", "email_verified": "true", "custom:student_id": "0000987",
        })
        self.assertEqual(self.cognito.users[ADMIN], before[ADMIN])
        self.assertEqual(set(self.cognito.users), set(before))
        self.assertEqual(self.session_state(), self.admin_session)

    def test_unchanged_email_never_overwrites_existing_verification(self):
        for verified in ("true", "false", None):
            with self.subTest(email_verified=verified):
                self.cognito.calls.clear()
                attributes = self.cognito.users[TARGET]
                attributes.pop("email_verified", None)
                if verified is not None:
                    attributes["email_verified"] = verified
                response = self.client.post(UPDATE_URL, data={
                    **self.form, "email": "  USER@Example.TEST ", "student_id": " 000123 ",
                })
                self.assert_success(response)
                self.assert_write({"name": "Updated User", "custom:role": "admin"})
                self.assertEqual(attributes.get("email_verified"), verified)
                self.assertEqual(attributes["email"], "user@example.test")

    def test_student_id_edit_clear_boundaries_and_legacy_omission(self):
        cases = [
            ("000123", " 000987 ", "000987"),
            ("000123", " \t ", ""),
            ("000123", " 000123 ", None),
            (None, "", None),
            ("", " \t ", None),
            (None, "0", "0"),
            (None, "0" * 64, "0" * 64),
        ]
        for current, submitted, written in cases:
            with self.subTest(current=current, submitted=submitted):
                self.cognito.calls.clear()
                attributes = self.cognito.users[TARGET]
                attributes.pop("custom:student_id", None)
                if current is not None:
                    attributes["custom:student_id"] = current
                response = self.client.post(UPDATE_URL, data={
                    **self.form, "email": "user@example.test", "student_id": submitted,
                })
                self.assert_success(response)
                expected = {"name": "Updated User", "custom:role": "admin"}
                if written is not None:
                    expected["custom:student_id"] = written
                self.assert_write(expected)
                self.assertEqual(attributes.get("custom:student_id", ""), submitted.strip())

    def test_legacy_numeric_username_is_not_used_as_the_student_id(self):
        legacy = "00000123"
        self.cognito.users[legacy] = self.cognito.users.pop(TARGET)
        self.cognito.users[legacy].pop("custom:student_id")
        response = self.client.post(UPDATE_URL, data={
            **self.form, "cognito_username": legacy, "email": "user@example.test", "student_id": "",
        })
        self.assert_success(response)
        self.assert_write({"name": "Updated User", "custom:role": "admin"}, target=legacy)
        self.assertNotIn("custom:student_id", self.cognito.users[legacy])
        self.assertEqual(self.cognito.users[legacy]["sub"], "sub-" + TARGET)

    def test_self_update_changes_session_profile_only_after_success(self):
        # The canonical helper can resolve a token even when the cached session name differs.
        before = {**self.admin_session, "cognito_username": "cached-legacy-alias"}
        self.set_session(before)
        response = self.client.post(UPDATE_URL, data={**self.form, "cognito_username": ADMIN, "role": "user"})
        self.assert_success(response, is_self=True)
        self.assert_write({"name": "Updated User", "custom:role": "user", "email": "new.user+tag@example.net",
                           "email_verified": "true", "custom:student_id": "0000987"}, target=ADMIN)
        self.assertEqual(self.session_state(), {
            **before, "username": "Updated User", "role": "user", "user": "new.user+tag@example.net",
        })

    def test_cognito_failures_are_sanitized_and_leave_the_self_session_unchanged(self):
        cases = [
            ("admin_get_user", ClientError("UserNotFoundException"), 404),
            ("admin_get_user", ClientError("TooManyRequestsException"), 429),
            ("admin_get_user", ClientError("InternalErrorException"), 503),
            ("admin_update_user_attributes", ClientError("AliasExistsException"), 409),
            ("admin_update_user_attributes", ClientError("UserNotFoundException"), 404),
            ("admin_update_user_attributes", ClientError("InvalidParameterException"), 400),
            ("admin_update_user_attributes", ClientError("TooManyRequestsException"), 429),
            ("admin_update_user_attributes", ClientError("AccessDeniedException"), 503),
            ("admin_update_user_attributes", ClientError("NotAuthorizedException"), 503),
            ("admin_update_user_attributes", RuntimeError(UPSTREAM_DETAIL), 503),
        ]
        before = deepcopy(self.cognito.users)
        for operation, error, status in cases:
            with self.subTest(operation=operation, error=str(error)):
                self.cognito.calls.clear()
                self.cognito.failures = {operation: error}
                self.set_session(self.admin_session)
                response = self.client.post(UPDATE_URL, data={**self.form, "cognito_username": ADMIN, "role": "user"})
                self.assert_failure(response, status)
                self.assertEqual(self.session_state(), self.admin_session)
                self.assertEqual(self.cognito.users, before)
                expected = ["admin_get_user"]
                if operation == "admin_update_user_attributes":
                    expected.append(operation)
                self.assertEqual([op for op, _ in self.cognito.calls], expected)
                for _, kwargs in self.cognito.calls:
                    self.assertEqual((kwargs["UserPoolId"], kwargs["Username"]), (POOL, ADMIN))

    def test_student_id_schema_failures_explain_configuration_without_leaking_aws_details(self):
        cases = [
            ("InvalidParameterException", "custom:student_id does not exist"),
            ("InvalidParameterException", "Attribute does not exist in the schema"),
            ("InvalidParameterException", "Attribute schema does not allow this value"),
            ("InvalidParameterException", "Cannot update an immutable attribute"),
            ("NotAuthorizedException", "Cannot write custom:student_id"),
        ]
        before = deepcopy(self.cognito.users)
        for code, message in cases:
            with self.subTest(code=code, message=message):
                self.cognito.failures = {
                    "admin_update_user_attributes": ClientError(code, message + ": " + UPSTREAM_DETAIL),
                }
                response = self.client.post(UPDATE_URL, data={**self.form, "cognito_username": ADMIN})
                payload = self.assert_failure(response, 503)
                self.assertRegex(payload["message"].lower(), r"configur|schema|mutable")
                self.assertEqual(self.session_state(), self.admin_session)
                self.assertEqual(self.cognito.users, before)

    def test_removed_delete_endpoint_never_calls_cognito_or_data_helpers(self):
        for state in (self.admin_session, {"user": "student@example.test", "role": "user"},
                      {"user": "Guest", "role": "guest"}, {}):
            for method in ("POST", "DELETE", "GET"):
                with self.subTest(session=state, method=method):
                    self.set_session(state)
                    response = self.client.open(USERS_URL + "/delete", method=method,
                                                json={"cognito_username": TARGET})
                    self.assertIn(response.status_code, (404, 405))
                    self.assertEqual(self.cognito.calls, [])
                    self.assertEqual(self.session_state(), state)
                    self.data_access.assert_not_called()


if __name__ == "__main__":
    unittest.main()
