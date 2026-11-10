"""Render and exercise the real chat/dashboard UI against ui_preview.py only."""

import argparse
import json
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
    errors, audit = [], []

    def route_request(route):
        url = urlsplit(route.request.url)
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
            content = page.locator("#announcementBanner")
            if not content.is_visible():
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
            smallInputs: [...document.querySelectorAll('input:not([type=hidden]),textarea,select')].filter(visible)
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

    def exercise_feedback_toggle(page, width):
        message = page.locator(".message.assistant").first
        msg_id = message.get_attribute("id")
        down = message.locator(".feedback-down")
        up = message.locator(".feedback-up")
        feedback_url = base + "/chat/feedback"
        batch_url = feedback_url + "/batch"
        votes = {msg_id: "up"}
        requests = []
        reports = []
        state = {"fail": False, "hold": False}

        def respond(route):
            data = route.request.post_data_json
            requests.append(data)
            if state["fail"]:
                state["fail"] = False
                route.fulfill(status=500, json={"success": False, "message": "Preview feedback save failed."})
                return
            if data["verdict"] is None:
                votes.pop(data["msg_id"], None)
            else:
                votes[data["msg_id"]] = data["verdict"]
            if state["hold"]:
                state["route"] = route
                return
            route.fulfill(json={"success": True, "can_report": data["verdict"] == "down"})

        def restore(route):
            ids = route.request.post_data_json["msg_ids"]
            route.fulfill(json={"success": True, "votes": {key: votes[key] for key in ids if key in votes}})

        def note_report(request):
            if request.url == base + "/chat/report":
                reports.append(request)

        page.route(feedback_url, respond)
        page.route(batch_url, restore)
        page.on("request", note_report)
        expect(up).to_have_attribute("aria-pressed", "true")
        up.click()
        expect(up).to_be_enabled()
        expect(up).to_have_attribute("aria-pressed", "false")

        for theme in ("light", "dark"):
            if theme == "dark":
                page.locator("#themeToggle").click()
            down.click()
            expect(down).to_be_enabled()
            expect(down).to_have_attribute("aria-pressed", "true")
            expect(message.locator(".reason-sheet")).to_be_visible()
            expect(message.locator(".escalate-offer")).to_be_visible()
            capture(page, f"feedback-options-{width}-{theme}")
            down.click()
            expect(down).to_be_enabled()
            expect(down).to_have_attribute("aria-pressed", "false")
            expect(message.locator(".feedback-btn.voted")).to_have_count(0)
            expect(message.locator(".reason-sheet, .escalate-offer")).to_have_count(0)
            assert requests[-1]["verdict"] is None and msg_id not in votes
            down.click()
            expect(down).to_be_enabled()
            expect(message.locator(".reason-sheet")).to_have_count(1)
            up.click()
            expect(up).to_be_enabled()
            expect(up).to_have_attribute("aria-pressed", "true")
            expect(down).to_have_attribute("aria-pressed", "false")
            expect(message.locator(".reason-sheet, .escalate-offer")).to_have_count(0)
            up.click()
            expect(up).to_be_enabled()
            expect(up).to_have_attribute("aria-pressed", "false")

        state["hold"] = True
        before = len(requests)
        down.click()
        expect(down).to_be_disabled()
        down.evaluate("el => window.jQuery(el).trigger('click')")
        assert len(requests) == before + 1
        state["hold"] = False
        state.pop("route").fulfill(json={"success": True, "can_report": True})
        expect(down).to_be_enabled()
        expect(message.locator(".reason-sheet")).to_be_visible()
        message.locator(".reason-other").click()
        message.locator(".reason-other-text").fill("An unsent explanation")
        state["fail"] = True
        down.click()
        expect(down).to_be_enabled()
        expect(down).to_have_attribute("aria-pressed", "true")
        expect(message.locator(".reason-sheet")).to_be_visible()
        expect(message.locator(".reason-other-text")).to_have_value("An unsent explanation")
        assert votes[msg_id] == "down"
        down.click()
        expect(down).to_be_enabled()
        expect(message.locator(".reason-sheet, .escalate-offer")).to_have_count(0)
        assert msg_id not in votes and not reports

        page.locator("#themeToggle").click()
        page.reload(wait_until="networkidle")
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        with page.expect_response(batch_url) as restored:
            page.locator("#conversationList .conversation-item").first.click()
        assert msg_id not in restored.value.json()["votes"]
        expect(page.locator(".message.assistant")).to_have_count(2)
        expect(page.locator(".message.assistant").first.locator(".feedback-btn.voted")).to_have_count(0)
        if width <= 768:
            page.keyboard.press("Escape")
        capture(page, f"feedback-cleared-after-reload-{width}")
        page.unroute(feedback_url)
        page.unroute(batch_url)
        page.remove_listener("request", note_report)

    def exercise_user_editor(page, width, theme):
        if width <= 768:
            page.locator("#hamburgerMenu").click()
        page.locator("#usersMenuItem").click()
        expect(page.locator("#usersSection")).to_be_visible()
        if width <= 768:
            page.keyboard.press("Escape")
        edit_button = page.locator("#userListBody .edit-user").first
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
                context = browser.new_context(viewport={"width": width, "height": 900 if width > 820 else 812})
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
                exercise_feedback_toggle(page, width)
                if width > 768:
                    page.locator("#sidebarCollapseBtn").click()
                    expect(page.locator("#sidebar")).to_have_class("sidebar collapsed")
                    capture(page, f"chat-collapsed-{width}")
                    page.locator("#sidebarCollapseBtn").click()
                page.goto(base + "/dashboard", wait_until="networkidle")
                expect(page.locator("#kpiQuestions")).to_have_text("412")
                expect(page.locator("#attentionList .attention-row")).to_have_count(2)
                page.locator("#analyticsDays").select_option("7")
                expect(page.locator("#analyticsGeneratedAt")).to_contain_text("Last 7 days")
                capture(page, f"dashboard-{width}-light")
                page.locator("#themeToggle").click()
                capture(page, f"dashboard-{width}-dark")
                if width <= 768:
                    page.locator("#hamburgerMenu").click()
                page.locator("#uploadsMenuItem").focus()
                page.keyboard.press("Enter")
                expect(page.locator("#uploadSection")).to_be_visible()
                if width <= 768:
                    page.keyboard.press("Escape")
                capture(page, f"documents-{width}")
                exercise_user_editor(page, width, "dark")
                page.locator("#themeToggle").click()
                exercise_user_editor(page, width, "light")
                context.close()

                guest = browser.new_context(viewport={"width": width, "height": 900 if width > 820 else 812})
                guest.route("**/*", route_request)
                page = guest.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/guest-chat", wait_until="networkidle")
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
            print(json.dumps({"errors": errors, "audit": audit}, indent=2))
            if not args.diagnostic:
                assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
