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
        for ident in ("analyticsMenuItem", "reportsMenuItem", "escalationsMenuItem",
                      "conflictsMenuItem", "gapsMenuItem", "uploadsMenuItem", "usersMenuItem"):
            tag, attrs = dom.by_id(ident)
            self.assertEqual(tag, "button", ident)
            self.assertEqual(attrs["type"], "button", ident)
        for ident in ("analyticsHourlyChart", "analyticsTopicsChart", "analyticsDays"):
            self.assertIsNotNone(dom.by_id(ident), ident)

    def test_dashboard_omits_answer_feedback_without_removing_staff_replies(self):
        html, dom = self.page("/dashboard")
        for ident in ("feedbackMenuItem", "sidebarFeedbackBadge", "feedbackSection", "fbSourceList",
                      "fbRecentList", "refreshFeedbackBtn"):
            self.assertIsNone(dom.by_id(ident), ident)
            self.assertNotIn(ident, html)
        self.assertNotIn("/admin/feedback/api/list", html)
        self.assertNotIn("/admin/feedback/api/delete", html)
        for ident in ("reportsSection", "escalationsSection", "escList", "uploadsMenuItem", "refreshActivitiesBtn"):
            self.assertIsNotNone(dom.by_id(ident), ident)

    def test_overview_omits_retired_chart_feedback_card_and_document_review(self):
        html, dom = self.page("/dashboard")
        for ident in ("analyticsVolumeChart", "analyticsSeriesNote", "kpiSatisfaction",
                      "kpiSatisfactionDelta", "analyticsDocumentsBody", "kpiAnsweredRate",
                      "kpiAnsweredRateDelta", "analyticsUnansweredBody"):
            self.assertIsNone(dom.by_id(ident), ident)
            self.assertNotIn(ident, html)
        for label in ("Answered vs. unanswered", "Helpful feedback", "Documents to review", "Unanswered topics"):
            self.assertNotIn(label, html)
        self.assertEqual(sum("analytics-kpi" in attrs.get("class", "").split() for _, attrs in dom.elements), 2)
        self.assertEqual(sum("analytics-grid" in attrs.get("class", "").split() for _, attrs in dom.elements), 1)
        for ident in ("kpiConversations", "kpiPending", "analyticsHourlyChart", "analyticsTopicsChart",
                      "refreshActivitiesBtn", "gapsSection"):
            self.assertIsNotNone(dom.by_id(ident), ident)

    def test_dashboard_has_direct_logout_without_an_account_settings_dropdown(self):
        _, dom = self.page("/dashboard")
        for ident in ("userAccountButton", "userDropdown", "settingsButton", "profileButton"):
            self.assertIsNone(dom.by_id(ident), ident)
        tag, logout = dom.by_id("logoutButton")
        self.assertEqual(tag, "a")
        self.assertEqual(logout["aria-label"], "Logout")
        self.assertTrue(logout["href"].endswith("/logout"))
        self.assertIsNotNone(dom.by_id("confirmLogoutButton"))
        self.assertIsNotNone(dom.by_id("cancelLogout"))

    def test_most_asked_topics_identifies_completed_account_and_guest_usage(self):
        html, dom = self.page("/dashboard")
        self.assertIn("Completed account + guest questions", html)
        self.assertIn("Topic counts are temporarily unavailable", html)
        self.assertIn("No topic usage recorded for this date range yet", html)
        self.assertIsNotNone(dom.by_id("analyticsTopicsChart"))
        analytics = app.test_client().get("/admin/analytics/api/overview").get_json()["analytics"]
        self.assertTrue(analytics["topic_usage_available"])
        self.assertEqual(analytics["top_topics"][0], {
            "key": "enrollment", "topic": "Admissions and enrollment", "total": 64,
        })

    def test_chat_uses_report_only_controls_with_explicit_guest_capability(self):
        for path, allowed in (("/", "true"), ("/guest-chat", "false")):
            html, dom = self.page(path)
            sections = [attrs for _, attrs in dom.elements if "chat-section" in attrs.get("class", "").split()]
            self.assertEqual(sections[0]["data-can-report"], allowed)
            self.assertNotIn("feedback-btn", html)
            self.assertNotIn("fa-thumbs-up", html)
            self.assertNotIn("fa-thumbs-down", html)
        script = Path("sc_assistant/static/js/chat.js").read_text(encoding="utf-8")
        self.assertIn("Report this answer", script)
        self.assertIn("fa-flag", script)
        self.assertNotIn("/chat/feedback", script)
        self.assertNotIn("feedbackButtons", script)
        self.assertNotIn("restoreVotes", script)

    def test_chat_suggestions_have_safe_native_fallbacks(self):
        expected = ["How do I enroll?", "What programs does Samar College offer?",
                    "What are the tuition fees and payment options?"]
        for path in ("/", "/guest-chat"):
            html, dom = self.page(path)
            self.assertEqual(dom.by_id("faqSuggestions")[1]["aria-label"], "Suggested questions")
            buttons = [attrs for tag, attrs in dom.elements if "faq-btn" in attrs.get("class", "").split()]
            self.assertEqual(len(buttons), 3)
            self.assertTrue(all(attrs["type"] == "button" for attrs in buttons))
            for question in expected:
                self.assertIn(question, html)

    def test_guest_new_chat_uses_only_the_shared_handler(self):
        html, dom = self.page("/guest-chat")
        self.assertIn("new-chat-btn", dom.by_id("guestNewChatBtn")[1]["class"].split())
        self.assertNotIn("window.guestActiveConv", html)
        self.assertNotIn("$.post('/chat/clear'", html)

    def test_overview_counts_saved_conversations_and_can_refresh_activity(self):
        html, dom = self.page("/dashboard")
        self.assertIn(">Total conversations</p>", html)
        self.assertIn("Saved account chats plus completed guest chats", html)
        self.assertIn("Account + guest chats", html)
        self.assertNotIn("Temporary guest chats are excluded", html)
        self.assertIsNotNone(dom.by_id("kpiConversations"))
        self.assertIsNotNone(dom.by_id("kpiConversationsNote"))
        self.assertIsNone(dom.by_id("kpiQuestions"))
        self.assertIsNone(dom.by_id("kpiQuestionsDelta"))
        tag, attrs = dom.by_id("refreshActivitiesBtn")
        self.assertEqual((tag, attrs["type"]), ("button", "button"))
        self.assertEqual(attrs["aria-label"], "Refresh recent activity")
        client = app.test_client()
        payload = client.get("/admin/analytics/api/overview").get_json()["analytics"]
        self.assertNotEqual(payload["totals"]["conversations"], payload["totals"]["questions_with_signal"])
        activity = client.get("/api/dashboard/activities").get_json()
        self.assertEqual(activity["window_days"], 30)
        self.assertIn("Signed in:", activity["activities"][0]["text"])
        self.assertEqual(activity["activities"][0]["section"], "users")

    def test_chat_keeps_the_registered_account_dropdown(self):
        _, dom = self.page("/")
        for ident in ("userAccountButton", "userDropdown", "settingsButton", "profileButton", "logoutButton"):
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

    def test_dashboard_uses_documents_without_manual_calendar_or_announcements(self):
        html, dom = self.page("/dashboard")
        for ident in ("calendarMenuItem", "announcementsMenuItem", "calendarSection", "announcementsSection",
                      "sidebarCalendarBadge", "sidebarAnnouncementsBadge", "calSaveBtn", "annSaveBtn"):
            self.assertIsNone(dom.by_id(ident), ident)
        for endpoint in ("/admin/calendar/", "/admin/announcements/"):
            self.assertNotIn(endpoint, html)
        self.assertEqual(dom.by_id("fileInput")[1]["type"], "file")
        self.assertIn("multiple", dom.by_id("fileInput")[1])
        self.assertIsNotNone(dom.by_id("uploadSection"))

    def test_chat_does_not_display_or_poll_manual_announcements(self):
        for path in ("/", "/guest-chat"):
            html, dom = self.page(path)
            self.assertIsNone(dom.by_id("announcementBanner"))
            self.assertNotIn("ann-notice", html)
            self.assertIsNotNone(dom.by_id("repliesToggle"))
        script = Path("sc_assistant/static/js/chat.js").read_text(encoding="utf-8")
        self.assertNotIn("/api/announcements", script)
        self.assertNotIn("ANN_POLL_MS", script)

    def test_admin_mobile_tables_keep_scroll_hints(self):
        _, dom = self.page("/dashboard")
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
