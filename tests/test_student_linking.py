"""Verified registration -> separate dummy records, without production imports."""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from fixtures.auth_preview import ClientError, FakeCognito, create_test_app

from rag.dummy_students import DummyStudentDatabase, create_database

ROOT = Path(__file__).resolve().parents[1]


def student_adapter():
    forbidden = Mock(name="RealStudentRecords")
    for operation in ("get_item", "query", "scan", "put_item"):
        getattr(forbidden, operation).side_effect = AssertionError("Real student data must not be consulted")
    namespace = {
        "os": os, "students_table": forbidden, "ClientError": ClientError,
        "DummyStudentDatabase": DummyStudentDatabase,
        "Key": lambda name: SimpleNamespace(eq=lambda value: (name, value)),
    }
    tree = ast.parse((ROOT / "aws/students.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "aws/students.py", "exec"), namespace)  # noqa: S102
    return namespace, forbidden


class StudentLinkingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sc_student_link_")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "dummy.sqlite3"
        create_database(self.path, start_id=100000, end_id=100003, batch_size=2)
        self.database = DummyStudentDatabase(self.path)
        environment = patch.dict(os.environ, {
            "STUDENT_RECORDS_BACKEND": "sqlite_demo", "DUMMY_STUDENT_DB_PATH": str(self.path),
            "COGNITO_USER_POOL_ID": "offline-pool",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.adapter, self.real_table = student_adapter()
        self.cognito = FakeCognito()
        self.app = create_test_app(self.cognito, SimpleNamespace(time=lambda: 1000.0))
        self.addCleanup(self.app.extensions["auth_test"]["reset_tmp"].cleanup)
        self.ns = self.app.extensions["auth_test"]["namespace"]
        self.ns.update({name: self.adapter[name] for name in ("dummy_students_enabled", "link_dummy_student_record")})
        self.client = self.app.test_client()
        self.client.get("/signup")
        with self.client.session_transaction() as state:
            self.csrf = state["signup_csrf"]

    def tearDown(self):
        self.assertEqual(self.real_table.mock_calls, [])

    def account(self, username="demo.owner", student_number="100000", *, verified=True, confirmed=True):
        user = {"password": "ExamplePass123!", "confirmed": confirmed, "attributes": {
            "name": username, "email": f"{username}@example.invalid",
            "email_verified": "true" if verified else "false", "custom:student_id": student_number,
            "custom:role": "user", "custom:data_consent": "true",
        }}
        self.cognito.users[username] = user
        return user

    def login(self, username="demo.owner", password="ExamplePass123!", **extra):
        return self.client.post("/login", data={"email": f"{username}@example.invalid", "password": password, **extra})

    def record(self, username="demo.owner"):
        return self.adapter["get_student_by_uid"]("test-sub-" + username)

    def confirm(self, code="123456"):
        return self.client.post("/confirm-signup", data={"code": code}, headers={"X-Auth-CSRF": self.csrf})

    def test_registration_links_only_after_successful_email_verification(self):
        response = self.client.post("/signup", data={
            "username": "demo.owner", "student_id": "100000", "email": "demo.owner@example.invalid",
            "password": "ExamplePass123!", "data_consent": "true",
        }, headers={"X-Auth-CSRF": self.csrf})
        self.assertTrue(response.get_json()["requires_confirmation"])
        self.assertIsNone(self.record())
        self.assertFalse(self.login().get_json()["success"])
        self.assertFalse(self.confirm("999999").get_json()["success"])
        self.assertIsNone(self.record())
        self.assertTrue(self.confirm().get_json()["success"])
        record = self.record()
        self.assertEqual(record["student_number"], "100000")
        self.assertEqual(record["student_id"], "test-sub-demo.owner")
        self.assertIs(record["is_demo"], True)
        with self.client.session_transaction() as state:
            self.assertIsNone(state.get("user"))
        self.assertTrue(self.login(email="100000").get_json()["success"])
        self.assertEqual(self.record(), record)
        with self.client.session_transaction() as state:
            self.assertEqual((state["uid"], state["user"], state["username"]),
                             ("test-sub-demo.owner", "demo.owner@example.invalid", "demo.owner"))

    def test_auto_confirmed_registration_still_checks_verified_account_attributes(self):
        signup = self.ns["sign_up_user"]

        def auto_confirm(*args, **kwargs):
            result = signup(*args, **kwargs)
            user = self.cognito.users[args[0]]
            user["confirmed"] = True
            user["attributes"]["email_verified"] = "true"
            return {**result, "requires_confirmation": False}

        with patch.dict(self.ns, {"sign_up_user": auto_confirm}):
            response = self.client.post("/signup", data={
                "username": "demo.owner", "student_id": "100000", "email": "demo.owner@example.invalid",
                "password": "ExamplePass123!", "data_consent": "true",
            }, headers={"X-Auth-CSRF": self.csrf})
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self.record()["student_number"], "100000")

    def test_login_uses_current_profile_not_typed_identifier_or_token_id(self):
        self.account("100002", "100001")
        claims = {**self.cognito.claims("100002"), "custom:student_id": "100003"}
        with patch.object(self.ns["jwt"], "get_unverified_claims", return_value=claims):
            response = self.client.post("/login", data={"email": "100002", "password": "ExamplePass123!",
                                                         "student_id": "100003"})
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self.record("100002")["student_number"], "100001")

    def test_failed_unconfirmed_unverified_and_guest_logins_never_link(self):
        user = self.account()
        self.assertFalse(self.login(password="WrongPassword123!").get_json()["success"])
        user["confirmed"] = False
        self.assertFalse(self.login().get_json()["success"])
        user["confirmed"] = True
        user["attributes"]["email_verified"] = "false"
        self.assertEqual(self.login().status_code, 403)
        self.assertTrue(self.client.post("/guest").get_json()["success"])
        self.assertIsNone(self.record())

    def test_general_auth_helper_and_password_recovery_do_not_claim_records(self):
        self.account()
        self.assertTrue(self.ns["login_user"]("demo.owner@example.invalid", "ExamplePass123!")["success"])
        response = self.client.post("/forgot-password", data={"email": "demo.owner@example.invalid"},
                                    headers={"X-Auth-CSRF": self.csrf})
        self.assertTrue(response.get_json()["success"])
        self.assertIsNone(self.record())

    def test_duplicate_accounts_cannot_steal_the_record(self):
        self.account()
        self.account("another.owner")
        self.assertTrue(self.login().get_json()["success"])
        original = self.record()
        self.assertTrue(self.login("another.owner").get_json()["success"])
        self.assertIsNone(self.record("another.owner"))
        self.assertEqual(self.record(), original)
        self.assertIsNone(self.adapter["get_student_by_email"]("demo.owner@example.invalid"))

    def test_profile_edits_and_clears_never_move_existing_ownership(self):
        user = self.account()
        self.assertTrue(self.login().get_json()["success"])
        original = self.record()
        for new_id in ("100001", "", "99999"):
            with self.subTest(student_id=new_id):
                user["attributes"]["custom:student_id"] = new_id
                self.assertTrue(self.login().get_json()["success"])
                self.assertEqual(self.record(), original)

    def test_out_of_range_missing_and_leading_zero_ids_do_not_grant_records(self):
        for index, student_id in enumerate(("99999", "3000001", "100009", "0100000", "")):
            with self.subTest(student_id=student_id):
                name = f"outside{index}"
                self.account(name, student_id)
                self.assertTrue(self.login(name).get_json()["success"])
                self.assertIsNone(self.record(name))
                self.assertIsNone(self.adapter["get_student_by_email"](f"{name}@example.invalid"))

    def test_account_and_pool_isolation(self):
        self.account()
        self.assertTrue(self.login().get_json()["success"])
        self.assertIsNone(self.adapter["get_student_by_uid"]("someone-else"))
        with patch.dict(os.environ, {"COGNITO_USER_POOL_ID": "another-pool"}):
            self.assertIsNone(self.record())

    def test_authoritative_account_must_match_verified_email_and_subject(self):
        self.account()
        get_account = self.cognito.admin_get_user
        for changed in ("email", "email_verified", "sub", "missing_sub", "status"):
            with self.subTest(changed=changed):
                account = get_account(UserPoolId="offline-pool", Username="demo.owner")
                if changed == "status":
                    account["UserStatus"] = "UNCONFIRMED"
                elif changed == "missing_sub":
                    account["UserAttributes"] = [a for a in account["UserAttributes"] if a["Name"] != "sub"]
                else:
                    for attr in account["UserAttributes"]:
                        if attr["Name"] == changed:
                            attr["Value"] = "false" if changed == "email_verified" else "another-owner"
                with patch.object(self.cognito, "admin_get_user", return_value=account):
                    self.assertTrue(self.login().get_json()["success"])
                self.assertIsNone(self.record())

    def test_database_or_directory_failure_does_not_break_login_or_expose_details(self):
        self.account()
        error = RuntimeError("SECRET_TOKEN PRIVATE_USER_RECORD")
        with patch.dict(self.ns, {"link_dummy_student_record": Mock(side_effect=error)}), \
                patch("builtins.print") as output:
            response = self.login()
        self.assertTrue(response.get_json()["success"])
        self.assertNotIn("SECRET", response.get_data(as_text=True) + str(output.call_args_list))
        self.assertNotIn("PRIVATE_USER", str(output.call_args_list))
        self.assertIsNone(self.record())
        self.assertTrue(self.login().get_json()["success"])
        self.assertIsNotNone(self.record())

    def test_missing_database_is_not_created_and_never_falls_back_to_real_email(self):
        missing = self.path.with_name("missing.sqlite3")
        self.account()
        with patch.dict(os.environ, {"DUMMY_STUDENT_DB_PATH": str(missing)}):
            self.assertTrue(self.login().get_json()["success"])
            with self.assertRaises(RuntimeError):
                self.record()
            self.assertIsNone(self.adapter["get_student_by_email"]("demo.owner@example.invalid"))
        self.assertFalse(missing.exists())

    def test_unknown_backend_fails_closed(self):
        with patch.dict(os.environ, {"STUDENT_RECORDS_BACKEND": "sqlite_typo"}):
            with self.assertRaises(ValueError):
                self.record()
            with self.assertRaises(ValueError):
                self.adapter["get_student_by_email"]("demo.owner@example.invalid")

    def test_demo_mode_cannot_write_or_bulk_read_real_roster(self):
        with self.assertRaises(RuntimeError):
            self.adapter["upsert_student"]({"student_id": "not-an-account"})
        with self.assertRaises(RuntimeError):
            self.adapter["list_all_students"]()

    def test_default_backend_does_not_link_or_open_sqlite(self):
        with patch.dict(os.environ, {"STUDENT_RECORDS_BACKEND": "dynamodb"}), \
                patch.dict(self.adapter, {"DummyStudentDatabase": Mock(side_effect=AssertionError("No SQLite"))}):
            self.account()
            self.assertTrue(self.login().get_json()["success"])
            self.assertEqual(self.adapter["link_dummy_student_record"]("offline-pool", "uid", "100000"), "disabled")
            self.assertNotIn("admin_get_user", [name for name, _ in self.cognito.calls])
            self.real_table.get_item.side_effect = None
            self.real_table.get_item.return_value = {"Item": {"full_name": "Existing roster"}}
            self.assertEqual(self.record(), {"full_name": "Existing roster"})
            self.real_table.get_item.assert_called_once_with(Key={"student_id": "test-sub-demo.owner"})
            self.real_table.reset_mock()

    def test_demo_record_formatter_and_error_fallback_keep_source_label(self):
        self.account()
        self.assertTrue(self.login().get_json()["success"])
        record = self.record()
        text = self.adapter["format_student_context"](record)
        self.assertIn("SYNTHETIC DEMO DATA - NOT OFFICIAL STUDENT RECORDS", text)
        self.assertIn("Demo Student 100000", text)
        self.assertIn("BALANCE DUE", text)
        self.assertNotIn("STUDENT PERSONAL ACADEMIC RECORD", text)
        with patch("builtins.print"):
            text = self.adapter["format_student_context"]({**record, "grades": [None]})
        self.assertIn("SYNTHETIC DEMO DATA - NOT OFFICIAL STUDENT RECORDS", text)


if __name__ == "__main__":
    unittest.main()
