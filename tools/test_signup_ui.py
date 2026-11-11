"""Browser regression against auth_preview.py ONLY, never a real Cognito app."""

import argparse
import json
import math
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8767")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--channel", choices=("msedge", "chromium"), default="msedge")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    host = urlsplit(base)
    if (host.scheme != "http" or host.hostname not in ("localhost", "127.0.0.1")
            or host.username or host.password or host.path or host.query or host.fragment):
        raise SystemExit("This test only permits the local fake-Cognito preview.")
    with build_opener(ProxyHandler({})).open(base + "/__auth_test__/health", timeout=5) as response:
        marker = json.load(response)
    if marker.get("offline_fixture") is not True:
        raise SystemExit("Missing fake-Cognito marker; refusing to create accounts.")

    from playwright.sync_api import expect, sync_playwright

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    errors, audits = [], []
    run_id = uuid.uuid4().hex[:8]

    def restrict(route):
        target = urlsplit(route.request.url)
        local = target.netloc == host.netloc and target.scheme == host.scheme
        asset = route.request.method == "GET" and target.scheme == "https" and target.hostname in (
            "code.jquery.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com",
        )
        if local or asset:
            route.continue_()
        else:
            route.abort()

    def context_for(browser, width=1280):
        context = browser.new_context(viewport={"width": width, "height": 900 if width > 500 else 812})
        context.route("**/*", restrict)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/signup", wait_until="networkidle")
        expect(page.locator("#signupForm")).to_be_visible()
        expect(page.locator("#loginForm")).to_be_hidden()
        expect(page.locator("#signupButton")).to_be_disabled()
        return context, page

    def audit(page, label, width):
        page.evaluate("""async () => {
          await document.fonts.ready;
          await Promise.all(document.getAnimations().map(a => a.finished.catch(() => {})));
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
        if page.locator("#signupForm").is_visible():
            agreement = page.locator("#openEulaLink")
            expect(agreement).to_have_text("End User Agreement")
            expect(agreement).to_have_css("white-space", "nowrap")
            expect(page.locator("#signupDataConsent")).to_have_accessible_name("I agree to the End User Agreement")
            label_box = page.locator("#signupConsentLabel").bounding_box()
            link_box = agreement.bounding_box()
            assert label_box and link_box and abs(label_box["y"] - link_box["y"]) < 1
            assert page.locator("#signupForm .eula-gate").evaluate("el => el.scrollWidth <= el.clientWidth")
        details = page.evaluate("""() => {
          const visible = el => el.getClientRects().length > 0;
          const color = s => (s.match(/[\\d.]+/g) || []).map(Number);
          const luminance = c => c.slice(0,3).map(x => x / 255)
            .map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4)
            .reduce((sum,x,i) => sum + x * [.2126,.7152,.0722][i], 0);
          function contrast(el) {
            const ancestors = []; for (let n=el; n; n=n.parentElement) ancestors.unshift(n);
            let bg = [255,255,255];
            for (const n of ancestors) {
              const c = color(getComputedStyle(n).backgroundColor), a = c.length===4 ? c[3] : 1;
              if(c.length>=3) bg=bg.map((x,i) => c[i]*a+x*(1-a));
            }
            const fg = color(getComputedStyle(el).color);
            const a = luminance(fg), b = luminance(bg);
            return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
          }
          return {
            width: document.documentElement.scrollWidth,
            viewport: innerWidth,
            card: (() => {
              const card=document.querySelector('.auth-layout').getBoundingClientRect();
              const brand=document.querySelector('.college-brand--auth').getBoundingClientRect();
              return {width:card.width, centerOffset:Math.abs(card.left+card.width/2-innerWidth/2),
                brandOffset:Math.abs(brand.left+brand.width/2-card.left-card.width/2),
                headingAlign:getComputedStyle(document.querySelector('.login-header')).textAlign};
            })(),
            inputs: [...document.querySelectorAll('.form-input')].filter(visible)
              .map(el => ({id:el.id, font:parseFloat(getComputedStyle(el).fontSize)})),
            contrast: [...document.querySelectorAll('#signupForm .field-hint, #signupForm .agreement-link, #loginEntry a, #resetFlowForm .field-hint')]
              .filter(visible).map(el => ({text:el.textContent.trim().slice(0,45),
                color:getComputedStyle(el).color,ratio:contrast(el)})),
            targets: [...document.querySelectorAll('#loginButton,#signupButton,#openEulaLink,#resetButton,#sendCodeButton,.password-toggle,#themeToggle')]
              .filter(visible).map(el => ({id:el.id||el.className,height:el.getBoundingClientRect().height}))
          };
        }""")
        audits.append({"view": label, **details})
        page.screenshot(path=str(output / f"{label}.png"), full_page=True)
        assert details["width"] <= width, details
        assert details["card"]["width"] <= 480 and details["card"]["centerOffset"] < 2, details
        assert details["card"]["brandOffset"] < 2 and details["card"]["headingAlign"] == "center", details
        assert all(i["font"] >= 16 for i in details["inputs"]), details
        assert all(c["ratio"] >= 4.5 for c in details["contrast"]), details
        assert all(t["height"] >= 44 for t in details["targets"]), details

    with sync_playwright() as playwright:
        options = {"headless": True}
        if args.channel == "msedge":
            options["channel"] = "msedge"
        browser = playwright.chromium.launch(**options)
        try:
            for width in (1280, 390, 320):
                context, page = context_for(browser, width)
                expect(page.locator('input[name="signup_method"]')).to_have_count(0)
                expect(page.locator("#signupUsername")).to_be_visible()
                expect(page.locator("#signupUsername")).to_have_attribute("required", "")
                expect(page.locator("#signupUsername")).to_have_attribute("autocomplete", "username")
                expect(page.locator("#signupStudentId")).to_be_visible()
                expect(page.locator("#signupStudentId")).to_have_attribute("required", "")
                expect(page.locator("#signupStudentId")).to_have_attribute("type", "text")
                expect(page.locator("#signupStudentId")).to_have_attribute("inputmode", "numeric")
                expect(page.locator("#signupStudentId")).to_have_attribute("pattern", "[0-9]{1,64}")
                expect(page.locator("#signupEmailHint, #signupStudentIdHint, #signupIdNotice")).to_have_count(0)
                page.locator("#signupUsername").focus()
                page.keyboard.press("Tab")
                expect(page.locator("#signupStudentId")).to_be_focused()
                audit(page, f"signup-{width}-light", width)
                page.locator("#themeToggle").click()
                audit(page, f"signup-{width}-dark", width)
                page.locator("#openEulaLink").click()
                expect(page.locator("#eulaModal")).to_be_visible()
                for _ in range(6):
                    page.keyboard.press("Tab")
                    assert page.evaluate("!!document.activeElement.closest('#eulaModal')")
                page.keyboard.press("Escape")
                expect(page.locator("#eulaModal")).not_to_be_visible()
                expect(page.locator("#openEulaLink")).to_be_focused()
                expect(page.locator("#signupDataConsent")).not_to_be_checked()
                expect(page.locator("#signupButton")).to_be_disabled()
                page.locator("#openEulaLink").click()
                page.locator("#btnAcceptEula").click()
                expect(page.locator("#signupDataConsent")).to_be_checked()
                expect(page.locator("#signupButton")).to_be_enabled()
                page.locator("#signupConsentLabel").click()
                expect(page.locator("#signupDataConsent")).not_to_be_checked()
                expect(page.locator("#signupButton")).to_be_disabled()
                page.locator("#switchToLogin").click()
                expect(page.locator("#loginForm")).to_be_visible()
                expect(page.locator("#signupForm")).to_be_hidden()
                expect(page.locator("#loginDataConsent, #openEulaLinkLogin, #loginForm .eula-gate")).to_have_count(0)
                expect(page.locator("#eulaModal")).not_to_be_visible()
                expect(page.locator("#loginButton")).to_be_enabled()
                audit(page, f"login-{width}-dark", width)
                page.locator("#themeToggle").click()
                audit(page, f"login-{width}-light", width)
                context.close()

            for index, domain in enumerate(("gmail.com", "outlook.com")):
                context, page = context_for(browser, 390 if index else 1280)
                email = f"ui-{run_id}@{domain}"
                username = f"student_{index}_{run_id}"
                identifier = f"0001{index}{int(run_id, 16)}"
                password = "ExamplePass123!"
                page.locator("#signupUsername").fill(username)
                page.locator("#signupStudentId").fill(identifier)
                page.locator("#signupEmail").fill("test@@gmail.com")
                page.locator("#signupPassword").fill(password)
                page.locator("#signupDataConsent").focus()
                page.keyboard.press("Space")
                expect(page.locator("#signupDataConsent")).to_be_checked()
                before = context.request.get(base + "/__auth_test__/health").json()["operations"].get("sign_up", 0)
                page.locator("#signupButton").click()
                assert page.locator("#signupEmail").evaluate("el => el.validity.customError")
                assert context.request.get(base + "/__auth_test__/health").json()["operations"].get("sign_up", 0) == before
                forged = context.request.post(base + "/signup", headers={
                    "X-Auth-CSRF": page.locator("body").get_attribute("data-auth-csrf"),
                }, form={
                    "username": username, "student_id": identifier,
                    "email": "x@@gmail.com", "password": "ExamplePass123!", "data_consent": "true",
                })
                assert forged.status == 400
                page.locator("#signupEmail").fill(email.upper())
                for invalid_username in ("", "bad user", "user@example.com", "user/name"):
                    page.locator("#signupUsername").fill(invalid_username)
                    page.locator("#signupButton").click()
                    assert page.locator("#signupUsername").evaluate("el => el.validity.customError")
                    assert context.request.get(base + "/__auth_test__/health").json()["operations"].get("sign_up", 0) == before
                page.locator("#signupUsername").fill(username)
                for invalid_id in ("ID123", "2024-001", "12.34", "1e3", "12 34"):
                    page.locator("#signupStudentId").fill(invalid_id)
                    page.locator("#signupButton").click()
                    assert page.locator("#signupStudentId").evaluate("el => el.validity.customError")
                    expect(page.locator("#signupStudentId")).to_have_attribute("aria-invalid", "true")
                    assert context.request.get(base + "/__auth_test__/health").json()["operations"].get("sign_up", 0) == before
                page.locator("#signupStudentId").fill(identifier)
                page.locator("#signupButton").click()
                expect(page.locator("#signupVerifyForm")).to_be_visible()
                expect(page.locator("#signupForm")).to_be_hidden()
                expect(page.locator("#signupVerificationEmail")).to_have_text(email)
                assert context.request.get(base + "/chat").json()["user"] is None
                expect(page.locator("#resendSignupCodeButton")).to_be_disabled()

                # Move only the fake server's clock. Reopening the page must
                # derive the remaining time from server state, not start at30.
                sends = context.request.get(base + "/__auth_test__/health").json()["operations"].get("resend_confirmation_code", 0)
                advanced = context.request.post(base + "/__auth_test__/advance-time", data={"seconds": 12})
                assert advanced.ok
                page.locator("#backToLoginFromVerify").click()
                page.locator("#switchToSignup").click()
                page.wait_for_load_state("networkidle")
                expect(page.locator("#signupVerifyForm")).to_be_visible()
                state = json.loads(page.locator("#signupState").inner_text())
                expected = max(0, math.ceil((state["resend_available_at"] - state["server_now"]) / 1000))
                remaining = re.search(r"(\d+) seconds", page.locator("#signupResendStatus").inner_text())
                assert expected < 25 and remaining and int(remaining.group(1)) <= expected
                deadline = state["resend_available_at"]
                page.reload(wait_until="networkidle")
                refreshed = json.loads(page.locator("#signupState").inner_text())
                assert refreshed["resend_available_at"] == deadline
                assert context.request.get(base + "/__auth_test__/health").json()["operations"].get("resend_confirmation_code", 0) == sends

                page.locator("#restartSignupButton").click()
                expect(page.locator("#signupForm")).to_be_visible()
                expect(page.locator('input[name="signup_method"]')).to_have_count(0)
                expect(page.locator("#signupStudentId")).to_be_visible()
                expect(page.locator("#signupUsername")).to_have_value("")
                expect(page.locator("#signupStudentId")).to_have_value("")
                expect(page.locator("#signupEmail")).to_have_value("")
                expect(page.locator("#signupPassword")).to_have_value("")
                expect(page.locator("#signupConfirmPassword")).to_have_value("")
                expect(page.locator("#signupDataConsent")).not_to_be_checked()
                page.reload(wait_until="networkidle")
                expect(page.locator("#signupForm")).to_be_visible()
                email = f"corrected-{run_id}@{domain}"
                username = f"corrected_{index}_{run_id}"
                password = "RestartedPass456!"
                identifier = f"0002{index}{int(run_id, 16)}"
                page.locator("#signupUsername").fill(username)
                page.locator("#signupStudentId").fill(identifier)
                page.locator("#signupEmail").fill(email.upper())
                page.locator("#signupPassword").fill(password)
                page.locator("#signupDataConsent").focus()
                page.keyboard.press("Space")
                page.screenshot(path=str(output / f"restart-signup-{domain}.png"), full_page=True)
                page.locator("#signupButton").click()
                expect(page.locator("#signupVerifyForm")).to_be_visible()
                expect(page.locator("#signupVerificationEmail")).to_have_text(email)
                expect(page.locator("#resendSignupCodeButton")).to_be_disabled()
                page.reload(wait_until="networkidle")
                expect(page.locator("#signupVerificationEmail")).to_have_text(email)
                page.screenshot(path=str(output / f"verification-{domain}.png"), full_page=True)
                page.locator("#signupCode").fill("111111")
                page.locator("#confirmSignupButton").click()
                expect(page.locator("#authMessage")).to_contain_text("incorrect")
                page.locator("#signupCode").fill("123456")
                page.locator("#confirmSignupButton").click()
                expect(page.locator("#loginForm")).to_be_visible()
                expect(page.locator("#email")).to_have_value(email)
                expect(page.locator("#signupPassword")).to_have_value("")
                expect(page.locator("#signupCode")).to_have_value("")
                id_login = context.request.post(base + "/login", form={"email": identifier, "password": password})
                assert id_login.json()["success"] is False
                page.locator("#email").fill(username)
                page.locator("#password").fill(password)
                expect(page.locator("#loginButton")).to_be_enabled()
                page.locator("#loginButton").click()
                page.wait_for_url(base + "/chat")
                assert context.request.get(base + "/chat").json()["user"] == email
                assert context.request.get(base + "/chat").json()["username"] == username
                context.close()

            reset_generation = 0
            for width in (1280, 390, 320):
                context, page = context_for(browser, width)
                context.request.post(base + "/__auth_test__/advance-time", data={"seconds": 31})
                page.goto(base + "/auth", wait_until="networkidle")
                page.locator("#forgotPasswordLink").click()
                expect(page.locator("#resetFlowForm")).to_be_visible()
                expect(page.locator("#forgotPasswordForm, #resendCodeLink")).to_have_count(0)
                for field in ("resetEmail", "resetVerificationCode", "newPassword", "confirmPassword"):
                    control = page.locator("#" + field)
                    expect(control).to_be_visible()
                    expect(control).to_be_enabled()
                    assert control.evaluate("el => el.form.id") == "resetFlowForm"
                expect(page.locator("#sendCodeButton")).to_have_text("Send OTP")
                expect(page.locator("#resetButton")).to_be_disabled()
                audit(page, f"reset-unified-{width}-initial", width)
                page.locator("#resetEmail").fill(email)
                password = f"ChangedPass{width}!"
                page.locator("#newPassword").fill(password)
                page.locator("#confirmPassword").fill(password)
                health_before = context.request.get(base + "/__auth_test__/health").json()
                writes = health_before["password_reset_writes"]
                confirms = health_before["operations"].get("confirm_forgot_password", 0)
                with page.expect_request(base + "/forgot-password") as requested:
                    if width == 1280:
                        page.locator("#resetEmail").press("Enter")
                    else:
                        page.locator("#sendCodeButton").click()
                assert parse_qs(requested.value.post_data) == {"email": [email]}
                expect(page.locator("#resetFlowForm")).to_be_visible()
                expect(page.locator("#sendCodeButton")).to_have_text("Resend OTP")
                expect(page.locator("#newPassword")).to_have_value(password)
                expect(page.locator("#confirmPassword")).to_have_value(password)
                reset_generation += 1
                expect(page.locator("#resetCodeForm, #verifyResetCodeButton")).to_have_count(0)
                expect(page.locator("#resetVerificationCode")).to_be_enabled()
                expect(page.locator("#newPassword")).to_be_enabled()
                expect(page.locator("#confirmPassword")).to_be_enabled()
                expect(page.locator("#sendCodeButton")).to_be_disabled()
                expect(page.locator("#resetButton")).to_be_enabled()
                health = context.request.get(base + "/__auth_test__/health").json()
                assert health["password_reset_writes"] == writes
                assert health["operations"].get("confirm_forgot_password", 0) == confirms
                assert context.request.get(base + "/chat").json()["user"] is None
                audit(page, f"reset-combined-{width}-light", width)
                page.locator("#themeToggle").click()
                audit(page, f"reset-combined-{width}-dark", width)

                calls_before_email_change = health["operations"].get("forgot_password", 0)
                page.locator("#resetVerificationCode").fill(f"{654320 + reset_generation:06d}")
                page.locator("#resetEmail").fill("another-account@example.com")
                expect(page.locator("#resetButton")).to_be_disabled()
                expect(page.locator("#resetVerificationCode")).to_have_value("")
                expect(page.locator("#newPassword")).to_have_value("")
                expect(page.locator("#resetResendStatus")).to_contain_text("Email changed")
                assert context.request.get(base + "/__auth_test__/health").json()["operations"].get("forgot_password", 0) == calls_before_email_change
                page.locator("#resetEmail").fill(email)
                page.locator("#resetVerificationCode").fill("000000")
                page.locator("#newPassword").fill(password)
                page.locator("#confirmPassword").fill(password)
                page.locator("#resetButton").click()
                expect(page.locator("#authMessage")).to_contain_text("incorrect")
                expect(page.locator("#resetFlowForm")).to_be_visible()
                expect(page.locator("#newPassword")).to_have_value(password)
                assert context.request.get(base + "/__auth_test__/health").json()["password_reset_writes"] == writes

                sends = context.request.get(base + "/__auth_test__/health").json()["operations"]["forgot_password"]
                context.request.post(base + "/__auth_test__/advance-time", data={"seconds": 31})
                page.reload(wait_until="networkidle")
                expect(page.locator("#resetFlowForm")).to_be_visible()
                expect(page.locator("#resetVerificationCode")).to_have_value("")
                expect(page.locator("#newPassword")).to_have_value("")
                assert context.request.get(base + "/__auth_test__/health").json()["operations"]["forgot_password"] == sends
                expect(page.locator("#resetEmail")).to_have_value(email)
                expect(page.locator("#sendCodeButton")).to_be_enabled()
                expect(page.locator("#sendCodeButton")).to_have_text("Resend OTP")
                page.locator("#sendCodeButton").click()
                expect(page.locator("#sendCodeButton")).to_be_disabled()
                expect(page.locator("#authMessage")).to_contain_text("code")
                reset_generation += 1
                page.locator("#resetVerificationCode").fill(f"{654320 + reset_generation:06d}")
                page.locator("#newPassword").fill(password)
                page.locator("#confirmPassword").fill(password)
                audit(page, f"reset-combined-{width}-ready", width)
                with page.expect_request(base + "/reset-password") as reset_request:
                    page.locator("#resetButton").click()
                assert parse_qs(reset_request.value.post_data)["email"] == [email]
                expect(page.locator("#loginForm")).to_be_visible()
                expect(page.locator("#email")).to_have_value(email)
                expect(page.locator("#resetVerificationCode")).to_have_value("")
                expect(page.locator("#newPassword")).to_have_value("")
                health = context.request.get(base + "/__auth_test__/health").json()
                assert health["password_reset_writes"] == writes + 1
                assert health["operations"].get("admin_set_user_password", 0) == health_before["operations"].get("admin_set_user_password", 0)
                assert health["operations"].get("respond_to_auth_challenge", 0) == health_before["operations"].get("respond_to_auth_challenge", 0)
                assert context.request.get(base + "/chat").json()["user"] is None
                page.locator("#password").fill(password)
                expect(page.locator("#loginButton")).to_be_enabled()
                page.locator("#loginButton").click()
                page.wait_for_url(base + "/chat")
                context.close()

            context, page = context_for(browser, 390)
            page.goto(base + "/auth", wait_until="networkidle")
            page.locator("#guestButton").click()
            page.wait_for_url(base + "/chat")
            assert context.request.get(base + "/chat").json()["user"] == "guest"
            context.close()

            assert not errors, errors
            print(json.dumps({"status": "passed", "flows": ["username + ID + Gmail", "username + ID + Outlook", "password-reset", "guest"],
                              "viewports": [1280, 390, 320], "page_errors": errors, "audits": audits}, indent=2))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
