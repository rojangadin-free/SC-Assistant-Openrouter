"""Signup policy and verification regressions; no AWS calls or real emails."""

import base64
import hashlib
import hmac
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401
from fixtures.auth_preview import FakeCognito, create_test_app


class SignupTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cognito = FakeCognito()
        self.app = create_test_app(self.cognito, SimpleNamespace(time=lambda: self.now))
        self.client = self.app.test_client()
        self.ns = self.app.extensions["auth_test"]["namespace"]

    def signup(self, **changes):
        self.client.get("/signup")
        with self.client.session_transaction() as state:
            self.client.environ_base["HTTP_X_AUTH_CSRF"] = state.get("signup_csrf", "")
        data = {"username": "student.one", "student_id": "2024001", "email": "test.student@samarcollege.edu.ph",
                "password": "ExamplePass123!", "confirm_password": "ExamplePass123!", "data_consent": "true"}
        data.update(changes)
        return self.client.post("/signup", data=data)

    def pending(self):
        with self.client.session_transaction() as state:
            return dict(state.get("pending_signup") or {})

    def rendered_signup_state(self):
        page = self.client.get("/signup").get_data(as_text=True)
        return json.loads(re.search(r'<script type="application/json" id="signupState">(.*?)</script>', page).group(1))

    def test_signup_pages_and_visible_entrypoint_render(self):
        login = self.client.get("/auth")
        signup = self.client.get("/signup")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b'id="switchToSignup"', login.data)
        self.assertEqual(signup.status_code, 200)
        self.assertEqual(login.headers["Cache-Control"], "no-store")
        self.assertEqual(signup.headers["Cache-Control"], "no-store")
        self.assertNotIn(b'name="signup_method"', signup.data)
        self.assertNotIn(b'id="signupMethodEmail"', signup.data)
        self.assertNotIn(b'LMS email', signup.data)
        self.assertIn(b'name="student_id"', signup.data)
        self.assertIn(b'name="username"', signup.data)
        self.assertNotIn(b'id="signupStudentIdHint"', signup.data)
        self.assertNotIn(b'id="signupIdNotice"', signup.data)

    def test_signup_is_excluded_from_the_pwa_cache(self):
        from sc_assistant.pwa import CACHE_VERSION, bp_pwa
        self.app.register_blueprint(bp_pwa)
        script = self.client.get("/sw.js").get_data(as_text=True)
        for path in ("/signup", "/confirm-signup", "/resend-signup-code"):
            self.assertIn(f"'{path}'", script)
        self.assertIn(CACHE_VERSION, script)

    def test_invalid_email_syntax_is_rejected_before_cognito(self):
        invalid = ["not-an-email", "a@gmail.com.", "a@evil.com@gmail.com", "@gmail.com", "a b@gmail.com",
                   ".a@gmail.com", "a.@gmail.com", "a..b@gmail.com", "a\nb@gmail.com",
                   "Name <a@gmail.com>", "x" * 65 + "@gmail.com", "a@-gmail.com", "a@gmail..com",
                   "a@gmail.com/path", "a@localhost", "a@", ""]
        for email in invalid:
            with self.subTest(email=email):
                response = self.signup(email=email)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])
        self.assertEqual(self.cognito.calls, [])

    def test_personal_school_and_other_email_domains_are_accepted(self):
        for i, email in enumerate(("a@gmail.com", "b@yahoo.com", "c@outlook.com", "d@example.org",
                                   "e@samarcollege.edu.ph", "f@dept.samarcollege.edu.ph", "g+tag@company.net")):
            with self.subTest(email=email):
                response = self.signup(username=f"student{i}", student_id=f"2024{i}", email=email)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["requires_confirmation"])
                self.assertEqual(self.pending()["email"], email)

    def test_direct_signup_helper_accepts_personal_email_but_checks_format(self):
        result = self.ns["sign_up_user"]("student.one", "person@gmail.com", "ExamplePass123!", student_id="2024001")
        self.assertTrue(result["success"])
        count = len(self.cognito.calls)
        result = self.ns["sign_up_user"]("student.two", "invalid", "ExamplePass123!", student_id="2024002")
        self.assertFalse(result["success"])
        self.assertEqual(len(self.cognito.calls), count)

    def test_signup_keeps_username_id_and_email_separate_without_granting_a_session(self):
        response = self.signup(email="  Test.Student@GMAIL.COM  ", username="  Student.One_1  ", role="admin")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["requires_confirmation"])
        pending = self.pending()
        self.assertEqual(pending["email"], "test.student@gmail.com")
        self.assertEqual(pending["username"], "Student.One_1")
        self.assertNotIn("@", pending["username"])
        attrs = self.cognito.users[pending["username"].lower()]["attributes"]
        self.assertEqual(attrs["name"], "Student.One_1")
        self.assertEqual(attrs["custom:student_id"], "2024001")
        self.assertNotIn("custom:role", attrs)
        self.assertNotIn("email_verified", attrs)
        with self.client.session_transaction() as state:
            self.assertIsNone(state.get("uid"))
            self.assertIsNone(state.get("user"))
            self.assertNotIn("ExamplePass123!", json.dumps(dict(state)))

    def test_id_signup_preserves_leading_zeros_with_personal_email(self):
        response = self.signup(student_id="  0002024007  ", email="someone@outlook.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pending()["username"], "student.one")
        request = self.cognito.calls[0][1]
        self.assertEqual(request["Username"], "student.one")
        self.assertEqual({a["Name"]: a["Value"] for a in request["UserAttributes"]}["custom:student_id"], "0002024007")
        self.assertEqual({a["Name"]: a["Value"] for a in request["UserAttributes"]}["email"],
                         "someone@outlook.com")

    def test_id_is_required_and_removed_email_only_mode_is_rejected(self):
        for ident in ("", "letters", "ID123", "2024-001", "12 34", "id@123", "ID_123", "../123",
                      "-123", "+123", "12.34", "1e3", "\u0661\u0662\u0663", "\uff11\uff12\uff13", "1" * 65):
            with self.subTest(identifier=ident):
                response = self.signup(signup_method="id", student_id=ident)
                self.assertEqual(response.status_code, 400)
                self.assertIn("digits (0-9) only", response.get_json()["message"])
        for method in ("email", "", "unknown"):
            self.assertEqual(self.signup(signup_method=method).status_code, 400)
        self.assertEqual(self.cognito.calls, [])

    def test_numeric_id_length_boundaries_preserve_the_exact_string(self):
        for index, ident in enumerate(("0", "0" * 64)):
            with self.subTest(identifier=ident):
                response = self.signup(username=f"boundary{index}", student_id=ident, email=f"boundary{index}@example.com")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.pending()["username"], f"boundary{index}")
                self.assertEqual(self.cognito.users[f"boundary{index}"]["attributes"]["custom:student_id"], ident)

    def test_username_is_required_and_invalid_names_never_reach_cognito(self):
        for username in ("", "   ", "user name", "user@example.com", "user/name", "user\nname", "na\u00efve", "u" * 65):
            with self.subTest(username=username):
                response = self.signup(username=username)
                self.assertEqual(response.status_code, 400)
                self.assertIn("username", response.get_json()["message"])
        self.assertEqual(self.cognito.calls, [])

    def test_duplicate_username_does_not_replace_an_existing_account(self):
        self.signup()
        self.client.post("/restart-signup")
        response = self.signup(student_id="999999", email="different@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pending(), {})
        self.assertEqual(self.cognito.users["student.one"]["attributes"]["custom:student_id"], "2024001")
        self.assertEqual(self.cognito.users["student.one"]["attributes"]["email"], "test.student@samarcollege.edu.ph")

    def test_missing_signup_schema_or_write_access_is_a_configuration_error(self):
        for code, message in (
            ("InvalidParameterException", "custom:student_id: Attribute does not exist in the schema."),
            ("InvalidParameterException", "custom:role: Attribute does not exist in the schema."),
            ("NotAuthorizedException", "A client attempted to write unauthorized attribute."),
        ):
            with self.subTest(code=code):
                error = self.ns["ClientError"](code)
                error.response["Error"]["Message"] = message
                self.cognito.sign_up = Mock(side_effect=error)
                response = self.signup()
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.get_json()["configuration_error"])
                self.assertIn("configuration", response.get_json()["message"])
                self.assertEqual(self.pending(), {})
                self.assertEqual(self.cognito.users, {})

    def test_unrelated_cognito_parameter_errors_remain_bad_requests(self):
        error = self.ns["ClientError"]("InvalidParameterException")
        error.response["Error"]["Message"] = "Invalid username format."
        self.cognito.sign_up = Mock(side_effect=error)
        response = self.signup()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json().get("configuration_error", False))
        self.assertEqual(self.pending(), {})

    def test_missing_role_claim_defaults_to_an_ordinary_user(self):
        with patch.object(self.ns["jwt"], "get_unverified_claims", return_value={"sub": "offline-user"}):
            self.assertEqual(self.ns["get_user_role_from_claims"]("offline-token"), "user")

    def test_consent_password_bounds_and_match_are_server_enforced(self):
        for change in ({"data_consent": "false"}, {"password": "short"},
                       {"password": "p" * 257}, {"confirm_password": "different"}):
            self.assertEqual(self.signup(**change).status_code, 400)
        self.assertEqual(self.cognito.calls, [])

    def test_optional_password_confirmation_can_be_left_blank(self):
        self.assertEqual(self.signup(confirm_password="").status_code, 200)

    def test_signup_does_not_authenticate_until_code_and_password_signin(self):
        self.signup(signup_method="id", student_id="2024001")
        credentials = {"email": "student.one", "password": "ExamplePass123!"}
        self.assertFalse(self.client.post("/login", data=credentials).get_json()["success"])
        bad = self.client.post("/confirm-signup", data={"code": "111111"})
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(self.pending())
        verified = self.client.post("/confirm-signup", data={"code": "123456", "username": "someone-else"})
        self.assertTrue(verified.get_json()["success"])
        self.assertEqual(verified.get_json()["redirect"], "/auth")
        with self.client.session_transaction() as state:
            self.assertIsNone(state.get("uid"))
            self.assertNotIn("pending_signup", state)
        confirmed_call = next(kwargs for op, kwargs in self.cognito.calls if op == "confirm_sign_up"
                              and kwargs["ConfirmationCode"] == "123456")
        self.assertEqual(confirmed_call["Username"], "student.one")
        self.assertIs(confirmed_call["ForceAliasCreation"], False)
        expected_hash = base64.b64encode(hmac.new(b"offline-secret", b"student.oneoffline-client", hashlib.sha256).digest()).decode()
        self.assertEqual(confirmed_call["SecretHash"], expected_hash)
        self.assertFalse(self.client.post("/login", data={**credentials, "email": "2024001"}).get_json()["success"])
        logged_in = self.client.post("/login", data=credentials)
        self.assertTrue(logged_in.get_json()["success"])
        with self.client.session_transaction() as state:
            self.assertEqual(state["user"], "test.student@samarcollege.edu.ph")
            self.assertEqual(state["username"], "student.one")
            self.assertEqual(state["cognito_username"], "student.one")
            self.assertEqual(state["role"], "user")
            self.assertFalse(state["is_guest"])
            self.assertTrue(state["data_consent"])

    def test_confirmation_requires_bound_session_and_six_digits(self):
        self.assertEqual(self.client.post("/confirm-signup", data={"code": "123456"}).status_code, 403)
        self.assertEqual(self.client.post("/resend-signup-code").status_code, 403)
        self.signup()
        before = len(self.cognito.calls)
        for code in ("", "12345", "abcdef", "1234567"):
            self.assertEqual(self.client.post("/confirm-signup", data={"code": code}).status_code, 400)
        self.assertEqual(len(self.cognito.calls), before)

    def test_signup_token_is_required_and_bound_to_the_browser_session(self):
        self.client.get("/signup")
        with self.client.session_transaction() as state:
            token = state["signup_csrf"]
        payload = {"username": "student.one", "student_id": "2024001", "email": "safe@gmail.com",
                   "password": "ExamplePass123!", "data_consent": "true"}
        self.assertEqual(self.client.post("/signup", data=payload).status_code, 403)
        self.assertEqual(self.client.post("/signup", data=payload,
                                         headers={"X-Auth-CSRF": chr(233)}).status_code, 403)
        other = self.app.test_client()
        self.assertEqual(other.post("/signup", data=payload, headers={"X-Auth-CSRF": token}).status_code, 403)
        self.assertEqual(self.cognito.calls, [])

    def test_resend_is_bound_to_pending_account_and_has_cooldown(self):
        self.signup()
        pending = self.pending()
        early = self.client.post("/resend-signup-code")
        self.assertEqual(early.status_code, 429)
        self.now += 31
        sent = self.client.post("/resend-signup-code", data={"username": "attacker", "email": "evil@example.com"})
        self.assertTrue(sent.get_json()["success"])
        kwargs = self.cognito.calls[-1][1]
        self.assertEqual(kwargs["Username"], pending["username"])
        self.assertEqual(self.client.post("/resend-signup-code").status_code, 429)

    def test_navigation_uses_remaining_cooldown_without_sending_codes(self):
        result = self.signup().get_json()
        initial = result["signup_state"]
        self.assertEqual(initial["resend_available_at"] - initial["server_now"], 30000)
        calls = len(self.cognito.calls)
        self.now += 12
        self.client.get("/auth")
        state = self.rendered_signup_state()
        self.assertEqual(state["resend_available_at"], initial["resend_available_at"])
        self.assertEqual(state["resend_available_at"] - state["server_now"], 18000)
        self.now += 19
        state = self.rendered_signup_state()
        self.assertLessEqual(state["resend_available_at"], state["server_now"])
        self.assertEqual(len(self.cognito.calls), calls)

    def test_rate_limit_and_failed_delivery_return_server_timing(self):
        self.signup()
        self.now += 7
        early = self.client.post("/resend-signup-code")
        self.assertEqual(early.status_code, 429)
        self.assertEqual(early.headers["Retry-After"], "23")
        state = early.get_json()["signup_state"]
        self.assertEqual(state["resend_available_at"] - state["server_now"], 23000)
        self.now += 24
        self.cognito.fail_next["resend_confirmation_code"] = "TooManyRequestsException"
        failure = self.client.post("/resend-signup-code")
        self.assertEqual(failure.status_code, 400)
        state = failure.get_json()["signup_state"]
        self.assertEqual(state["resend_available_at"] - state["server_now"], 30000)

    def test_restart_returns_default_signup_without_changing_accounts(self):
        self.signup(signup_method="id", student_id="00012024")
        original = self.pending()
        calls = len(self.cognito.calls)
        response = self.client.post("/restart-signup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect"], "/signup")
        self.assertEqual(self.pending(), {})
        self.assertEqual(len(self.cognito.calls), calls)
        page = self.client.get("/signup").get_data(as_text=True)
        self.assertIn('data-initial-view="signup"', page)
        self.assertNotIn('id="signupChangeEmailForm"', page)
        user = self.cognito.users[original["username"]]
        self.assertEqual(user["attributes"]["email"], original["email"])
        self.assertEqual(user["password"], "ExamplePass123!")
        self.assertFalse(user["confirmed"])

    def test_new_signup_after_restart_uses_the_new_email_and_password(self):
        self.signup()
        original = self.pending()
        self.client.post("/restart-signup")
        response = self.signup(username="corrected.student", student_id="2024002", email="corrected@gmail.com", password="RestartedPass456!",
                               confirm_password="RestartedPass456!")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pending()["email"], "corrected@gmail.com")
        self.assertNotEqual(self.pending()["username"], original["username"])
        self.assertEqual(self.cognito.users[original["username"]]["password"], "ExamplePass123!")
        self.assertTrue(self.client.post("/confirm-signup", data={"code": "123456"}).get_json()["success"])
        self.assertTrue(self.client.post("/login", data={"email": "corrected@gmail.com",
                                                        "password": "RestartedPass456!"}).get_json()["success"])

    def test_restart_requires_csrf_and_does_not_change_signed_in_accounts(self):
        self.assertEqual(self.client.post("/restart-signup").status_code, 403)
        self.signup()
        self.client.environ_base["HTTP_X_AUTH_CSRF"] = "wrong"
        count = len(self.cognito.calls)
        self.assertEqual(self.client.post("/restart-signup").status_code, 403)
        with self.client.session_transaction() as state:
            self.client.environ_base["HTTP_X_AUTH_CSRF"] = state["signup_csrf"]
            state["user"] = "signed-in@samarcollege.edu.ph"
            state["uid"] = "existing-user"
        self.assertEqual(self.client.post("/restart-signup").status_code, 409)
        self.assertEqual(len(self.cognito.calls), count)

    def test_failed_delivery_has_resend_path_without_authentication(self):
        self.cognito.fail_next["sign_up"] = "CodeDeliveryFailureException"
        response = self.signup()
        self.assertTrue(response.get_json()["requires_confirmation"])
        self.assertTrue(response.get_json()["delivery_pending"])
        with self.client.session_transaction() as state:
            self.assertNotIn("uid", state)

    def test_guest_transition_has_no_authenticated_signup_session(self):
        self.client.post("/guest")
        self.signup()
        with self.client.session_transaction() as state:
            self.assertEqual(state["user"], "guest")
            self.assertIsNone(state["uid"])
        self.client.post("/confirm-signup", data={"code": "123456"})
        self.client.post("/login", data={"email": "TEST.STUDENT@SAMARCOLLEGE.EDU.PH", "password": "ExamplePass123!"})
        with self.client.session_transaction() as state:
            self.assertFalse(state["is_guest"])
            self.assertNotIn("pending_signup", state)

    def test_unverified_personal_email_cannot_gain_account_session(self):
        self.signup(email="test.student@gmail.com")
        user = self.cognito.users[self.pending()["username"]]
        user["confirmed"] = True
        response = self.client.post("/login", data={"email": "test.student@gmail.com", "password": "ExamplePass123!"})
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as state:
            self.assertIsNone(state.get("uid"))

    def test_verified_account_is_preferred_over_abandoned_signup(self):
        self.cognito.list_users = Mock(side_effect=[
            {"Users": [{"Username": "abandoned", "UserStatus": "UNCONFIRMED"}], "PaginationToken": "next"},
            {"Users": [{"Username": "owner", "UserStatus": "CONFIRMED",
                         "Attributes": [{"Name": "email_verified", "Value": "true"}]}]},
        ])
        self.assertEqual(self.ns["resolve_cognito_username"]("TEST.STUDENT@SAMARCOLLEGE.EDU.PH"), "owner")
        self.assertEqual(self.cognito.list_users.call_args.kwargs["PaginationToken"], "next")

    def test_existing_external_email_accounts_can_still_sign_in(self):
        self.cognito.users["legacy"] = {"password": "ExamplePass123!", "confirmed": True,
            "attributes": {"email": "legacy@example.com", "email_verified": "true", "name": "Legacy", "custom:role": "user"}}
        response = self.client.post("/login", data={"email": "legacy", "password": "ExamplePass123!"})
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self.client.get("/signup").status_code, 302)
        self.assertEqual(self.client.get("/auth").status_code, 302)
        self.assertEqual(self.signup().status_code, 409)

    def test_existing_numeric_username_still_signs_in_without_a_student_id_attribute(self):
        self.cognito.users["0002024001"] = {"password": "ExamplePass123!", "confirmed": True,
            "attributes": {"email": "legacy@example.com", "email_verified": "true", "name": "0002024001"}}
        response = self.client.post("/login", data={"email": "0002024001", "password": "ExamplePass123!"})
        self.assertTrue(response.get_json()["success"])
        with self.client.session_transaction() as state:
            self.assertEqual(state["cognito_username"], "0002024001")


if __name__ == "__main__":
    unittest.main()
