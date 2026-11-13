"""Render and exercise the real chat/dashboard UI against ui_preview.py only."""

import argparse
import json
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5055")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    origin = urlsplit(base)
    if origin.hostname not in ("localhost", "127.0.0.1") or origin.scheme != "http":
        raise SystemExit("Only a local UI fixture is permitted.")
    with build_opener(ProxyHandler({})).open(base + "/__ui_preview__/health", timeout=5) as response:
        if json.load(response).get("fixture") != "ui-preview":
            raise SystemExit("UI fixture marker missing; refusing to interact.")

    from playwright.sync_api import expect, sync_playwright

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    errors, audit, retired_requests, clear_requests = [], [], [], []

    def route_request(route):
        url = urlsplit(route.request.url)
        if url.netloc == origin.netloc and url.path == "/chat/clear":
            clear_requests.append(route.request.url)
        if url.netloc == origin.netloc and url.path.startswith((
            "/api/announcements", "/admin/calendar/", "/admin/announcements/",
            "/admin/feedback/api/list", "/admin/feedback/api/delete",
            "/chat/feedback",
        )):
            retired_requests.append(route.request.url)
        if url.hostname == "ui-avatars.com":
            route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" rx="24" fill="#e4ece5"/><text x="24" y="30" text-anchor="middle" fill="#24462c" font-family="sans-serif" font-size="16">JD</text></svg>')
        elif url.netloc == origin.netloc or (route.request.method == "GET" and url.hostname in (
            "fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "code.jquery.com",
        )):
            route.continue_()
        else:
            route.abort()

    def capture(page, name):
        page.evaluate("""async () => {
            await document.fonts.ready;
            await Promise.all(document.getAnimations().filter(a =>
              a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})));
        }""")
        theme_toggle = page.locator("#themeToggle")
        expect(theme_toggle).to_have_css("border-radius", "50%")
        expect(theme_toggle).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        expect(theme_toggle).to_have_css("box-shadow", "none")
        expect(theme_toggle).to_have_css("font-size", "16px")
        for side in ("top", "right", "bottom", "left"):
            expect(theme_toggle).to_have_css(f"border-{side}-width", "0px")
        toggle_box = theme_toggle.bounding_box()
        assert toggle_box and toggle_box["width"] == toggle_box["height"] and toggle_box["width"] >= 44
        expect(page.locator(".chat-section .feedback-btn, .chat-section .fa-thumbs-up, .chat-section .fa-thumbs-down")).to_have_count(0)
        for report in page.locator(".chat-section .report-btn").all():
            expect(report).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            expect(report).to_have_css("box-shadow", "none")
            for side in ("top", "right", "bottom", "left"):
                expect(report).to_have_css(f"border-{side}-width", "0px")
        floating = page.locator(".chat-floating-controls")
        if floating.count():
            expect(page.locator(".chat-section .header, .chat-section .header-left")).to_have_count(0)
            expect(floating).to_have_css("position", "absolute")
            expect(floating).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            expect(floating).to_have_css("border-bottom-width", "0px")
            expect(floating).to_have_css("pointer-events", "none")
            toggle = floating.locator("#themeToggle")
            expect(toggle).to_have_css("pointer-events", "auto")
            box = toggle.bounding_box()
            expect(page.locator("#announcementBanner, .ann-notice")).to_have_count(0)
            content = page.locator("#messagesContainer")
            content_box = content.bounding_box()
            assert box and box["width"] >= 44 and box["height"] >= 44
            assert content_box and box["y"] + box["height"] <= content_box["y"] + 1
        result = page.evaluate("""() => {
          const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
          const welcome = document.querySelector('#emptyChatState');
          let welcomeAlignment = null;
          if (welcome && visible(welcome)) {
            const box=welcome.getBoundingClientRect(), title=welcome.querySelector('h2').getBoundingClientRect();
            const suggestions=welcome.querySelector('.faq-suggestions').getBoundingClientRect();
            welcomeAlignment = {
              headingOffset: Math.abs(title.left+title.width/2-box.left-box.width/2),
              suggestionsOffset: Math.abs(suggestions.left+suggestions.width/2-box.left-box.width/2),
              verticalOffset: Math.abs((title.top+suggestions.bottom)/2-box.top-box.height/2),
              textAlign: getComputedStyle(welcome).textAlign
            };
          }
          return {
            viewport: innerWidth, width: document.documentElement.scrollWidth,
            welcomeAlignment,
            unnamedButtons: [...document.querySelectorAll('button')].filter(visible)
              .filter(el => !(el.textContent.trim() || el.getAttribute('aria-label') || el.title)).map(el => el.id || el.className),
            smallInputs: [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,select')].filter(visible)
              .filter(el => parseFloat(getComputedStyle(el).fontSize)<16).map(el => el.id || el.className),
            typography: [...document.querySelectorAll('.college-brand-name,.message.assistant .message-bubble,.dashboard-header h1')]
              .filter(visible).map(el => ({selector:el.className, font:getComputedStyle(el).fontFamily,
                size:getComputedStyle(el).fontSize,weight:getComputedStyle(el).fontWeight})),
            overflow: [...document.querySelectorAll('body *')].filter(visible).filter(el => {
              const r=el.getBoundingClientRect(); return r.width>innerWidth+1 && !el.closest('.sidebar,.table-wrapper,.table-container,.sc-scroll-x');
            }).slice(0,8).map(el => el.id || el.className || el.tagName)
          };
        }""")
        page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        audit.append({"view": name, **result})
        if not args.diagnostic:
            assert result["width"] <= result["viewport"], result
            assert not result["unnamedButtons"], result
            if result["viewport"] <= 820:
                assert not result["smallInputs"], result
            if result["welcomeAlignment"]:
                alignment = result["welcomeAlignment"]
                assert alignment["headingOffset"] < 2 and alignment["suggestionsOffset"] < 2, result
                assert alignment["verticalOffset"] < 3 and alignment["textAlign"] == "center", result

    def exercise_suggestions(page, width, kind):
        cards = page.locator("#faqSuggestions .faq-btn")
        expected = ["How do I enroll?", "What are the tuition fees and payment options?",
                    "What scholarships are available?"]
        expect(cards).to_have_text(expected)
        page.evaluate("""() => {
            window.suggestionWaiters = [];
            window.originalSuggestionGetJSON = jQuery.getJSON;
            jQuery.getJSON = function(url, ...args) {
                if (url !== '/chat/api/suggestions') {
                    return window.originalSuggestionGetJSON.call(this, url, ...args);
                }
                const pending = jQuery.Deferred();
                window.suggestionWaiters.push(pending);
                return pending.promise();
            };
        }""")

        def new_pending():
            index = page.evaluate("() => window.suggestionWaiters.length")
            if width <= 768 and "active" not in page.locator("#sidebar").get_attribute("class").split():
                page.locator("#hamburgerMenu").click()
            page.locator(".new-chat-btn").click()
            page.wait_for_function("n => window.suggestionWaiters.length === n", arg=index + 1)
            if width <= 768 and "active" in page.locator("#sidebar").get_attribute("class").split():
                page.keyboard.press("Escape")
            expect(page.locator("#emptyChatState")).to_be_visible()
            return index

        def resolve(index, questions):
            page.evaluate("([i, questions]) => { window.suggestionWaiters[i].resolve({success: true, suggestions: questions}); }",
                          [index, questions])

        try:
            rank_a = ["What scholarships are available?", "Who are the college deans and school officials?",
                      "What are the important dates in the academic calendar?"]
            rank_b = ["What programs does Samar College offer?", "How do I reset my account password?",
                      "How do I request my school records?"]
            pending = new_pending()
            resolve(pending, rank_a)
            expect(cards).to_have_text(rank_a)

            pending = new_pending()
            page.locator("#messageInput").fill("Keep this unsent draft")
            resolve(pending, rank_b)
            expect(page.locator("#messageInput")).to_have_value("Keep this unsent draft")
            expect(cards).to_have_text(rank_a)
            page.locator("#messageInput").fill("")

            pending = new_pending()
            cards.first.focus()
            resolve(pending, rank_b)
            expect(cards.first).to_be_focused()
            expect(cards).to_have_text(rank_a)

            older = new_pending()
            newer = new_pending()
            resolve(newer, rank_b)
            expect(cards).to_have_text(rank_b)
            resolve(older, rank_a)
            expect(cards).to_have_text(rank_b)

            pending = new_pending()
            page.evaluate("i => { window.suggestionWaiters[i].reject(); }", pending)
            expect(cards).to_have_text(rank_b)
            pending = new_pending()
            resolve(pending, ["Duplicate", "Duplicate", "Only two"])
            expect(cards).to_have_text(rank_b)
            capture(page, f"popular-suggestions-{kind}-{width}")

            pending = new_pending()
            with page.expect_request(base + "/chat/get") as sent:
                cards.first.press("Enter")
            assert rank_b[0] in sent.value.post_data
            resolve(pending, rank_a)
            expect(page.locator("#emptyChatState")).to_be_hidden()
            expect(cards).to_have_text(rank_b)
            expect(page.locator(".message.user")).to_have_count(1)
            expect(page.locator(".message.user")).to_contain_text(rank_b[0])
            expect(page.locator("#sendButton")).to_be_enabled(timeout=15000)
        finally:
            page.evaluate("() => { jQuery.getJSON = window.originalSuggestionGetJSON; }")
        page.locator("#messageInput").fill("")
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        page.locator(".new-chat-btn").click()
        page.wait_for_load_state("networkidle")
        if width <= 768 and "active" in page.locator("#sidebar").get_attribute("class").split():
            page.keyboard.press("Escape")
        expect(cards).to_have_text(expected)

    def exercise_reporting(page, width):
        message = page.locator(".message.assistant").first
        msg_id = message.get_attribute("id")
        button = message.locator(".report-btn")
        report_url = base + "/chat/report"
        requests = []
        state = {"hold": False, "reject": False}

        def respond(route):
            data = route.request.post_data_json
            requests.append(data)
            if state["hold"]:
                state["route"] = route
                return
            if state["reject"]:
                route.fulfill(json={"success": False, "message": "Preview report rejected."})
            else:
                route.fulfill(json={"status": "ok", "success": True})

        page.route(report_url, respond)
        expect(button).to_have_attribute("aria-label", "Report this answer")
        expect(button).to_have_attribute("aria-expanded", "false")

        for theme in ("light", "dark"):
            if theme == "dark":
                page.locator("#themeToggle").click()
            button.click()
            sheet = message.locator(".reason-sheet")
            expect(button).to_have_attribute("aria-expanded", "true")
            expect(button).to_have_attribute("aria-controls", sheet.get_attribute("id"))
            expect(sheet.locator(".reason-chip")).to_have_count(6)
            expect(sheet.locator(".escalate-offer")).to_be_visible()
            capture(page, f"report-options-{width}-{theme}")
            button.click()
            expect(button).to_have_attribute("aria-expanded", "false")
            expect(message.locator(".reason-sheet, .escalate-offer")).to_have_count(0)
            button.press("Enter")
            sheet.locator(".reason-dismiss").click()
            expect(button).to_be_focused()
            button.press("Enter")
            sheet.locator(".reason-other").click()
            sheet.locator(".reason-other-text").fill("Discard this draft")
            page.keyboard.press("Escape")
            expect(sheet).to_have_count(0)
            expect(button).to_be_focused()
            assert not requests, "Opening and dismissing report options must not submit anything"

        button.click()
        sheet = message.locator(".reason-sheet")
        sheet.locator(".reason-other").click()
        details = sheet.locator(".reason-other-text")
        details.fill("This answer cites an outdated deadline.")
        state["hold"] = True
        sheet.locator(".reason-other-send").click()
        expect(button).to_be_disabled()
        expect(details).to_be_disabled()
        sheet.locator(".reason-other-send").evaluate("el => window.jQuery(el).trigger('click')")
        button.evaluate("el => window.jQuery(el).trigger('click')")
        assert len(requests) == 1
        state["hold"] = False
        state.pop("route").fulfill(status=500, json={"message": "Preview report save failed."})
        expect(button).to_be_enabled()
        expect(details).to_have_value("This answer cites an outdated deadline.")
        expect(page.locator("#notification .notification-message")).to_have_text("Preview report save failed.")
        capture(page, f"report-retry-{width}")
        state["reject"] = True
        sheet.locator(".reason-other-send").click()
        expect(button).to_be_enabled()
        expect(sheet.locator(".reason-thanks")).to_have_count(0)
        expect(page.locator("#notification .notification-message")).to_have_text("Preview report rejected.")
        state["reject"] = False
        page.evaluate("""() => {
            window.reportTimers = [];
            window.originalReportTimer = window.setTimeout;
            window.setTimeout = function(callback, delay, ...args) {
                if (delay === 2600) { window.reportTimers.push(() => callback(...args)); return 0; }
                return window.originalReportTimer.call(window, callback, delay, ...args);
            };
        }""")
        try:
            sheet.locator(".reason-other-send").click()
            expect(sheet.locator(".reason-thanks")).to_contain_text("Report sent to the administrator.")
            expect(button).to_be_enabled()
            assert requests[-1]["msg_id"] == msg_id and requests[-1]["reason"] == "Others"
            assert requests[-1]["other_text"] == "This answer cites an outdated deadline."
            assert requests[-1]["question"] and requests[-1]["sources"] and "verdict" not in requests[-1]
            button.click()
            button.click()
            sheet.locator(".reason-other").click()
            sheet.locator(".reason-other-text").fill("A new unsent explanation")
            page.evaluate("() => window.reportTimers.splice(0).forEach(finish => finish())")
            expect(sheet.locator(".reason-other-text")).to_have_value("A new unsent explanation")
            sheet.locator(".reason-dismiss").click()
            button.click()
            sheet.locator('.reason-chip[data-reason="Wrong information"]').click()
            expect(sheet.locator(".reason-thanks")).to_be_visible()
            assert requests[-1]["reason"] == "Wrong information"
            page.evaluate("() => window.reportTimers.splice(0).forEach(finish => finish())")
            expect(sheet).to_have_count(0)
            expect(button).to_have_attribute("aria-expanded", "false")
        finally:
            page.evaluate("() => { window.setTimeout = window.originalReportTimer; }")
        page.unroute(report_url, respond)
        page.locator("#themeToggle").click()
        page.reload(wait_until="networkidle")
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        page.locator("#conversationList .conversation-item").first.click()
        expect(page.locator(".message.assistant")).to_have_count(2)
        expect(page.locator(".message.assistant .report-btn")).to_have_count(2)
        expect(page.locator(".message.assistant").first.locator(".report-btn")).to_have_attribute("aria-expanded", "false")
        if width <= 768:
            page.keyboard.press("Escape")
        capture(page, f"report-after-reload-{width}")

    def exercise_guest_reporting(page, width):
        submitted = []
        def note_report(request):
            if request.url == base + "/chat/report":
                submitted.append(request)
        page.on("request", note_report)
        page.locator("#messageInput").fill("How do I enroll?")
        page.locator("#sendButton").click()
        message = page.locator(".message.assistant").last
        button = message.locator(".report-btn")
        expect(button).to_be_enabled(timeout=15000)
        expect(button).to_have_attribute("data-msg-id", "m9")
        for theme in ("light", "dark"):
            if theme == "dark":
                page.locator("#themeToggle").click()
            button.click()
            sheet = message.locator(".reason-sheet")
            expect(sheet.locator(".reason-chip")).to_have_count(0)
            expect(sheet.locator(".report-sign-in")).to_have_text("Sign in")
            expect(sheet.locator(".report-sign-in")).to_have_attribute(
                "href", page.locator("#guestSignInButton").get_attribute("href")
            )
            expect(sheet).to_contain_text("Sign in to submit reports")
            capture(page, f"report-guest-{width}-{theme}")
            if theme == "dark":
                sheet.locator(".escalate-open-btn").click()
                expect(page.locator("#escalateModal")).to_be_visible()
                page.locator("#escalateContact").fill("guest@example.invalid")
                page.locator("#escalateRoute").select_option("admin")
                with page.expect_request(base + "/chat/escalate") as request:
                    page.locator("#escalateSubmitBtn").click()
                assert request.value.post_data_json["contact"] == "guest@example.invalid"
                expect(page.locator("#escalateModal")).to_be_hidden()
            button.click()
            expect(sheet).to_have_count(0)
        assert not submitted, "Guests must not file reports through the protected report API"
        page.remove_listener("request", note_report)
        page.locator("#themeToggle").click()

    def exercise_poll_rate_limit(page):
        upload_url = base + "/upload"
        poll_url = base + "/upload/status/rate-limit-preview"
        polls = []

        def status_response(route):
            polls.append(route.request)
            if len(polls) == 1:
                route.fulfill(status=429, headers={"Retry-After": "1"},
                              json={"success": False, "retry_after": 1, "message": "Wait before polling again."})
            else:
                route.fulfill(json={"success": True, "job": {"percent": 100, "status": "done", "done": True,
                    "files": [{"filename": "rate-limit-preview.pdf", "status": "done", "percent": 100,
                               "label": "Done", "chunks": 1}]}})

        page.route(upload_url, lambda route: route.fulfill(status=202,
                   json={"success": True, "job_id": "rate-limit-preview"}))
        page.route(poll_url, status_response)
        page.locator("#fileInput").set_input_files({"name": "rate-limit-preview.pdf", "mimeType": "application/pdf",
                                                   "buffer": b"%PDF-1.4\nPreview fixture only.\n%%EOF"})
        expect(page.locator("#uploadProgress h4")).to_contain_text("Checking indexing status again")
        expect(page.locator("#uploadProgress h4")).to_have_text("Done", timeout=5000)
        assert len(polls) == 2
        page.unroute(upload_url)
        page.unroute(poll_url)

    def exercise_admin_layouts(page, width, theme):
        def open_section(menu, section):
            if width <= 768 and "active" not in (page.locator("#sidebar").get_attribute("class") or ""):
                page.locator("#hamburgerMenu").click()
            page.locator("#" + menu + "MenuItem").click()
            expect(page.locator("#" + section + "Section")).to_be_visible()
            if width <= 768:
                page.keyboard.press("Escape")
                page.wait_for_function("[...document.querySelectorAll('.dashboard-content,.page-content')].every(el => el.scrollTop < 2)")

        def assert_controls_fit():
            clipped = page.evaluate("""() => {
                const section = document.querySelector('.dashboard-section.active');
                return [...section.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea,button')]
                    .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
                    .filter(el => !el.closest('.sc-scroll-x,.table-wrapper,.table-container'))
                    .filter(el => { const r = el.getBoundingClientRect(); return r.left < -1 || r.right > innerWidth + 1; })
                    .map(el => el.id || el.className);
            }""")
            assert not clipped, clipped

        expect(page.locator(
            "#calendarMenuItem, #announcementsMenuItem, #calendarSection, #announcementsSection, "
            "#sidebarCalendarBadge, #sidebarAnnouncementsBadge, "
            "#feedbackMenuItem, #feedbackSection, #sidebarFeedbackBadge"
        )).to_have_count(0)
        expect(page.locator('.sidebar .menu-item[id$="MenuItem"]')).to_have_count(7)
        expect(page.locator('[data-target="#feedbackMenuItem"]')).to_have_count(0)

        # Persisted navigation from the removed screens must not restore a blank page.
        for saved in ("calendarMenuItem", "announcementsMenuItem", "feedbackMenuItem"):
            page.evaluate("saved => localStorage.setItem('dashboardLastSection', saved)", saved)
            page.reload(wait_until="networkidle")
            expect(page.locator("#analyticsSection")).to_be_visible()
            expect(page.locator(".dashboard-section.active")).to_have_count(1)
            expect(page.locator(".header-left span")).to_have_text("Overview")
            expect(page.locator("#kpiConversations")).to_have_text("128")
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        capture(page, f"dashboard-navigation-{width}-{theme}")
        if width <= 768:
            page.keyboard.press("Escape")
        for section in ("analytics", "escalations", "gaps", "conflicts"):
            open_section(section, section)
            expect(page.locator(".dashboard-section.active")).to_have_count(1)
            assert_controls_fit()

        open_section("reports", "reports")
        expect(page.locator("#reportsTbody .r-view-btn")).to_have_count(2)
        view_report = page.locator('.r-view-btn[data-id="mobile-report-1"]')
        if width <= 768:
            expect(page.locator("#reportsTable .r-done-btn").first).to_be_hidden()
            expect(view_report).to_be_in_viewport()
            scroller = page.locator("#reportsTable").locator("..").bounding_box()
            button = view_report.bounding_box()
            assert button and scroller and button["x"] + button["width"] <= scroller["x"] + scroller["width"] + 1
        else:
            expect(page.locator("#reportsTable .r-done-btn").first).to_be_visible()
        capture(page, f"reports-mobile-table-{width}-{theme}")
        view_report.click()
        dialog = page.get_by_role("dialog", name="Report Detail", exact=True)
        expect(page.locator("body > #reportDetailModal")).to_have_count(1)
        expect(dialog).to_be_visible()
        expect(page.locator("#rd-full-conversation .markdown-body table").first).to_be_visible()
        expect(page.locator("#closeReportDetailBtn")).to_be_focused()
        page.locator("#rdDoneBtn").scroll_into_view_if_needed()
        expect(page.locator("#rdDoneBtn")).to_be_in_viewport(ratio=1)
        box = page.locator("#reportDetailBox").bounding_box()
        assert box and box["y"] >= 15 and box["y"] + box["height"] <= page.viewport_size["height"] - 15
        assert box["x"] >= 15 and box["x"] + box["width"] <= width - 15
        if width <= 768:
            assert page.locator("#rd-details-grid").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(/\\s+/).length") == 1
        capture(page, f"report-mobile-detail-{width}-{theme}")
        page.locator("#toggleFullScreenBtn").click()
        expect(page.locator("#toggleFullScreenBtn")).to_have_attribute("aria-pressed", "true")
        if width <= 768:
            expect(page.locator("#closeReportDetailBtn")).to_be_visible()
        capture(page, f"report-mobile-fullscreen-{width}-{theme}")
        box = page.locator("#reportDetailBox").bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= page.viewport_size["height"] + 1
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(page.locator("#toggleFullScreenBtn")).to_have_attribute("aria-pressed", "false")
        expect(view_report).to_be_focused()
        view_report.click()
        page.locator("#usersMenuItem").evaluate("el => window.jQuery(el).trigger('click')")
        expect(dialog).to_be_hidden()
        expect(page.locator("#usersSection")).to_be_visible()
        expect(page.locator("#userListBody .edit-user")).to_have_count(2)
        if width <= 768:
            expect(page.locator("#userTableHint")).to_be_visible()
            expect(page.locator("#userListBody .edit-user").first).to_be_in_viewport()
        capture(page, f"users-mobile-actions-{width}-{theme}")

        open_section("uploads", "upload")
        page.reload(wait_until="networkidle")
        expect(page.locator("#uploadSection")).to_be_visible()
        assert page.evaluate("localStorage.getItem('dashboardLastSection')") == "uploadsMenuItem"
        expect(page.locator("#fileList .file-item")).to_have_count(3)
        assert_controls_fit()
        capture(page, f"documents-mobile-filenames-{width}-{theme}")

    def exercise_overview(page, width, theme):
        expect(page.locator("#kpiQuestions, #kpiQuestionsDelta")).to_have_count(0)
        expect(page.locator(
            "#analyticsVolumeChart, #analyticsSeriesNote, #kpiSatisfaction, "
            "#kpiSatisfactionDelta, #analyticsDocumentsBody, #kpiAnsweredRate, "
            "#kpiAnsweredRateDelta, #analyticsUnansweredBody"
        )).to_have_count(0)
        expect(page.locator("#analyticsSection .analytics-kpi")).to_have_count(2)
        expect(page.locator("#analyticsSection table")).to_have_count(0)
        expect(page.locator("#analyticsSection canvas")).to_have_count(2)
        grid = page.locator("#analyticsSection .analytics-kpis").evaluate("""el => ({
            columns: getComputedStyle(el).gridTemplateColumns.split(/\\s+/).length,
            width: el.clientWidth,
            lastWidth: el.lastElementChild.getBoundingClientRect().width
        })""")
        assert grid["columns"] == 2, grid
        assert abs(grid["width"] - 2 * grid["lastWidth"]) < 2, grid
        expect(page.locator(".analytics-kpi-label").first).to_have_text("Total conversations")
        expect(page.locator("#kpiConversations")).to_have_text("128")
        expect(page.locator("#kpiConversationsNote")).to_have_text("Account + guest chats")
        page.locator("#analyticsDays").select_option("90")
        expect(page.locator("#analyticsGeneratedAt")).to_contain_text("Last 90 days")
        expect(page.locator("#kpiConversations")).to_have_text("128")
        page.locator("#analyticsDays").select_option("7")
        expect(page.locator("#analyticsRefreshBtn")).to_be_enabled()

        count_url = base + "/admin/analytics/api/overview*"
        for count in (0, None):
            def count_response(route, _request, value=count):
                payload = route.fetch().json()
                payload["analytics"]["totals"]["conversations"] = value
                route.fulfill(json=payload)
            page.route(count_url, count_response)
            page.locator("#analyticsRefreshBtn").click()
            expect(page.locator("#kpiConversations")).to_have_text("0" if count == 0 else "\u2014")
            expect(page.locator("#kpiConversationsNote")).to_have_text(
                "Account + guest chats" if count == 0 else "Count unavailable"
            )
            expect(page.locator("#kpiPending")).to_have_text("3")
            expect(page.locator("#analyticsRefreshBtn")).to_be_enabled()
            page.unroute(count_url, count_response)
        capture(page, f"overview-count-unavailable-{width}-{theme}")
        page.locator("#analyticsRefreshBtn").click()
        expect(page.locator("#kpiConversations")).to_have_text("128")
        expect(page.locator("#refreshActivitiesBtn")).to_be_enabled()

        topic_case_rows = [{"key": "programs", "topic": "Programs and courses", "total": 1}]
        for state, available, topic_rows in (
            ("single-question", True, topic_case_rows),
            ("empty", True, []),
            ("unavailable", False, topic_case_rows),
        ):
            def topic_response(route, _request, ready=available, rows=topic_rows):
                payload = route.fetch().json()
                payload["analytics"]["top_topics"] = rows
                payload["analytics"]["topic_usage_available"] = ready
                route.fulfill(json=payload)
            page.route(count_url, topic_response)
            page.locator("#analyticsRefreshBtn").click()
            expect(page.locator("#analyticsRefreshBtn")).to_be_enabled()
            if state == "single-question":
                page.wait_for_function("""() => window.Chart?.getChart('analyticsTopicsChart')
                    ?.data.labels[0] === 'Programs and courses'""")
                chart = page.evaluate("""() => {
                    const chart = Chart.getChart('analyticsTopicsChart');
                    return {labels: chart.data.labels, values: chart.data.datasets[0].data,
                            label: chart.data.datasets[0].label};
                }""")
                assert chart == {"labels": ["Programs and courses"], "values": [1], "label": "Completed questions"}
            else:
                message = ("No topic usage recorded for this date range yet." if available else
                           "Topic counts are temporarily unavailable. Try refreshing.")
                expect(page.locator("#analyticsTopicsChart").locator("..")).to_contain_text(message)
                assert page.evaluate("() => !Chart.getChart('analyticsTopicsChart')")
            assert page.evaluate("() => Boolean(Chart.getChart('analyticsHourlyChart'))")
            expect(page.locator("#kpiConversations")).to_have_text("128")
            expect(page.locator("#kpiPending")).to_have_text("3")
            capture(page, f"overview-topics-{state}-{width}-{theme}")
            page.unroute(count_url, topic_response)
        page.locator("#analyticsRefreshBtn").click()
        expect(page.locator("#analyticsRefreshBtn")).to_be_enabled()
        page.wait_for_function("""() => window.Chart?.getChart('analyticsTopicsChart')
            ?.data.labels[0] === 'Admissions and enrollment'""")

        activity_url = base + "/api/dashboard/activities*"
        name = 'Student <img src=x onerror=alert(1)> & Dean'
        email = "previewstudentwithaverylongenrollmentandscholarshipinquiry@students.example.invalid"
        def fresh_signin(route):
            route.fulfill(json={"success": True, "window_days": 30, "activities": [{
                "text": f"Signed in: <strong>{escape(name)}</strong> ({escape(email)})",
                "time": "Just now", "at": "2026-11-13T05:01:00Z",
                "icon": "fa-right-to-bracket", "tone": "info", "section": "users",
            }]})
        page.route(activity_url, fresh_signin)
        page.locator("#refreshActivitiesBtn").click()
        row = page.locator(".activity-item").first
        expect(row.locator("strong")).to_have_text(name)
        expect(row).to_contain_text(email)
        expect(row.locator(".activity-time")).to_have_text("Just now")
        expect(row.locator("img, script, svg")).to_have_count(0)
        expect(row).to_have_attribute("role", "button")
        expect(page.locator("#refreshActivitiesBtn")).to_be_enabled()
        row.focus()
        capture(page, f"overview-signin-activity-{width}-{theme}")
        page.keyboard.press("Enter")
        expect(page.locator("#usersSection")).to_be_visible()
        page.unroute(activity_url, fresh_signin)
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        page.locator("#analyticsMenuItem").click()
        expect(page.locator("#analyticsSection")).to_be_visible()
        expect(page.locator(".activity-list")).to_contain_text("Maria Santos")

        def failed_activity(route):
            route.fulfill(status=503, json={"success": False})
        page.route(activity_url, failed_activity)
        page.locator("#refreshActivitiesBtn").click()
        expect(page.locator(".activity-list")).to_contain_text("Could not load activity.")
        expect(page.locator("#refreshActivitiesBtn")).to_be_enabled()
        page.unroute(activity_url, failed_activity)
        page.locator("#refreshActivitiesBtn").click()
        expect(page.locator(".activity-list")).to_contain_text("Maria Santos")
        expect(page.locator("#refreshActivitiesBtn")).to_be_enabled()
        page.locator(".page-content, .dashboard-content").evaluate_all("els => els.forEach(el => el.scrollTop = 0)")

    def exercise_user_editor(page, width, theme):
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        page.locator("#usersMenuItem").click()
        expect(page.locator("#usersSection")).to_be_visible()
        if width <= 768:
            page.keyboard.press("Escape")
        edit_button = page.locator("#userListBody .edit-user").first
        expect(page.locator("#userListBody .edit-user")).to_have_count(2)
        search = page.get_by_role("searchbox", name="Search users", exact=True)
        expect(search).to_be_enabled()
        expect(page.locator("#userSearchStatus")).to_have_text("2 users")
        search_requests = []

        def record_user_load(request):
            if request.url == base + "/api/dashboard/users":
                search_requests.append(request)

        page.on("request", record_user_load)
        for query, expected_id in (("  JUAN  ", "preview-juan"), ("MARIA@SAMARCOLLEGE", "preview-maria"),
                                   ("PREVIEW-JUAN", "preview-juan"), ("maria   preview-maria", "preview-maria")):
            search.fill(query)
            expect(page.locator("#userListBody .edit-user")).to_have_count(1)
            expect(edit_button).to_have_attribute("data-id", expected_id)
            expect(page.locator("#userSearchStatus")).to_have_text("1 of 2 users")
        capture(page, f"user-search-{width}-{theme}")
        edit_button.click()
        expect(page.locator("#editUserCognitoUsername")).to_have_value("preview-maria")
        page.locator("#cancelEditUser").click()
        search.fill("<no matching user>")
        expect(page.locator("#userListBody .edit-user")).to_have_count(0)
        expect(page.locator("#userListBody")).to_have_text("No users match this search.")
        expect(page.locator("#userSearchStatus")).to_have_text("0 of 2 users")
        if width <= 768:
            expect(page.locator("#userListTable thead")).to_be_hidden()
            expect(page.locator("#userListBody td")).to_be_in_viewport(ratio=1)
        capture(page, f"user-search-empty-{width}-{theme}")
        search.fill("")
        expect(page.locator("#userListBody .edit-user")).to_have_count(2)
        assert not search_requests
        page.remove_listener("request", record_user_load)
        if width == 1280 and theme == "light":
            users_url = base + "/api/dashboard/users"
            literal_name = 'A "Quoted" <account>'
            page.route(users_url, lambda route: route.fulfill(json={"success": True, "users": [{
                "id": "null", "username": literal_name, "email": "literal@example.com", "role": "user",
                "status": "CONFIRMED", "joined": "2026-11-01T08:00:00Z",
            }]}))
            page.locator("#usersMenuItem").click()
            expect(search).to_be_enabled()
            search.fill("<account>")
            expect(page.locator("#userListBody td").first).to_have_text(literal_name)
            expect(page.locator("#userListBody account")).to_have_count(0)
            edit_button.click()
            expect(page.locator("#editUserCognitoUsername")).to_have_value("null")
            expect(page.locator("#editUserUsername")).to_have_value(literal_name)
            page.locator("#cancelEditUser").click()
            search.fill("")
            page.unroute(users_url)
            page.route(users_url, lambda route: route.fulfill(status=503, json={"success": False}))
            page.locator("#usersMenuItem").click()
            expect(page.locator("#userListBody")).to_have_text("Failed to load users.")
            expect(search).to_be_disabled()
            page.unroute(users_url)
            page.locator("#usersMenuItem").click()
            expect(search).to_be_enabled()
            expect(page.locator("#userListBody .edit-user")).to_have_count(2)
        modal = page.get_by_role("dialog", name="Edit User", exact=True)
        expect(page.locator("body > #editUserModal")).to_have_count(1)
        expect(modal).to_be_hidden()
        edit_button.click()
        expect(modal).to_be_visible()
        expect(page.locator("#editUserUsername")).to_be_focused()
        expect(page.locator("#editUserUsername")).to_have_value("Juan Dela Cruz")
        expect(page.locator("#editUserCognitoUsername")).to_have_value("preview-juan")
        expect(page.locator("#editUserEmail")).to_be_disabled()
        expect(page.locator("#editUserRole")).to_have_value("admin")
        capture(page, f"user-editor-{width}-{theme}")
        bounds = modal.bounding_box()
        box = page.locator("#editUserBox").bounding_box()
        viewport = page.viewport_size
        assert bounds and bounds["x"] == 0 and bounds["y"] == 0
        assert bounds["width"] == viewport["width"] and bounds["height"] == viewport["height"]
        assert box and box["x"] >= 15 and box["x"] + box["width"] <= width - 15
        assert box["y"] >= 15 and box["y"] + box["height"] <= viewport["height"] - 15
        assert abs(box["x"] + box["width"] / 2 - width / 2) < 1
        assert page.evaluate("document.elementFromPoint(8, 8).id") == "editUserModal"

        page.locator("#saveUserChanges").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#closeEditUserModal")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#saveUserChanges")).to_be_focused()
        page.locator("#editUserUsername").fill("Unsaved preview name")
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
        expect(edit_button).to_be_focused()
        expect(page.locator("#editUserUsername")).to_have_value("")
        edit_button.click()
        expect(page.locator("#editUserUsername")).to_have_value("Juan Dela Cruz")
        page.locator("#cancelEditUser").click()
        expect(modal).to_be_hidden()
        edit_button.click()
        page.locator("#closeEditUserModal").click()
        expect(modal).to_be_hidden()
        edit_button.click()
        page.mouse.click(8, 8)
        expect(modal).to_be_hidden()

        update_url = base + "/api/dashboard/users/update"
        search.fill("preview-juan")
        page.route(update_url, lambda route: route.fulfill(
            status=400, json={"success": False, "message": "Preview update rejected."}))
        edit_button.click()
        page.locator("#editUserUsername").fill("Edited preview user")
        with page.expect_response(update_url):
            page.locator("#saveUserChanges").click()
        expect(modal).to_be_visible()
        expect(page.locator("#saveUserChanges")).to_be_enabled()
        expect(page.locator("#editUserUsername")).to_have_value("Edited preview user")
        expect(page.locator("#notification .notification-message")).to_have_text("Preview update rejected.")
        capture(page, f"user-editor-error-{width}-{theme}")
        assert page.locator("#notification").evaluate("""el => {
            const box = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
        }""")
        page.unroute(update_url)
        with page.expect_request(update_url) as saved:
            page.locator("#saveUserChanges").click()
        expect(modal).to_be_hidden()
        payload = parse_qs(saved.value.post_data)
        assert payload == {"cognito_username": ["preview-juan"], "username": ["Edited preview user"], "role": ["admin"]}
        expect(search).to_be_enabled()
        expect(search).to_have_value("preview-juan")
        expect(page.locator("#userListBody .edit-user")).to_have_count(1)
        search.fill("")
        expect(page.locator("#userListBody .edit-user")).to_have_count(2)

        edit_button.click()
        page.locator("#editUserUsername").fill("Discard this draft on navigation")
        target, section = (("#uploadsMenuItem", "#uploadSection") if theme == "dark"
                           else ("#analyticsMenuItem", "#analyticsSection"))
        # Section restoration also dispatches menu clicks programmatically.
        page.locator(target).evaluate("el => window.jQuery(el).trigger('click')")
        expect(page.locator(section)).to_be_visible()
        expect(modal).to_be_hidden()
        expect(page.locator("#editUserUsername")).to_have_value("")
        expect(page.locator("#editUserCognitoUsername")).to_have_value("")
        capture(page, f"after-user-navigation-{width}-{theme}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width in (1280, 820, 390, 320):
                context = browser.new_context(viewport={"width": width, "height": 900 if width > 820 else 812},
                                              is_mobile=width <= 768, has_touch=width <= 768)
                context.route("**/*", route_request)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/", wait_until="networkidle")
                expect(page.locator(".header, .header-left")).to_have_count(0)
                expect(page.locator(".chat-floating-controls")).to_have_count(1)
                expect(page.locator("#themeToggle")).to_be_visible()
                expect(page.locator("#emptyChatState")).to_be_visible()
                capture(page, f"chat-empty-{width}-light")
                page.locator("#themeToggle").click()
                expect(page.locator("body")).to_have_attribute("data-theme", "dark")
                capture(page, f"chat-empty-{width}-dark")
                exercise_suggestions(page, width, "account")
                if width <= 768:
                    page.locator("#hamburgerMenu").click()
                    expect(page.locator("#sidebar")).to_have_class("sidebar active")
                    capture(page, f"drawer-{width}")
                page.locator("#conversationList .conversation-item").first.click()
                expect(page.locator(".message.assistant")).to_have_count(2)
                if width <= 768:
                    page.keyboard.press("Escape")
                capture(page, f"chat-history-{width}-dark")
                page.locator("#themeToggle").click()
                expect(page.locator("body")).not_to_have_attribute("data-theme", "dark")
                capture(page, f"chat-history-{width}-light")
                page.locator("#messageInput").fill("What are the requirements for transferees?")
                page.locator("#sendButton").click()
                expect(page.locator(".message.assistant")).to_have_count(3)
                expect(page.locator(".message.assistant").last).to_contain_text("Enrollment for transferees", timeout=15000)
                expect(page.locator("#sendButton")).to_be_enabled()
                capture(page, f"chat-response-{width}")
                exercise_reporting(page, width)
                if width > 768:
                    page.locator("#sidebarCollapseBtn").click()
                    expect(page.locator("#sidebar")).to_have_class("sidebar collapsed")
                    capture(page, f"chat-collapsed-{width}")
                    page.locator("#sidebarCollapseBtn").click()
                page.goto(base + "/dashboard", wait_until="networkidle")
                expect(page.locator("#kpiConversations")).to_have_text("128")
                expect(page.locator("#attentionList .attention-row")).to_have_count(2)
                page.locator("#analyticsDays").select_option("7")
                expect(page.locator("#analyticsGeneratedAt")).to_contain_text("Last 7 days")
                exercise_overview(page, width, "light")
                capture(page, f"dashboard-{width}-light")
                page.locator("#themeToggle").click()
                exercise_overview(page, width, "dark")
                capture(page, f"dashboard-{width}-dark")
                if width <= 768:
                    page.locator("#hamburgerMenu").click()
                page.locator("#uploadsMenuItem").focus()
                page.keyboard.press("Enter")
                expect(page.locator("#uploadSection")).to_be_visible()
                if width <= 768:
                    page.keyboard.press("Escape")
                capture(page, f"documents-{width}")
                if width == 1280:
                    exercise_poll_rate_limit(page)
                exercise_user_editor(page, width, "dark")
                page.locator("#themeToggle").click()
                exercise_user_editor(page, width, "light")
                exercise_admin_layouts(page, width, "light")
                page.locator("#themeToggle").click()
                exercise_admin_layouts(page, width, "dark")
                page.goto(base + "/settings", wait_until="networkidle")
                for theme in ("light", "dark"):
                    if (page.locator("body").get_attribute("data-theme") or "light") != theme:
                        page.locator("#themeToggle").click()
                    page.mouse.move(0, 0)
                    capture(page, f"settings-{width}-{theme}")
                    toggle = page.locator("#themeToggle")
                    toggle.hover()
                    expect(toggle).to_have_css("border-top-width", "0px")
                    expect(toggle).to_have_css("border-radius", "50%")
                    expect(toggle).to_have_css("background-color", "rgba(0, 0, 0, 0)")
                    page.mouse.down()
                    expect(toggle).to_have_css("background-color", "rgba(0, 0, 0, 0)")
                    page.mouse.move(0, 0)
                    page.mouse.up()
                    toggle.focus()
                    page.keyboard.press("Tab")
                    page.keyboard.press("Shift+Tab")
                    expect(toggle).to_be_focused()
                    assert toggle.evaluate("el => parseFloat(getComputedStyle(el).outlineWidth)") >= 2
                    page.keyboard.press("Space")
                    assert (page.locator("body").get_attribute("data-theme") or "light") != theme
                    page.keyboard.press("Space")
                    assert (page.locator("body").get_attribute("data-theme") or "light") == theme
                context.close()

                guest = browser.new_context(viewport={"width": width, "height": 900 if width > 820 else 812})
                guest.route("**/*", route_request)
                page = guest.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/guest-chat", wait_until="networkidle")
                exercise_suggestions(page, width, "guest")
                exercise_guest_reporting(page, width)
                if width <= 768:
                    page.locator("#hamburgerMenu").click()
                previous_clears = len(clear_requests)
                page.locator("#guestNewChatBtn").click()
                page.wait_for_load_state("networkidle")
                assert len(clear_requests) == previous_clears + 1, "Guest New chat must send exactly one clear request"
                expect(page.locator("#emptyChatState")).to_be_visible()
                if width <= 768 and "active" in page.locator("#sidebar").get_attribute("class").split():
                    page.keyboard.press("Escape")
                expect(page.locator(".header, .header-left")).to_have_count(0)
                expect(page.locator(".chat-floating-controls")).to_have_count(1)
                expect(page.locator("#emptyChatState")).to_be_visible()
                signin = page.get_by_role("link", name="Sign in to save your conversations", exact=True)
                for theme in ("light", "dark"):
                    if theme == "dark":
                        page.locator(".header-right #themeToggle").click()
                        expect(page.locator("body")).to_have_attribute("data-theme", "dark")
                    else:
                        expect(page.locator("body")).not_to_have_attribute("data-theme", "dark")
                    if width <= 768:
                        page.locator("#hamburgerMenu").click()
                        expect(page.locator("#sidebar")).to_have_class("sidebar active")
                    expect(signin).to_be_visible()
                    expect(signin.locator("strong")).to_be_visible()
                    expect(signin.locator("strong")).to_have_text("Sign in")
                    expect(signin.locator(".guest-signin-copy > span")).to_be_visible()
                    expect(signin.locator(".guest-signin-copy > span")).to_have_text("Save your conversations")
                    box = signin.bounding_box()
                    assert box and box["width"] >= 44 and box["height"] >= 44
                    page.locator("#guestNewChatBtn").focus()
                    page.keyboard.press("Tab")
                    expect(signin).to_be_focused()
                    assert signin.evaluate("el => parseFloat(getComputedStyle(el).outlineWidth)") >= 2
                    capture(page, f"guest-signin-{width}-{theme}")
                    if width > 768:
                        page.locator("#sidebarCollapseBtn").click()
                        expect(signin.locator(".guest-signin-copy")).to_be_hidden()
                        expect(signin.locator("i")).to_be_visible()
                        box = signin.bounding_box()
                        assert box and box["width"] >= 44 and box["height"] >= 44
                        capture(page, f"guest-signin-collapsed-{width}-{theme}")
                    if theme == "light":
                        page.locator("#sidebarCollapseBtn").click()
                        if width > 768:
                            expect(signin.locator(".guest-signin-copy")).to_be_visible()
                        else:
                            expect(page.locator("#sidebar")).to_have_class("sidebar")
                signin.focus()
                page.keyboard.press("Enter")
                expect(page.locator("#loginForm")).to_be_visible()
                guest.close()
            print(json.dumps({"views": len(audit), "errors": errors, "retired_requests": retired_requests, "audit": audit}, indent=2))
            if not args.diagnostic:
                assert not errors, errors
                assert not retired_requests, retired_requests
                print(f"Validated {len(audit)} browser views: no JavaScript errors or retired feed requests.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
