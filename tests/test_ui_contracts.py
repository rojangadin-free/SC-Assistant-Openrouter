"""Rendered UI contracts: no production models, accounts, or network requests."""

import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import _bootstrap  # noqa: F401

from tools.ui_preview import app


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def by_id(self, ident):
        return next((item for item in self.elements if item[1].get("id") == ident), None)


class UIContracts(unittest.TestCase):
    def page(self, path):
        response = app.test_client().get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        return html, Elements(html)

    def test_major_pages_render_with_unique_control_ids(self):
        for path in ("/", "/guest-chat", "/dashboard", "/login", "/signup"):
            with self.subTest(path=path):
                _, dom = self.page(path)
                counts = Counter(attrs["id"] for _, attrs in dom.elements if attrs.get("id"))
                self.assertEqual([key for key, n in counts.items() if n > 1], [])

    def test_chat_controls_and_history_targets_remain(self):
        html, dom = self.page("/")
        for ident in ("sidebar", "sidebarCollapseBtn", "hamburgerMenu", "themeToggle", "messagesContainer",
                      "emptyChatState", "conversationList", "messageForm", "messageInput", "sendButton", "micBtn"):
            self.assertIsNotNone(dom.by_id(ident), ident)
        self.assertEqual(dom.by_id("sendButton")[1]["aria-label"], "Send message")
        self.assertIn("college-brand", html)
        self.assertIn("Ask about Samar College.", html)

    def test_chat_has_floating_controls_without_a_header_bar(self):
        for path in ("/", "/guest-chat"):
            html, dom = self.page(path)
            classes = {name for _, attrs in dom.elements for name in attrs.get("class", "").split()}
            self.assertNotIn("header", classes)
            self.assertNotIn("header-left", classes)
            self.assertIn("chat-floating-controls", classes)
            self.assertNotIn("<span>Assistant</span>", html)
            self.assertIsNotNone(dom.by_id("themeToggle"))
            self.assertIsNotNone(dom.by_id("hamburgerMenu"))
            self.assertIsNotNone(dom.by_id("repliesToggle"))
            self.assertIn("Ask about Samar College.", html)
        dashboard, dom = self.page("/dashboard")
        self.assertTrue(any(attrs.get("class") == "header" for _, attrs in dom.elements))
        self.assertNotIn("chat-floating-controls", dashboard)
        self.assertIn("<span>Dashboard</span>", dashboard)

    def test_guest_signin_is_a_named_link_and_not_shown_to_signed_in_users(self):
        html, guest = self.page("/guest-chat")
        tag, attrs = guest.by_id("guestSignInButton")
        self.assertEqual(tag, "a")
        self.assertEqual(attrs["aria-label"], "Sign in to save your conversations")
        self.assertIn(attrs["href"], ("/auth", "/login"))
        self.assertIn("Save your conversations", html)
        _, signed_in = self.page("/")
        self.assertIsNone(signed_in.by_id("guestSignInButton"))

    def test_dashboard_navigation_is_native_and_keeps_existing_ids(self):
        _, dom = self.page("/dashboard")
        for ident in ("analyticsMenuItem", "reportsMenuItem", "escalationsMenuItem", "feedbackMenuItem",
                      "conflictsMenuItem", "uploadsMenuItem", "usersMenuItem", "calendarMenuItem", "announcementsMenuItem"):
            tag, attrs = dom.by_id(ident)
            self.assertEqual(tag, "button", ident)
            self.assertEqual(attrs["type"], "button", ident)
        for ident in ("analyticsVolumeChart", "analyticsHourlyChart", "analyticsTopicsChart", "analyticsDays"):
            self.assertIsNotNone(dom.by_id(ident), ident)

    def test_user_editor_is_a_named_dialog_with_non_submit_close_controls(self):
        _, dom = self.page("/dashboard")
        modal = dom.by_id("editUserModal")[1]
        self.assertEqual(modal["role"], "dialog")
        self.assertEqual(modal["aria-modal"], "true")
        self.assertEqual(modal["aria-labelledby"], "editUserTitle")
        self.assertIsNotNone(dom.by_id("editUserTitle"))
        for ident in ("closeEditUserModal", "cancelEditUser"):
            self.assertEqual(dom.by_id(ident)[1]["type"], "button")
        self.assertEqual(dom.by_id("closeEditUserModal")[1]["aria-label"], "Close user editor")
        self.assertIn("disabled", dom.by_id("editUserEmail")[1])
        self.assertEqual(dom.by_id("editUserCognitoUsername")[1]["name"], "cognito_username")

    def test_user_preview_supplies_the_editor_api_contract(self):
        response = app.test_client().get("/api/dashboard/users").get_json()
        self.assertTrue(response["success"])
        for user in response["users"]:
            self.assertTrue({"id", "username", "email", "role", "status", "joined"} <= user.keys())
            self.assertIn(user["role"], ("user", "admin"))

    def test_user_search_is_labeled_and_announces_results(self):
        _, dom = self.page("/dashboard")
        search = dom.by_id("userSearch")[1]
        self.assertEqual(search["type"], "search")
        self.assertEqual(search["aria-controls"], "userListTable")
        self.assertEqual(search["aria-describedby"], "userSearchStatus")
        self.assertIn("disabled", search)
        self.assertTrue(any(tag == "label" and attrs.get("for") == "userSearch" for tag, attrs in dom.elements))
        self.assertEqual(dom.by_id("userSearchStatus")[1]["role"], "status")

    def test_admin_mobile_editors_keep_controls_without_fixed_inline_grids(self):
        html, dom = self.page("/dashboard")
        self.assertIn("admin-editor-grid calendar-editor-grid", html)
        self.assertIn("admin-editor-grid announcement-editor-grid", html)
        self.assertNotIn("grid-template-columns:1.4fr .9fr .9fr 1.4fr auto", html)
        self.assertNotIn("grid-template-columns:1fr 1fr 1fr 1fr auto", html)
        for ident in ("calPreviewQ", "calScanYear"):
            self.assertNotIn("style", dom.by_id(ident)[1])
        self.assertIsNotNone(dom.by_id("userTableHint"))
        self.assertIsNotNone(dom.by_id("reportsTableHint"))

    def test_report_dialog_has_accessible_controls_and_scrollable_context(self):
        _, dom = self.page("/dashboard")
        modal = dom.by_id("reportDetailModal")[1]
        self.assertEqual(modal["role"], "dialog")
        self.assertEqual(modal["aria-modal"], "true")
        self.assertEqual(modal["aria-labelledby"], "reportDetailTitle")
        self.assertEqual(dom.by_id("rd-full-conversation")[1]["tabindex"], "0")
        for ident in ("toggleFullScreenBtn", "closeReportDetailBtn", "cancelReportDetailBtn", "rdIgnoreBtn", "rdDoneBtn"):
            self.assertEqual(dom.by_id(ident)[1]["type"], "button")

    def test_styles_keep_accessibility_layer_and_scoped_utilities(self):
        for path in ("/", "/dashboard", "/login"):
            _, dom = self.page(path)
            links = [a["href"] for tag, a in dom.elements if tag == "link" and a.get("rel") == "stylesheet"]
            self.assertTrue(any("ui.utilities.css" in href for href in links))
            polish = next(i for i, href in enumerate(links) if "polish.css" in href)
            console = next(i for i, href in enumerate(links) if "console.css" in href)
            self.assertLess(polish, console)
        source = Path("sc_assistant/static/css/ui.input.css").read_text(encoding="utf-8")
        self.assertIn("prefix(ui)", source)
        self.assertNotIn('"tailwindcss/preflight.css"', source)
        compiled = Path("sc_assistant/static/css/ui.utilities.css").read_text(encoding="utf-8")
        self.assertIn("ui\\:grid", compiled)

    def test_authentication_fields_and_verification_stages_stay_present(self):
        html, dom = self.page("/signup")
        for ident in ("signupForm", "signupUsername", "signupStudentId", "signupEmail", "signupPassword", "signupDataConsent",
                      "signupVerifyForm", "signupCode", "resetFlowForm", "resetVerificationCode", "resetState", "signupState", "eulaModal"):
            self.assertIsNotNone(dom.by_id(ident), ident)
        self.assertIn("required", dom.by_id("signupStudentId")[1])
        self.assertIn("required", dom.by_id("signupUsername")[1])
        self.assertEqual(dom.by_id("signupUsername")[1]["name"], "username")
        self.assertEqual(dom.by_id("signupUsername")[1]["autocomplete"], "username")
        self.assertEqual(dom.by_id("signupStudentId")[1]["autocomplete"], "off")
        self.assertEqual(dom.by_id("signupStudentId")[1]["type"], "text")
        self.assertEqual(dom.by_id("signupStudentId")[1]["inputmode"], "numeric")
        self.assertEqual(dom.by_id("signupStudentId")[1]["pattern"], "[0-9]{1,64}")
        self.assertIsNone(dom.by_id("signupEmailHint"))
        self.assertIsNone(dom.by_id("signupStudentIdHint"))
        self.assertIsNone(dom.by_id("signupIdNotice"))
        self.assertNotIn("aria-describedby", dom.by_id("signupStudentId")[1])
        self.assertNotIn("Your ID number is your username. Verify your email before signing in.", html)
        self.assertEqual(dom.by_id("email")[1]["placeholder"], "Email or username")
        self.assertNotIn("signupEmailHint", dom.by_id("signupEmail")[1].get("aria-describedby", "").split())
        self.assertNotIn("Use any valid email you can access, including a personal email.", html)
        self.assertIn("hidden", dom.by_id("resetFlowForm")[1])
        self.assertIsNone(dom.by_id("resetCodeForm"))
        self.assertIsNone(dom.by_id("verifyResetCodeButton"))
        self.assertEqual(dom.by_id("resetVerificationCode")[1]["name"], "code")
        self.assertEqual(dom.by_id("resetVerificationCode")[1]["pattern"], "[0-9]{6}")
        self.assertEqual(dom.by_id("resetFlowForm")[1]["action"], "/reset-password")

    def test_password_recovery_uses_one_form_with_send_otp(self):
        html, dom = self.page("/password-reset")
        self.assertIsNone(dom.by_id("forgotPasswordForm"))
        self.assertIsNone(dom.by_id("resendCodeLink"))
        self.assertNotIn("hidden", dom.by_id("resetFlowForm")[1])
        self.assertEqual(dom.by_id("sendCodeButton")[1]["type"], "button")
        self.assertEqual(dom.by_id("resetButton")[1]["type"], "submit")
        self.assertIn("disabled", dom.by_id("resetButton")[1])
        for field in ("resetEmail", "resetVerificationCode", "newPassword", "confirmPassword"):
            self.assertNotIn("disabled", dom.by_id(field)[1])
        self.assertIn(">Send OTP</button>", html)

    def test_agreement_is_required_only_for_account_creation(self):
        for path in ("/login", "/signup"):
            with self.subTest(path=path):
                html, dom = self.page(path)
                self.assertIsNone(dom.by_id("loginDataConsent"))
                self.assertIsNone(dom.by_id("openEulaLinkLogin"))
                self.assertNotIn("data_consent_ack", html)
                self.assertNotIn("disabled", dom.by_id("loginButton")[1])
                consent = dom.by_id("signupDataConsent")[1]
                self.assertIn("required", consent)
                self.assertEqual(consent["name"], "data_consent")
                self.assertEqual(consent["value"], "true")
                self.assertEqual(consent["aria-labelledby"], "signupConsentLabel openEulaLink")
                self.assertEqual(dom.by_id("openEulaLink")[1]["aria-controls"], "eulaModal")


if __name__ == "__main__":
    unittest.main()
