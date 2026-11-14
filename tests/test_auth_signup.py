"""Signup and student-ID login regressions; no AWS calls or real emails."""

import base64
import hashlib
import hmac
import json
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import _bootstrap  # noqa: F401
from fixtures.auth_preview import FakeCognito, create_test_app


class SignupTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cognito = FakeCognito()
        self.app = create_test_app(self.cognito, SimpleNamespace(time=lambda: self.now))
        self.client = self.app.test_client()
        self.ns = self.app.extensions["auth_test"]["namespace"]
        self.login_events = self.app.extensions["auth_test"]["login_events"]
        self.addCleanup(self.app.extensions["auth_test"]["reset_tmp"].cleanup)

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
        with patch.dict(sys.modules, {"config": SimpleNamespace(FLASK_SECRET_KEY="offline-test-secret")}):
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
        self.assertEqual(self.login_events, [])
        self.assertTrue(self.client.post("/login", data={**credentials, "email": "2024001"}).get_json()["success"])
        with self.client.session_transaction() as state:
            id_session = dict(state)
        logged_in = self.client.post("/login", data=credentials)
        self.assertTrue(logged_in.get_json()["success"])
        self.assertEqual(len(self.login_events), 2)
        event = self.login_events[0]
        self.assertEqual(set(event), {"id", "at", "uid", "display_name", "email"})
        self.assertEqual(event["uid"], "test-sub-student.one")
        self.assertEqual(event["display_name"], "student.one")
        self.assertEqual(event["email"], "test.student@samarcollege.edu.ph")
        self.assertEqual({k: v for k, v in event.items() if k != "id"},
                         {k: v for k, v in self.login_events[1].items() if k != "id"})
        self.assertNotEqual(event["id"], self.login_events[1]["id"])
        with self.client.session_transaction() as state:
            self.assertEqual(dict(state), id_session)
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
        self.assertEqual(self.login_events, [])

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

    def test_failed_and_missing_credentials_never_record_a_signin(self):
        self.cognito.users["owner"] = {"password": "ExamplePass123!", "confirmed": True,
            "attributes": {"email": "owner@example.invalid", "email_verified": "true", "name": "Owner"}}
        for credentials in ({}, {"email": "owner"}, {"password": "ExamplePass123!"},
                            {"email": "owner", "password": "wrong"},
                            {"email": "missing", "password": "ExamplePass123!"}):
            with self.subTest(credentials=credentials):
                response = self.client.post("/login", data=credentials)
                self.assertFalse(response.get_json()["success"])
                self.assertEqual(self.login_events, [])

    def test_missing_or_unverified_token_email_never_records_a_signin(self):
        base = {"sub": "verified-sub", "email": "owner@example.invalid", "email_verified": True}
        for changes, status in (({"email": None}, 503), ({"email": ""}, 503),
                                ({"email_verified": False}, 403), ({"email_verified": "false"}, 403),
                                ({"email_verified": None}, 403)):
            with self.subTest(changes=changes), patch.dict(self.ns, {"login_user": Mock(return_value={
                "success": True, "auth_result": {"IdToken": "SECRET_ID_TOKEN"},
            })}), patch.object(self.ns["jwt"], "get_unverified_claims", return_value={**base, **changes}):
                response = self.client.post("/login", data={"email": "owner", "password": "ExamplePass123!"})
                self.assertEqual(response.status_code, status)
                self.assertFalse(response.get_json()["success"])
                self.assertEqual(self.login_events, [])
                with self.client.session_transaction() as state:
                    self.assertIsNone(state.get("uid"))

    def test_login_events_use_verified_session_identity_and_name_fallbacks_for_all_roles(self):
        cases = (({"name": "<b>Display & Name</b>", "cognito:username": "account-name"}, "<b>Display & Name</b>", "user"),
                 ({"name": "", "cognito:username": "account-name"}, "account-name", "staff"),
                 ({}, "verified.owner", "admin"))
        for identity, expected_name, role in cases:
            claims = {"sub": "verified-sub", "email": "verified.owner@example.invalid", "email_verified": "true",
                      "custom:role": role, **identity}
            with self.subTest(role=role), patch.dict(self.ns, {"login_user": Mock(return_value={
                "success": True, "auth_result": {"IdToken": "SECRET_ID_TOKEN", "AccessToken": "SECRET_ACCESS_TOKEN"},
            })}), patch.object(self.ns["jwt"], "get_unverified_claims", return_value=claims):
                response = self.client.post("/login", data={"email": "typed-alias", "password": "SECRET_PASSWORD"})
                self.assertTrue(response.get_json()["success"])
                self.assertEqual(response.get_json()["redirect"], "/dashboard" if role == "admin" else "/chat")
                event = self.login_events[-1]
                self.assertEqual(set(event), {"id", "at", "uid", "display_name", "email"})
                self.assertEqual(event["uid"], "verified-sub")
                self.assertEqual(event["email"], "verified.owner@example.invalid")
                self.assertEqual(event["display_name"], expected_name)
                self.assertNotIn("SECRET", json.dumps(event))
                self.assertNotIn("typed-alias", json.dumps(event))
                with self.client.session_transaction() as state:
                    self.assertEqual(event["display_name"], state["username"])
                    self.assertEqual(state["role"], role)
        self.assertEqual(len(self.login_events), 3)

    def test_repeated_successes_are_distinct_but_navigation_and_password_checks_are_not_events(self):
        self.cognito.users["owner"] = {"password": "ExamplePass123!", "confirmed": True,
            "attributes": {"email": "owner@example.invalid", "email_verified": "true", "name": "Owner"}}
        credentials = {"email": "owner", "password": "ExamplePass123!"}
        for _ in range(2):
            self.assertTrue(self.client.post("/login", data=credentials).get_json()["success"])
        self.assertEqual(len(self.login_events), 2)
        self.assertEqual(len({event["id"] for event in self.login_events}), 2)
        for path in ("/auth", "/chat", "/chat", "/signup", "/dashboard", "/login"):
            self.client.get(path)
        # Settings reuses this helper to verify the existing password.
        self.assertTrue(self.ns["login_user"]("owner", "ExamplePass123!")["success"])
        self.assertEqual(len(self.login_events), 2)
        self.client.get("/logout")
        self.now += 1
        self.assertTrue(self.client.post("/login", data=credentials).get_json()["success"])
        self.assertEqual(len({event["id"] for event in self.login_events}), 3)
        self.assertNotEqual(self.login_events[-1]["at"], self.login_events[0]["at"])

    def test_guest_signup_confirmation_and_password_recovery_do_not_record_signins(self):
        self.assertTrue(self.client.post("/guest").get_json()["success"])
        self.assertTrue(self.signup(email="owner@example.invalid").get_json()["success"])
        self.now += 31
        self.assertTrue(self.client.post("/resend-signup-code").get_json()["success"])
        self.assertTrue(self.client.post("/confirm-signup", data={"code": "123456"}).get_json()["success"])
        self.assertTrue(self.client.post("/forgot-password", data={"email": "owner@example.invalid"}).get_json()["success"])
        self.assertTrue(self.client.post("/reset-password", data={
            "code": "654321", "password": "ChangedPass456!", "confirmPassword": "ChangedPass456!",
        }).get_json()["success"])
        self.client.get("/password-reset")
        self.assertEqual(self.login_events, [])

    def test_failed_activity_writer_cannot_fail_auth_or_log_sensitive_error_details(self):
        self.cognito.users["owner"] = {"password": "ExamplePass123!", "confirmed": True,
            "attributes": {"email": "owner@example.invalid", "email_verified": "true", "name": "Owner"}}
        attempts = []

        def failed_writer(**identity):
            attempts.append((identity, dict(self.ns["session"])))
            raise OSError("SECRET_PASSWORD SECRET_TOKEN owner@example.invalid")

        with patch.dict(self.ns, {"record_login": failed_writer}), patch("builtins.print") as output:
            response = self.client.post("/login", data={"email": "owner", "password": "ExamplePass123!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True, "redirect": "/chat"})
        self.assertEqual(len(attempts), 1)
        identity, recorded_session = attempts[0]
        self.assertEqual(identity, {"uid": "test-sub-owner", "display_name": "Owner", "email": "owner@example.invalid"})
        self.assertEqual(recorded_session["uid"], identity["uid"])
        self.assertTrue(recorded_session["start_new_chat"])
        output.assert_called_once_with("[auth] Sign-in activity could not be recorded.")
        with self.client.session_transaction() as state:
            self.assertEqual(state["uid"], "test-sub-owner")
            self.assertEqual(state["user"], "owner@example.invalid")
            self.assertFalse(state["is_guest"])
        self.assertEqual(self.login_events, [])


class StudentIdLoginTests(unittest.TestCase):
    def setUp(self):
        self.student_id = "0002024001"
        self.user = {"password": "ExamplePass123!", "confirmed": True, "attributes": {
            "email": "student.one@example.invalid", "email_verified": "true", "name": "Stored Name",
            "custom:student_id": self.student_id, "custom:role": "user",
        }}
        self.cognito = FakeCognito()
        self.cognito.users["student.one"] = self.user
        self.app = create_test_app(self.cognito, SimpleNamespace(time=lambda: 1000.0))
        self.addCleanup(self.app.extensions["auth_test"]["reset_tmp"].cleanup)
        self.client = self.app.test_client()
        self.ns = self.app.extensions["auth_test"]["namespace"]
        self.login_events = self.app.extensions["auth_test"]["login_events"]
        with self.client.session_transaction() as state:
            state["untouched"] = "existing browser state"
        self.bad_credentials = {"success": False, "message": self.ns["COGNITO_ERROR_MESSAGES"]["NotAuthorizedException"]}

    def login(self, identifier=None, password="ExamplePass123!"):
        return self.client.post("/login", data={"email": self.student_id if identifier is None else identifier,
                                                "password": password})

    def assert_no_signin(self, response):
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(self.login_events, [])
        with self.client.session_transaction() as state:
            self.assertEqual(dict(state), {"untouched": "existing browser state"})

    def test_student_id_uses_canonical_hash_and_verified_token_identity_without_writes(self):
        users_before = json.dumps(self.cognito.users, sort_keys=True)
        claims = {**self.cognito.claims("student.one"), "sub": "verified-token-sub",
                  "email": "verified.owner@example.invalid", "name": "Verified Name",
                  "custom:role": "staff", "custom:data_consent": "false"}
        with patch.object(self.cognito, "get_paginator", wraps=self.cognito.get_paginator) as paginator, \
                patch.object(self.ns["jwt"], "get_unverified_claims", return_value=claims):
            response = self.login(" \t" + self.student_id + "\r\n")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True, "redirect": "/chat"})
        paginator.assert_called_once_with("list_users")
        expected_hash = base64.b64encode(hmac.new(b"offline-secret", b"student.oneoffline-client", hashlib.sha256).digest()).decode()
        self.assertEqual(self.cognito.calls, [
            ("admin_get_user", {"UserPoolId": "offline-pool", "Username": self.student_id}),
            ("list_users", {"UserPoolId": "offline-pool", "Limit": 60}),
            ("admin_get_user", {"UserPoolId": "offline-pool", "Username": "student.one"}),
            ("initiate_auth", {"ClientId": "offline-client", "AuthFlow": "USER_PASSWORD_AUTH", "AuthParameters": {
                "USERNAME": "student.one", "PASSWORD": "ExamplePass123!", "SECRET_HASH": expected_hash,
            }}),
        ])
        self.assertEqual(json.dumps(self.cognito.users, sort_keys=True), users_before)
        with self.client.session_transaction() as state:
            self.assertEqual(state["user"], claims["email"])
            self.assertEqual(state["uid"], claims["sub"])
            self.assertEqual(state["username"], claims["name"])
            self.assertEqual(state["cognito_username"], "student.one")
            self.assertEqual(state["role"], "staff")
            self.assertFalse(state["data_consent"])
            self.assertFalse(state["is_guest"])
        self.assertEqual(len(self.login_events), 1)
        event = self.login_events[0]
        self.assertEqual(set(event), {"id", "at", "uid", "display_name", "email"})
        self.assertEqual((event["uid"], event["display_name"], event["email"]),
                         (claims["sub"], claims["name"], claims["email"]))
        self.assertNotIn(self.student_id, json.dumps(event))
        self.assertNotIn("ExamplePass123!", json.dumps(event))

    def test_resolved_canonical_username_case_is_preserved_in_auth_and_hash(self):
        get_user = self.cognito.admin_get_user

        def canonical_user(**kwargs):
            response = get_user(**kwargs)
            response["Username"] = "Student.One"
            return response

        page = {"Users": [{"Username": "Student.One", "Attributes": [
            {"Name": "custom:student_id", "Value": self.student_id},
        ]}]}
        with patch.object(self.cognito, "list_users", return_value=page), \
                patch.object(self.cognito, "admin_get_user", side_effect=canonical_user) as lookup:
            result = self.ns["login_user"](self.student_id, "ExamplePass123!")
        self.assertTrue(result["success"])
        self.assertEqual(lookup.call_args_list, [
            call(UserPoolId="offline-pool", Username=self.student_id),
            call(UserPoolId="offline-pool", Username="Student.One"),
        ])
        params = self.cognito.calls[-1][1]["AuthParameters"]
        self.assertEqual(params["USERNAME"], "Student.One")
        expected_hash = base64.b64encode(hmac.new(b"offline-secret", b"Student.Oneoffline-client", hashlib.sha256).digest()).decode()
        self.assertEqual(params["SECRET_HASH"], expected_hash)
        self.assertEqual(self.login_events, [])

    def test_zero_and_64_digit_ids_are_strings_and_helper_trims_whitespace(self):
        for ident in ("0", "0" * 64):
            with self.subTest(identifier=ident):
                self.user["attributes"]["custom:student_id"] = ident
                self.cognito.calls.clear()
                result = self.ns["login_user"](" \t" + ident + "\r\n", "ExamplePass123!")
                self.assertTrue(result["success"])
                self.assertEqual(self.cognito.calls[0],
                                 ("admin_get_user", {"UserPoolId": "offline-pool", "Username": ident}))
                self.assertEqual(self.cognito.calls[-1][1]["AuthParameters"]["USERNAME"], "student.one")
        self.assertEqual(self.login_events, [])

    def test_unknown_id_and_bad_password_have_same_generic_response_and_keep_session(self):
        wrong_password = self.login(password="SECRET_WRONG_PASSWORD")
        self.assert_no_signin(wrong_password)
        self.assertEqual(wrong_password.get_json(), self.bad_credentials)
        auth_calls = [kwargs for op, kwargs in self.cognito.calls if op == "initiate_auth"]
        self.assertEqual(len(auth_calls), 1)
        self.assertEqual(auth_calls[0]["AuthParameters"]["USERNAME"], "student.one")
        self.cognito.calls.clear()
        unknown = self.login("0002024999")
        self.assert_no_signin(unknown)
        self.assertEqual(unknown.get_json(), wrong_password.get_json())
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "list_users"])
        self.assertIsNone(self.ns["resolve_student_id_username"]("0002024999"))

    def test_leading_zeros_are_not_coerced_when_comparing_student_ids(self):
        for stored, typed in ((self.student_id, "2024001"), ("2024001", self.student_id)):
            with self.subTest(stored=stored, typed=typed):
                self.user["attributes"]["custom:student_id"] = stored
                self.cognito.calls.clear()
                response = self.login(typed)
                self.assert_no_signin(response)
                self.assertEqual(response.get_json(), self.bad_credentials)
                self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "list_users"])
                self.assertEqual(self.cognito.calls[0][1]["Username"], typed)

    def test_invalid_numeric_inputs_are_not_coerced_or_scanned(self):
        resolver = Mock(side_effect=AssertionError("Invalid input must not use the ID resolver"))
        with patch.dict(self.ns, {"resolve_student_id_username": resolver}), \
                patch.object(self.cognito, "list_users", wraps=self.cognito.list_users) as listing, \
                patch.object(self.cognito, "admin_get_user", wraps=self.cognito.admin_get_user) as lookup:
            for ident in ("12 34", "+123", "-123", "12.34", "1e3", "123\n456",
                          "\u0661\u0662\u0663", "\uff11\uff12\uff13", "0" * 65, 2024001, None):
                with self.subTest(identifier=ident):
                    self.assertFalse(self.ns["login_user"](ident, "ExamplePass123!")["success"])
        resolver.assert_not_called()
        listing.assert_not_called()
        lookup.assert_not_called()
        self.assertEqual(self.login_events, [])

    def test_legacy_numeric_username_wins_collision_without_student_id_scan(self):
        self.cognito.users[self.student_id] = {"password": "LegacyPass456!", "confirmed": True,
            "attributes": {"email": "legacy@example.invalid", "email_verified": "true", "name": "Legacy"}}
        with patch.object(self.cognito, "get_paginator", wraps=self.cognito.get_paginator) as paginator:
            response = self.login(password="LegacyPass456!")
        self.assertTrue(response.get_json()["success"])
        paginator.assert_not_called()
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "initiate_auth"])
        params = self.cognito.calls[-1][1]["AuthParameters"]
        self.assertEqual(params["USERNAME"], self.student_id)
        expected_hash = base64.b64encode(hmac.new(b"offline-secret", (self.student_id + "offline-client").encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(params["SECRET_HASH"], expected_hash)
        with self.client.session_transaction() as state:
            self.assertEqual(state["uid"], "test-sub-" + self.student_id)
            self.assertEqual(state["cognito_username"], self.student_id)
            self.assertEqual(state["user"], "legacy@example.invalid")
        self.assertEqual(len(self.login_events), 1)
        self.assertEqual(self.login_events[0]["email"], "legacy@example.invalid")

    def test_legacy_numeric_wrong_password_never_falls_back_to_other_account(self):
        self.cognito.users[self.student_id] = {"password": "LegacyPass456!", "confirmed": True,
            "attributes": {"email": "legacy@example.invalid", "email_verified": "true"}}
        response = self.login()  # This password belongs to the distinct student-ID account.
        self.assert_no_signin(response)
        self.assertEqual(response.get_json(), self.bad_credentials)
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "initiate_auth"])
        self.assertEqual(self.cognito.calls[-1][1]["AuthParameters"]["USERNAME"], self.student_id)

    def test_numeric_lookup_errors_other_than_user_not_found_fail_closed(self):
        for code in ("NotAuthorizedException", "TooManyRequestsException", "ResourceNotFoundException"):
            with self.subTest(code=code):
                self.cognito.calls.clear()
                self.cognito.fail_next["admin_get_user"] = code
                response = self.login()
                self.assert_no_signin(response)
                self.assertEqual(response.get_json()["message"], self.ns["handle_cognito_error"](self.ns["ClientError"](code)))
                self.assertEqual(self.cognito.calls,
                                 [("admin_get_user", {"UserPoolId": "offline-pool", "Username": self.student_id})])

    def test_duplicate_ids_across_real_pages_are_rejected_even_if_one_is_unconfirmed(self):
        for index in range(59):
            self.cognito.users[f"legacy{index}"] = {"confirmed": True, "password": "unused",
                                                   "attributes": {"email": f"legacy{index}@example.invalid"}}
        self.cognito.users["duplicate"] = {"confirmed": False, "password": "unused", "attributes": {
            "email": "duplicate@example.invalid", "custom:student_id": self.student_id,
        }}
        response = self.login()
        self.assert_no_signin(response)
        self.assertEqual(response.get_json(), self.bad_credentials)
        self.assertEqual(self.cognito.calls, [
            ("admin_get_user", {"UserPoolId": "offline-pool", "Username": self.student_id}),
            ("list_users", {"UserPoolId": "offline-pool", "Limit": 60}),
            ("list_users", {"UserPoolId": "offline-pool", "Limit": 60, "PaginationToken": "60"}),
        ])
        self.assertIsNone(self.ns["resolve_student_id_username"](self.student_id))

    def test_duplicate_after_empty_pages_is_found_without_a_page_count_cutoff(self):
        attrs = [{"Name": "custom:student_id", "Value": self.student_id}]
        pages = [{"Users": [{"Username": "student.one", "Attributes": attrs}], "PaginationToken": "page-1"}]
        pages.extend({"Users": [], "PaginationToken": f"page-{index + 1}"} for index in range(1, 11))
        pages.append({"Users": [{"Username": "duplicate", "Attributes": attrs}]})
        with patch.object(self.cognito, "list_users", side_effect=pages) as listing:
            response = self.login()
        self.assert_no_signin(response)
        self.assertEqual(response.get_json(), self.bad_credentials)
        self.assertEqual(listing.call_args_list, [call(UserPoolId="offline-pool", Limit=60)] + [
            call(UserPoolId="offline-pool", Limit=60, PaginationToken=f"page-{index}") for index in range(1, 12)
        ])
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user"])

    def test_same_username_repeated_and_legacy_missing_attributes_are_not_ambiguity(self):
        row = {"Username": "student.one", "Attributes": [{"Name": "custom:student_id", "Value": self.student_id}]}
        pages = [
            {"Users": [{"Username": "legacy"}, row], "PaginationToken": "next"},
            {"Users": [row, {"Username": "empty", "Attributes": []}, {"Attributes": row["Attributes"]}]},
        ]
        with patch.object(self.cognito, "list_users", side_effect=pages) as listing:
            response = self.login()
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(listing.call_args_list, [call(UserPoolId="offline-pool", Limit=60),
                                                call(UserPoolId="offline-pool", Limit=60, PaginationToken="next")])
        self.assertEqual([kwargs["Username"] for op, kwargs in self.cognito.calls if op == "admin_get_user"],
                         [self.student_id, "student.one"])
        self.assertEqual(self.cognito.calls[-1][1]["AuthParameters"]["USERNAME"], "student.one")
        self.assertEqual(len(self.login_events), 1)

    def test_accounts_without_custom_student_id_do_not_match(self):
        self.user["attributes"].pop("custom:student_id")
        response = self.login()
        self.assert_no_signin(response)
        self.assertEqual(response.get_json(), self.bad_credentials)
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "list_users"])

    def test_authoritative_recheck_rejects_changed_cleared_missing_or_deleted_ids(self):
        stale_page = self.cognito.list_users(UserPoolId="offline-pool", Limit=60)
        for change in ("changed", "cleared", "missing", "deleted"):
            with self.subTest(change=change), patch.object(self.cognito, "list_users", return_value=stale_page):
                self.cognito.users["student.one"] = self.user
                self.user["attributes"]["custom:student_id"] = self.student_id
                if change == "deleted":
                    self.cognito.users.pop("student.one")
                elif change == "missing":
                    self.user["attributes"].pop("custom:student_id")
                else:
                    self.user["attributes"]["custom:student_id"] = "0002024002" if change == "changed" else ""
                self.cognito.calls.clear()
                response = self.login()
                self.assert_no_signin(response)
                self.assertEqual(response.get_json(), self.bad_credentials)
                self.assertEqual(self.cognito.calls, [
                    ("admin_get_user", {"UserPoolId": "offline-pool", "Username": self.student_id}),
                    ("admin_get_user", {"UserPoolId": "offline-pool", "Username": "student.one"}),
                ])
                if change != "deleted":
                    self.assertIsNone(self.ns["resolve_student_id_username"](self.student_id))

    def test_admin_id_change_takes_effect_without_caching_old_alias(self):
        self.assertTrue(self.ns["login_user"](self.student_id, "ExamplePass123!")["success"])
        self.user["attributes"]["custom:student_id"] = "0002024002"
        self.cognito.calls.clear()
        old_id = self.login()
        self.assert_no_signin(old_id)
        self.assertEqual(old_id.get_json(), self.bad_credentials)
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user", "list_users"])
        self.assertTrue(self.login("0002024002").get_json()["success"])
        self.assertEqual(len(self.login_events), 1)
        self.assertEqual(self.login_events[0]["uid"], "test-sub-student.one")
        self.assertEqual(self.login_events[0]["email"], "student.one@example.invalid")

    def test_known_pagination_error_after_a_match_is_handled_without_authenticating(self):
        page = {"Users": [{"Username": "student.one", "Attributes": [
            {"Name": "custom:student_id", "Value": self.student_id},
        ]}], "PaginationToken": "next"}
        error = self.ns["ClientError"]("TooManyRequestsException")
        with patch.object(self.cognito, "list_users", side_effect=[page, error]) as listing:
            response = self.login()
        self.assert_no_signin(response)
        self.assertEqual(response.get_json()["message"], self.ns["handle_cognito_error"](error))
        self.assertEqual(listing.call_count, 2)
        self.assertEqual([op for op, _ in self.cognito.calls], ["admin_get_user"])

    def test_recheck_error_cannot_fall_back_to_a_list_snapshot(self):
        get_user = self.cognito.admin_get_user

        def failed_recheck(**kwargs):
            if kwargs["Username"] == "student.one":
                raise self.ns["ClientError"]("TooManyRequestsException")
            return get_user(**kwargs)

        with patch.object(self.cognito, "admin_get_user", side_effect=failed_recheck) as lookup:
            response = self.login()
        self.assert_no_signin(response)
        self.assertEqual(response.get_json()["message"], self.ns["COGNITO_ERROR_MESSAGES"]["TooManyRequestsException"])
        self.assertEqual(lookup.call_count, 2)
        self.assertNotIn("initiate_auth", [op for op, _ in self.cognito.calls])

    def test_unexpected_lookup_transport_and_pagination_failures_are_sanitized(self):
        sensitive = "SECRET_PASSWORD SECRET_TOKEN student.one@example.invalid " + self.student_id
        page = {"Users": [{"Username": "student.one", "Attributes": [
            {"Name": "custom:student_id", "Value": self.student_id},
        ]}], "PaginationToken": "next"}
        failures = (("admin_get_user", OSError(sensitive)), ("get_paginator", RuntimeError(sensitive)),
                    ("list_users", ConnectionError(sensitive)), ("list_users", [page, RuntimeError(sensitive)]))
        results = []
        for operation, failure in failures:
            with self.subTest(operation=operation, failure=type(failure).__name__), \
                    patch.object(self.cognito, operation, side_effect=failure), patch("builtins.print") as output:
                self.cognito.calls.clear()
                response = self.login()
                self.assert_no_signin(response)
                results.append(response.get_json())
                self.assertEqual(set(response.get_json()), {"success", "message"})
                public_and_logs = response.get_data(as_text=True) + str(output.call_args_list)
                for secret in ("SECRET_PASSWORD", "SECRET_TOKEN", "student.one@example.invalid", self.student_id):
                    self.assertNotIn(secret, public_and_logs)
                self.assertNotIn("initiate_auth", [op for op, _ in self.cognito.calls])
        self.assertTrue(all(result == results[0] for result in results))

    def test_id_helper_failure_never_exposes_exception_or_changes_session(self):
        error = RuntimeError("SECRET_HELPER_DETAIL SECRET_PASSWORD SECRET_TOKEN " + self.student_id)
        resolver = Mock(side_effect=error)
        with patch.dict(self.ns, {"resolve_student_id_username": resolver}), patch("builtins.print") as output:
            response = self.login()
        resolver.assert_called_once_with(self.student_id)
        self.assert_no_signin(response)
        self.assertEqual(set(response.get_json()), {"success", "message"})
        self.assertNotIn("SECRET", response.get_data(as_text=True) + str(output.call_args_list))
        self.assertNotIn(self.student_id, response.get_data(as_text=True) + str(output.call_args_list))
        self.assertEqual(self.cognito.calls, [])

    def test_unconfirmed_or_unverified_id_login_never_grants_session_or_event(self):
        for confirmed, verified in ((False, "false"), (True, "false"), (True, None)):
            with self.subTest(confirmed=confirmed, verified=verified):
                self.user["confirmed"] = confirmed
                if verified is None:
                    self.user["attributes"].pop("email_verified", None)
                else:
                    self.user["attributes"]["email_verified"] = verified
                self.cognito.calls.clear()
                response = self.login()
                self.assert_no_signin(response)
                self.assertEqual(response.status_code, 403 if confirmed else 200)
                self.assertEqual(self.cognito.calls[-1][0], "initiate_auth")
                self.assertEqual(self.cognito.calls[-1][1]["AuthParameters"]["USERNAME"], "student.one")

    def test_email_and_username_login_keep_existing_lookup_behavior(self):
        resolver = Mock(wraps=self.ns["resolve_student_id_username"])
        with patch.dict(self.ns, {"resolve_student_id_username": resolver}), \
                patch.object(self.cognito, "get_paginator", wraps=self.cognito.get_paginator) as paginator:
            for identifier in (" STUDENT.ONE@EXAMPLE.INVALID ", " student.one "):
                with self.subTest(identifier=identifier):
                    self.assertTrue(self.login(identifier).get_json()["success"])
        resolver.assert_not_called()
        paginator.assert_not_called()
        self.assertEqual([kwargs for op, kwargs in self.cognito.calls if op == "list_users"], [
            {"UserPoolId": "offline-pool", "Filter": 'email = "student.one@example.invalid"', "Limit": 60},
        ])
        self.assertNotIn("admin_get_user", [op for op, _ in self.cognito.calls])
        self.assertEqual(len(self.login_events), 2)
        self.assertTrue(all(event["uid"] == "test-sub-student.one" for event in self.login_events))
        expected_hash = base64.b64encode(hmac.new(b"offline-secret", b"student.oneoffline-client", hashlib.sha256).digest()).decode()
        for op, kwargs in self.cognito.calls:
            if op == "initiate_auth":
                self.assertEqual(kwargs["AuthParameters"]["USERNAME"], "student.one")
                self.assertEqual(kwargs["AuthParameters"]["SECRET_HASH"], expected_hash)

    def test_email_resolver_keeps_numeric_username_internal_behavior(self):
        self.assertEqual(self.ns["resolve_cognito_username"](self.student_id), self.student_id)
        self.assertEqual(self.cognito.calls, [])
        self.assertEqual(self.login_events, [])


if __name__ == "__main__":
    unittest.main()
