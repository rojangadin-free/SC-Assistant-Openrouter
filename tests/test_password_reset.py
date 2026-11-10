"""Standard code-and-password recovery, using real routes and fake Cognito."""

import concurrent.futures
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from fixtures.auth_preview import FakeCognito, create_test_app


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cognito = FakeCognito()
        self.cognito.otp_enabled = False
        self.cognito.users["2024-001"] = {
            "password": "OriginalPass123!", "confirmed": True,
            "attributes": {"email": "owner@example.com", "email_verified": "true", "name": "Owner"},
        }
        self.app = create_test_app(self.cognito, SimpleNamespace(time=lambda: self.now))
        self.client = self.app.test_client()
        self.client.get("/auth")
        with self.client.session_transaction() as state:
            self.client.environ_base["HTTP_X_AUTH_CSRF"] = state["signup_csrf"]
        self.store = self.app.extensions["auth_test"]["reset_store"]
        self.ns = self.app.extensions["auth_test"]["namespace"]
        self.addCleanup(self.app.extensions["auth_test"]["reset_tmp"].cleanup)

    def request_code(self, email="owner@example.com"):
        return self.client.post("/forgot-password", data={"email": email})

    def reset(self, **overrides):
        payload = {"code": "654321", "password": "ChangedPass456!", "confirmPassword": "ChangedPass456!"}
        payload.update(overrides)
        return self.client.post("/reset-password", data=payload)

    def flow_id(self):
        with self.client.session_transaction() as state:
            return state.get("password_reset_id")

    def confirmations(self):
        return [kwargs for operation, kwargs in self.cognito.calls if operation == "confirm_forgot_password"]

    def test_standard_flow_checks_code_and_changes_password_together(self):
        requested = self.request_code()
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.get_json()["reset_state"]["stage"], "code")
        sent = next(k for op, k in self.cognito.calls if op == "forgot_password")
        self.assertEqual(sent["Username"], "2024-001")
        self.assertEqual(sent["SecretHash"], self.ns["get_secret_hash"]("2024-001"))
        self.assertEqual(self.cognito.password_reset_writes, 0)
        self.assertEqual(self.cognito.users["2024-001"]["password"], "OriginalPass123!")
        response = self.reset()
        self.assertTrue(response.get_json()["success"])
        confirmation = self.confirmations()[0]
        self.assertEqual(confirmation, {"ClientId": "offline-client", "Username": "2024-001",
            "SecretHash": sent["SecretHash"], "ConfirmationCode": "654321", "Password": "ChangedPass456!"})
        self.assertEqual(self.cognito.users["2024-001"]["password"], "ChangedPass456!")
        self.assertEqual(self.cognito.password_reset_writes, 1)
        self.assertFalse(any(op in ("initiate_auth", "respond_to_auth_challenge", "admin_set_user_password")
                             for op, _ in self.cognito.calls))
        with self.client.session_transaction() as state:
            self.assertIsNone(state.get("user"))
            self.assertIsNone(state.get("uid"))
            self.assertNotIn("password_reset_id", state)

    def test_wrong_code_keeps_password_unchanged_and_allows_retry(self):
        self.request_code()
        response = self.reset(code="000000")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reset_state"]["stage"], "code")
        self.assertEqual(self.cognito.password_reset_writes, 0)
        self.assertEqual(self.cognito.users["2024-001"]["password"], "OriginalPass123!")
        self.assertTrue(self.reset().get_json()["success"])

    def test_csrf_is_required_for_request_and_confirmation(self):
        self.client.environ_base["HTTP_X_AUTH_CSRF"] = "wrong"
        self.assertEqual(self.request_code().status_code, 403)
        self.assertEqual(self.reset().status_code, 403)
        self.assertEqual(self.cognito.calls, [])

    def test_missing_or_malformed_code_cannot_reset_password(self):
        self.request_code()
        for code in ("", "12345", "1234567", "12345678", "abcdef"):
            with self.subTest(code=code):
                self.assertEqual(self.reset(code=code).status_code, 400)
        self.assertEqual(self.confirmations(), [])
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_password_confirmation_is_checked_on_the_server(self):
        self.request_code()
        for overrides in ({"confirmPassword": "different"}, {"password": "short", "confirmPassword": "short"},
                          {"password": "x" * 257, "confirmPassword": "x" * 257}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.reset(**overrides).status_code, 400)
        self.assertEqual(self.confirmations(), [])

    def test_confirmation_requires_a_requested_reset_context(self):
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(self.confirmations(), [])

    def test_recovery_page_has_all_fields_before_requesting_a_code(self):
        page = self.client.get("/password-reset")
        self.assertIn(b'data-initial-view="reset"', page.data)
        for field in (b'id="resetEmail"', b'id="resetVerificationCode"', b'id="newPassword"', b'id="confirmPassword"'):
            self.assertIn(field, page.data)
        self.assertIn(b'>Send OTP</button>', page.data)
        self.assertNotIn(b'id="forgotPasswordForm"', page.data)
        self.assertNotIn(b'id="resendCodeLink"', page.data)
        self.assertEqual(self.cognito.calls, [])

    def test_unknown_account_uses_the_same_request_and_bad_code_messages(self):
        known = self.request_code().get_json()
        known_error = self.reset(code="000000").get_json()["message"]
        unknown = self.request_code("unknown@example.com").get_json()
        self.assertEqual(known["message"], unknown["message"])
        self.assertEqual(known["reset_state"]["stage"], unknown["reset_state"]["stage"])
        self.assertEqual(self.reset().get_json()["message"], known_error)
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_unverified_account_does_not_receive_a_usable_code(self):
        self.cognito.users["2024-001"]["attributes"]["email_verified"] = "false"
        self.assertTrue(self.request_code().get_json()["success"])
        self.assertNotIn("reset_code", self.cognito.users["2024-001"])
        self.assertFalse(self.reset().get_json()["success"])
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_resend_limit_is_enforced_across_browser_sessions(self):
        self.request_code()
        self.now += 10
        second = self.app.test_client()
        second.get("/password-reset")
        with second.session_transaction() as state:
            second.environ_base["HTTP_X_AUTH_CSRF"] = state["signup_csrf"]
        count = len(self.cognito.calls)
        response = second.post("/forgot-password", data={"email": "OWNER@EXAMPLE.COM"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "20")
        state = response.get_json()["reset_state"]
        self.assertEqual(state["resend_available_at"] - state["server_now"], 20000)
        self.assertEqual(len(self.cognito.calls), count)

    def test_resend_replaces_context_and_old_code(self):
        self.request_code()
        old_id = self.flow_id()
        self.now += 31
        self.request_code()
        self.assertNotEqual(self.flow_id(), old_id)
        self.assertIsNone(self.store.get(old_id))
        self.assertEqual(self.reset(code="654321").status_code, 400)
        self.assertTrue(self.reset(code="654322").get_json()["success"])

    def test_page_reload_does_not_resend_or_restart_the_deadline(self):
        self.request_code()
        deadline = self.store.public_state(self.flow_id())["resend_available_at"]
        calls = len(self.cognito.calls)
        self.now += 10
        page = self.client.get("/password-reset")
        self.assertIn(b'data-initial-view="reset"', page.data)
        self.assertIn(b'name="code"', page.data)
        self.assertNotIn(b'id="resetCodeForm"', page.data)
        self.assertNotIn(b'id="verifyResetCodeButton"', page.data)
        self.assertEqual(self.store.public_state(self.flow_id())["resend_available_at"], deadline)
        self.assertEqual(len(self.cognito.calls), calls)

    def test_local_request_expiry_prevents_confirmation(self):
        self.request_code()
        self.now += 181
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(self.confirmations(), [])

    def test_cognito_code_expiry_never_changes_the_password(self):
        self.request_code()
        self.cognito.users["2024-001"]["reset_expires"] = self.now - 1
        response = self.reset()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reset_state"]["stage"], "expired")
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_attempt_limit_still_blocks_later_confirmations(self):
        self.request_code()
        for _ in range(5):
            self.assertEqual(self.reset(code="000000").status_code, 400)
        count = len(self.confirmations())
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(len(self.confirmations()), count)

    def test_success_consumes_context_even_when_cookie_is_replayed(self):
        self.request_code()
        with self.client.session_transaction() as state:
            old = dict(state)
        self.assertTrue(self.reset().get_json()["success"])
        with self.client.session_transaction() as state:
            state.update(old)
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(self.cognito.password_reset_writes, 1)
        with self.assertRaises(self.ns["ClientError"]):
            self.cognito.confirm_forgot_password(Username="2024-001", ConfirmationCode="654321", Password="AnotherPass789!")
        self.assertEqual(self.cognito.password_reset_writes, 1)

    def test_submitted_username_cannot_change_the_bound_target(self):
        self.request_code()
        response = self.reset(email=" OWNER@EXAMPLE.COM ", username="admin", verified="true")
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self.confirmations()[0]["Username"], "2024-001")

    def test_changed_email_requires_a_code_for_the_displayed_address(self):
        self.request_code()
        response = self.reset(email="different@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Email changed", response.get_json()["message"])
        self.assertEqual(self.confirmations(), [])
        self.assertEqual(self.cognito.password_reset_writes, 0)
        self.assertEqual(self.store.get(self.flow_id())["attempts"], 0)
        self.assertTrue(self.reset(email="owner@example.com").get_json()["success"])

    def test_malformed_displayed_email_cannot_attempt_password_change(self):
        self.request_code()
        self.assertEqual(self.reset(email="not-an-email").status_code, 400)
        self.assertEqual(self.confirmations(), [])
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_password_policy_rejection_keeps_the_code_for_retry(self):
        self.request_code()
        self.cognito.fail_next["confirm_forgot_password"] = "InvalidPasswordException"
        response = self.reset()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reset_state"]["stage"], "code")
        self.assertEqual(self.cognito.password_reset_writes, 0)
        self.assertTrue(self.reset().get_json()["success"])

    def test_retired_verification_route_cannot_grant_a_password_write(self):
        self.request_code()
        calls = len(self.cognito.calls)
        response = self.client.post("/verify-reset-code", data={"code": "654321"})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(len(self.cognito.calls), calls)
        self.assertEqual(self.reset(code="").status_code, 400)
        self.assertEqual(self.cognito.password_reset_writes, 0)

    def test_old_code_first_grants_are_not_native_reset_contexts(self):
        self.request_code()
        with self.store._transaction() as conn:
            conn.execute("UPDATE reset_flows SET stage='verified' WHERE id=?", (self.flow_id(),))
        self.assertEqual(self.store.public_state(self.flow_id())["stage"], "expired")
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(self.confirmations(), [])

    def test_password_and_code_are_not_stored_in_cookie_or_local_flow(self):
        self.request_code()
        self.reset(code="000000")
        with self.client.session_transaction() as state:
            serialized = json.dumps(dict(state))
        serialized += json.dumps(self.store.get(self.flow_id()))
        for sensitive in ("OriginalPass123!", "ChangedPass456!", "654321", "000000", "SECRET_ACCESS_TOKEN"):
            self.assertNotIn(sensitive, serialized)
        flow = self.store.get(self.flow_id())
        self.assertIsNone(flow["challenge"])
        self.assertIsNone(flow["subject"])

    def test_storage_failure_cannot_bypass_request_claim(self):
        self.request_code()
        with patch.object(self.store, "claim_password_change", side_effect=RuntimeError("private storage details")):
            response = self.reset()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private storage details", response.get_data(as_text=True))
        self.assertEqual(self.confirmations(), [])

    def test_cleanup_failure_does_not_allow_a_second_confirmation(self):
        self.request_code()
        with self.client.session_transaction() as state:
            old = dict(state)
        with patch.object(self.store, "finish_password_change", side_effect=OSError("storage unavailable")):
            self.assertTrue(self.reset().get_json()["success"])
        with self.client.session_transaction() as state:
            state.update(old)
        self.assertEqual(self.reset().status_code, 409)
        self.assertEqual(self.cognito.password_reset_writes, 1)

    def test_parallel_submissions_confirm_only_once(self):
        self.request_code()
        with self.client.session_transaction() as state:
            copied = dict(state)
        second = self.app.test_client()
        with second.session_transaction() as state:
            state.update(copied)
        second.environ_base["HTTP_X_AUTH_CSRF"] = copied["signup_csrf"]
        entered, release = threading.Event(), threading.Event()
        original = self.cognito.confirm_forgot_password

        def held(**kwargs):
            entered.set()
            release.wait(3)
            return original(**kwargs)

        self.cognito.confirm_forgot_password = held
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(second.post, "/reset-password", data={
                "code": "654321", "password": "ChangedPass456!", "confirmPassword": "ChangedPass456!",
            })
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.reset().status_code, 409)
            finally:
                release.set()
            self.assertTrue(future.result(timeout=3).get_json()["success"])
        self.assertEqual(self.cognito.password_reset_writes, 1)


if __name__ == "__main__":
    unittest.main()
