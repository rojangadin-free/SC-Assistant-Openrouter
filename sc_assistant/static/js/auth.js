$(document).ready(function() {
    const forms = {
        login: $('#loginForm'),
        signup: $('#signupForm'),
        verify: $('#signupVerifyForm'),
        reset: $('#resetFlowForm')
    };
    const allForms = $('#loginForm, #signupForm, #signupVerifyForm, #resetFlowForm');
    const navigation = $('#switchToSignup, #switchToLogin, #forgotPasswordLink, #backToLoginFromReset, #backToLoginFromVerify, #refreshResetLink');
    const body = $('body');
    const formTitle = $('#formTitle');
    const formSubtitle = $('#formSubtitle');
    const signupEmail = $('#signupEmail');
    const signupUsername = $('#signupUsername');
    const studentId = $('#signupStudentId');
    const signupPassword = $('#signupPassword');
    const signupConfirmPassword = $('#signupConfirmPassword');
    const signupCode = $('#signupCode');
    const signupConsent = $('#signupDataConsent');
    const newPassword = $('#newPassword');
    const confirmPassword = $('#confirmPassword');
    const resetEmail = $('#resetEmail');
    const resetCode = $('#resetVerificationCode');
    const notification = $('#notification');
    const authMessage = $('#authMessage');
    const eulaModal = document.getElementById('eulaModal');
    let activeView = '';
    let requestPending = false;
    let redirectPending = false;
    let pendingSignupEmail = forms.verify.attr('data-signup-email') || '';
    let signupResendAt = 0;
    let resetResendAt = 0;
    let resetExpiresAt = 0;
    let resetStage = 'expired';
    let pendingResetEmail = '';
    let cooldownTimer;
    let notificationTimer;
    let eulaOpener;

    function isBusy() {
        return requestPending || redirectPending;
    }

    function clearNotification() {
        clearTimeout(notificationTimer);
        notification.removeClass('show');
        authMessage.empty().removeClass('success error');
    }

    function showNotification(message, type = 'success', inline = false) {
        clearNotification();
        if (inline || ['signup', 'verify', 'reset'].includes(activeView)) {
            authMessage.addClass(type).text(message);
            return;
        }
        notification.removeClass('success error').addClass(type);
        notification.find('.notification-title').text(type === 'success' ? 'Success' : 'Error');
        notification.find('.notification-message').text(message);
        notification.find('.notification-icon i').attr('class', type === 'success' ? 'fas fa-check' : 'fas fa-exclamation-triangle');
        notification.addClass('show');
        notificationTimer = setTimeout(() => notification.removeClass('show'), type === 'error' ? 5000 : 3000);
    }

    function updateCooldowns() {
        const now = Date.now();
        if (resetExpiresAt && now >= resetExpiresAt && !isBusy()) {
            resetExpiresAt = 0;
            resetStage = 'expired';
            clearSensitiveFields(forms.reset);
            if (activeView === 'reset') {
                showNotification('This OTP request has expired. Send a new OTP to continue.', 'error');
            }
        }
        const seconds = Math.max(0, Math.ceil((signupResendAt - now) / 1000));
        $('#resendSignupCodeButton').prop('disabled', isBusy() || activeView !== 'verify' || seconds > 0);
        $('#signupResendStatus').text(seconds > 0
            ? `You can request another code in ${seconds} seconds.`
            : 'You can now request a new code.');
        const resetSeconds = Math.max(0, Math.ceil((resetResendAt - now) / 1000));
        const processing = resetStage === 'sending' || resetStage === 'writing';
        const emailChanged = pendingResetEmail && resetEmail.val().trim().toLowerCase() !== pendingResetEmail;
        $('#sendCodeButton').prop('disabled', isBusy() || activeView !== 'reset' || processing || resetSeconds > 0);
        if (!isBusy()) $('#sendCodeButton').text(hasActiveReset() ? 'Resend OTP' : 'Send OTP');
        $('#resetButton').prop('disabled', isBusy() || activeView !== 'reset' || !hasActiveReset());
        $('#resetResendStatus').text(resetSeconds > 0
            ? `${emailChanged ? 'Email changed. ' : ''}You can request another OTP in ${resetSeconds} seconds.`
            : hasActiveReset() ? 'Enter the latest OTP from your email.'
            : emailChanged
                ? 'Email changed. Send an OTP for this address.' : 'Send an OTP to this email to continue.');
        $('#resetProgressNotice').prop('hidden', activeView !== 'reset' || !processing);
        if (seconds === 0 && resetSeconds === 0 && now >= resetExpiresAt) {
            clearInterval(cooldownTimer);
            cooldownTimer = null;
        }
    }

    function syncSignupState(response) {
        const state = response && response.signup_state;
        if (!state) return;
        if (typeof state.email === 'string') {
            pendingSignupEmail = state.email;
            $('#signupVerificationEmail').text(state.email || 'your email');
        }
        if (Number.isFinite(state.resend_available_at) && Number.isFinite(state.server_now)) {
            // Offset against the server clock. A page visit never issues a code
            // or grants a fresh 30 seconds; only server state moves this deadline.
            signupResendAt = Date.now() + Math.max(0, state.resend_available_at - state.server_now);
            updateCooldowns();
            if (!cooldownTimer && Date.now() < signupResendAt) cooldownTimer = setInterval(updateCooldowns, 1000);
        }
    }

    function hasActiveReset() {
        return resetStage === 'code' && Date.now() < resetExpiresAt
            && resetEmail.val().trim().toLowerCase() === pendingResetEmail;
    }

    function syncResetState(response) {
        const state = response && response.reset_state;
        if (!state || typeof state !== 'object' || Array.isArray(state)
            || !['code', 'sending', 'failed', 'expired', 'writing'].includes(state.stage)) return;
        resetStage = state.stage;
        if (typeof state.email === 'string') {
            pendingResetEmail = state.email;
        }
        const now = Date.now();
        if (Number.isFinite(state.resend_available_at) && Number.isFinite(state.server_now)) {
            resetResendAt = now + Math.max(0, state.resend_available_at - state.server_now);
        }
        resetExpiresAt = Number.isFinite(state.expires_at) && Number.isFinite(state.server_now)
            && !['failed', 'expired'].includes(state.stage)
            ? now + Math.max(0, state.expires_at - state.server_now) : 0;
        if (!cooldownTimer && (now < resetResendAt || now < resetExpiresAt)) {
            cooldownTimer = setInterval(updateCooldowns, 1000);
        }
    }

    function handleResetFailure() {
        if (activeView !== 'reset') return;
        if (!hasActiveReset()) {
            clearSensitiveFields(forms.reset);
        }
    }

    function updateControls() {
        const busy = isBusy();
        allForms.each(function() {
            $(this).find('input, button').prop('disabled', busy || this.hidden);
        });
        allForms.attr('aria-busy', busy ? 'true' : null);
        navigation.attr('aria-disabled', busy ? 'true' : null);
        $('#loginButton').prop('disabled', busy || activeView !== 'login');
        $('#signupButton').prop('disabled', busy || activeView !== 'signup' || !signupConsent.prop('checked'));
        forms.reset.find('input, button').prop('disabled', busy || activeView !== 'reset'
            || ['sending', 'writing'].includes(resetStage));
        updateCooldowns();
    }

    function clearSensitiveFields(form) {
        form.find('input[autocomplete="current-password"], input[autocomplete="new-password"], input[autocomplete="one-time-code"]').val('');
        form.find('.password-toggle').each(function() {
            $('#' + this.getAttribute('aria-controls')).attr('type', 'password');
            $(this).attr({ 'aria-pressed': 'false', 'aria-label': 'Show password' }).find('i').attr('class', 'fas fa-eye');
        });
        form.find('input').each(function() {
            this.setCustomValidity('');
            $(this).removeAttr('aria-invalid');
        });
    }

    function showView(view, focus = true) {
        if (isBusy() || !forms[view]) return;
        if (activeView && activeView !== view) clearSensitiveFields(forms[activeView]);
        activeView = view;
        allForms.prop('hidden', true);
        forms[view].prop('hidden', false);
        $('#guestSection').prop('hidden', view !== 'login');
        $('#toggleLink').prop('hidden', view !== 'login' && view !== 'signup');
        $('#signupEntry').prop('hidden', view !== 'login');
        $('#loginEntry').prop('hidden', view !== 'signup');
        $('#resetPasswordNavigation').prop('hidden', view !== 'reset');
        const headings = {
            login: ['Sign in', 'Use your username or email to continue.'],
            signup: ['Create account', 'Choose a username and enter your account details.'],
            verify: ['Verify your email', 'Confirm your email before signing in.'],
            reset: ['Reset password', 'Send an OTP to your email and set a new password.']
        };
        formTitle.text(headings[view][0]);
        formSubtitle.text(headings[view][1]);
        document.title = headings[view][0] + ' - Samar College Assistant';
        if (view === 'reset') window.history.replaceState(null, '', '/password-reset');
        clearNotification();
        updateControls();
        if (focus) formTitle.trigger('focus');
    }

    // Capture the payload before disabling controls. One lock covers every auth
    // action, including guest/resend, and blocks edits or navigation mid-request.
    function postForm(form, button, url, data, loadingText, onSuccess) {
        if (isBusy() || form.prop('hidden')) return;
        const buttonContent = button.html();
        clearNotification();
        requestPending = true;
        updateControls();
        button.html('<span class="loading" aria-hidden="true"></span>').append(document.createTextNode(loadingText));
        const headers = ['/signup', '/confirm-signup', '/resend-signup-code', '/restart-signup',
            '/forgot-password', '/reset-password'].includes(url)
            ? { 'X-Auth-CSRF': body.attr('data-auth-csrf') || '' } : {};
        $.ajax({ url, method: 'POST', data, headers, dataType: 'json', timeout: 30000 })
            .always(function() {
                requestPending = false;
                button.html(buttonContent);
            })
            .done(function(response) {
                syncSignupState(response);
                syncResetState(response);
                if (response && response.success === true) {
                    updateControls();
                    onSuccess(response);
                }
                else {
                    handleResetFailure();
                    showNotification(response && response.message || 'The request could not be completed. Please try again.', 'error');
                }
                updateControls();
            })
            .fail(function(xhr) {
                syncSignupState(xhr.responseJSON);
                syncResetState(xhr.responseJSON);
                handleResetFailure();
                const message = xhr.responseJSON && xhr.responseJSON.message;
                showNotification(message || 'The request could not be completed. Check your connection and try again.', 'error');
                updateControls();
            });
    }

    navigation.on('click', function(event) {
        if (isBusy()) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    });
    // Signup and recovery remain real entry navigations so the server decides
    // which pending stage and absolute resend deadline can be resumed.
    $('#switchToLogin, #backToLoginFromReset, #backToLoginFromVerify').on('click', function(event) {
        event.preventDefault();
        showView('login');
        window.history.replaceState(null, '', '/auth');
    });

    $('#openEulaLink').on('click', function() {
        if (isBusy()) return;
        eulaOpener = this;
        eulaModal.showModal();
        $('#eulaTitle').trigger('focus');
    });
    signupConsent.on('change', updateControls);
    $('#btnAcceptEula').on('click', function() {
        if (!isBusy() && activeView === 'signup') signupConsent.prop('checked', true).trigger('change');
        eulaModal.close();
    });
    $('#btnDeclineEula').on('click', function() { eulaModal.close(); });
    $(eulaModal).on('close', function() {
        if (eulaOpener && $(eulaOpener).is(':visible')) eulaOpener.focus();
    });
    $(eulaModal).on('keydown', function(event) {
        if (event.key !== 'Tab') return;
        const controls = $(this).find('button:not(:disabled), a[href], [tabindex="0"]')
            .filter(':visible').toArray();
        if (!controls.length) return;
        const current = controls.indexOf(document.activeElement);
        if (event.shiftKey && current <= 0) {
            event.preventDefault();
            controls[controls.length - 1].focus();
        } else if (!event.shiftKey && (current < 0 || current === controls.length - 1)) {
            event.preventDefault();
            controls[0].focus();
        }
    });

    $('.password-toggle').on('click', function() {
        if (isBusy()) return;
        const input = $('#' + this.getAttribute('aria-controls'));
        const reveal = input.attr('type') === 'password';
        input.attr('type', reveal ? 'text' : 'password');
        $(this).attr({ 'aria-pressed': String(reveal), 'aria-label': reveal ? 'Hide password' : 'Show password' });
        $(this).find('i').attr('class', reveal ? 'fas fa-eye-slash' : 'fas fa-eye');
    });

    function setTheme(theme) {
        const dark = theme === 'dark';
        body.attr('data-theme', dark ? 'dark' : null);
        $('#themeToggle').attr({ title: dark ? 'Switch to light mode' : 'Switch to dark mode', 'aria-label': dark ? 'Switch to light mode' : 'Switch to dark mode' })
            .html(dark ? '<i class="fas fa-sun" aria-hidden="true"></i>' : '<i class="fas fa-moon" aria-hidden="true"></i>');
        try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (error) { /* Storage can be disabled. */ }
    }
    let savedTheme = 'light';
    try {
        savedTheme = localStorage.getItem('theme') || 'light';
        const savedIdentifier = localStorage.getItem('rememberedEmail');
        if (savedIdentifier) {
            $('#email').val(savedIdentifier);
            $('#rememberMe').prop('checked', true);
        }
    } catch (error) { /* Authentication does not require local storage. */ }
    setTheme(savedTheme);
    $('#themeToggle').on('click', function() { setTheme(body.attr('data-theme') === 'dark' ? 'light' : 'dark'); });

    forms.signup.add(forms.verify).find('input').on('input change', function() {
        this.setCustomValidity('');
        $(this).removeAttr('aria-invalid');
        if (this === signupPassword[0]) {
            signupConfirmPassword[0].setCustomValidity('');
            signupConfirmPassword.removeAttr('aria-invalid');
        }
    });
    resetCode.on('input change', function() { setValidity(resetCode, ''); });
    resetEmail.on('input', function() {
        setValidity(resetEmail, '');
        if (pendingResetEmail && resetEmail.val().trim().toLowerCase() !== pendingResetEmail) {
            clearSensitiveFields(forms.reset);
            clearNotification();
        }
        updateControls();
    });
    resetEmail.on('keydown', function(event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            requestResetCode();
        }
    });
    signupEmail.on('blur', function() {
        if (!isBusy()) $(this).val($(this).val().trim().toLowerCase());
    });

    function setValidity(input, message) {
        input[0].setCustomValidity(message);
        input.attr('aria-invalid', message ? 'true' : null);
    }

    function validateSignupEmail(input) {
        const email = input.val().trim().toLowerCase();
        input.val(email);
        const parts = email.split('@');
        const local = parts[0];
        let emailError = '';
        if (!email) emailError = 'Enter an email address you can access.';
        else if (parts.length !== 2 || /\s/.test(email) || !local || local.length > 64 || email.length > 254 || local.startsWith('.') || local.endsWith('.') || local.includes('..')) {
            emailError = 'Enter a valid email address without spaces or extra dots.';
        } else if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/i.test(parts[1])) {
            emailError = 'Enter a valid email domain, such as gmail.com or your email provider.';
        }
        setValidity(input, emailError);
    }

    function validateSignup() {
        validateSignupEmail(signupEmail);
        signupUsername.val(signupUsername.val().trim());
        setValidity(signupUsername, !/^[A-Za-z0-9_.-]{1,64}$/.test(signupUsername.val())
            ? 'Use 1-64 letters, numbers, periods, underscores or hyphens for your username.' : '');
        studentId.val(studentId.val().trim());
        setValidity(studentId, !/^[0-9]{1,64}$/.test(studentId.val())
            ? 'Enter your ID number using 1-64 digits (0-9) only.' : '');
        setValidity(signupPassword, signupPassword.val().length < 8 || signupPassword.val().length > 256
            ? 'Use a password between 8 and 256 characters.' : '');
        setValidity(signupConfirmPassword, signupConfirmPassword.val() && signupConfirmPassword.val() !== signupPassword.val()
            ? 'Passwords do not match.' : '');
        return forms.signup[0].reportValidity();
    }

    function returnToSignIn(email, message) {
        clearSensitiveFields(allForms);
        forms.signup[0].reset();
        pendingSignupEmail = '';
        signupResendAt = 0;
        showView('login');
        $('#email').val(email);
        window.history.replaceState(null, '', '/auth');
        showNotification(message || 'Email verified. You can now sign in.', 'success', true);
    }

    forms.signup.on('submit', function(event) {
        event.preventDefault();
        if (isBusy() || activeView !== 'signup') return;
        if (!signupConsent.prop('checked')) {
            showNotification('Please accept the End User Agreement to continue.', 'error');
            signupConsent.trigger('focus');
            return;
        }
        if (!validateSignup()) return;
        const email = signupEmail.val();
        postForm(forms.signup, $('#signupButton'), '/signup', forms.signup.serialize(), 'Creating Account...', function(response) {
            const normalizedEmail = typeof response.email === 'string' ? response.email : email;
            clearSensitiveFields(forms.signup);
            if (response.requires_confirmation === false) {
                returnToSignIn(normalizedEmail, response.message || 'Account created. Please sign in.');
                return;
            }
            pendingSignupEmail = normalizedEmail;
            $('#signupVerificationEmail').text(pendingSignupEmail);
            signupCode.val('');
            showView('verify');
            showNotification(response.message || 'Check your email for a verification code, or request a new code below.', response.delivery_pending ? 'error' : 'success');
        });
    });

    forms.verify.on('submit', function(event) {
        event.preventDefault();
        if (isBusy() || activeView !== 'verify') return;
        const code = signupCode.val();
        setValidity(signupCode, /^[0-9]{6}$/.test(code) ? '' : 'Enter the 6-digit code from your email.');
        if (!forms.verify[0].reportValidity()) return;
        const email = pendingSignupEmail;
        postForm(forms.verify, $('#confirmSignupButton'), '/confirm-signup', { code }, 'Verifying...', function(response) {
            returnToSignIn(email, response.message);
        });
    });
    $('#resendSignupCodeButton').on('click', function() {
        if (isBusy() || activeView !== 'verify' || Date.now() < signupResendAt) return;
        // Account identity comes only from the server's signed pending session.
        postForm(forms.verify, $(this), '/resend-signup-code', '', 'Sending...', function(response) {
            signupCode.val('');
            setValidity(signupCode, '');
            showNotification(response.message || 'A new code has been requested. Check your email.');
            signupCode.trigger('focus');
        });
    });

    $('#restartSignupButton').on('click', function() {
        if (isBusy() || activeView !== 'verify') return;
        postForm(forms.verify, $(this), '/restart-signup', '', 'Opening Signup...', function() {
            forms.signup[0].reset();
            clearSensitiveFields(allForms);
            pendingSignupEmail = '';
            signupResendAt = 0;
            forms.verify.attr('data-signup-email', '');
            $('#signupState').text('{}');
            showView('signup');
            window.history.replaceState(null, '', '/signup');
            studentId.trigger('focus');
        });
    });

    forms.login.on('submit', function(event) {
        event.preventDefault();
        if (isBusy() || activeView !== 'login') return;
        try {
            if ($('#rememberMe').prop('checked')) localStorage.setItem('rememberedEmail', $('#email').val());
            else localStorage.removeItem('rememberedEmail');
        } catch (error) { /* Remembering an identifier is optional. */ }
        postForm(forms.login, $('#loginButton'), '/login', forms.login.serialize(), 'Signing In...', function(response) {
            redirectPending = true;
            updateControls();
            window.location.href = response.redirect;
        });
    });
    $('#guestButton').on('click', function() {
        postForm(forms.login, $(this), '/guest', '', 'Entering as Guest...', function(response) {
            redirectPending = true;
            updateControls();
            window.location.href = response.redirect;
        });
    });

    function requestResetCode() {
        if (isBusy() || activeView !== 'reset' || Date.now() < resetResendAt
            || ['sending', 'writing'].includes(resetStage)) return;
        validateSignupEmail(resetEmail);
        if (!resetEmail[0].reportValidity()) return;
        const email = resetEmail.val();
        // Requesting an OTP must never submit password drafts or validate other fields.
        postForm(forms.reset, $('#sendCodeButton'), '/forgot-password', { email }, 'Sending...', function(response) {
            if (!response.reset_state || response.reset_state.stage !== 'code'
                || response.reset_state.email !== email || !hasActiveReset()) {
                handleResetFailure();
                showNotification('An OTP could not be requested. Please try again.', 'error');
                return;
            }
            resetCode.val('');
            setValidity(resetCode, '');
            showNotification(response.message || 'Check your email for the OTP.');
            resetCode.trigger('focus');
        });
    }
    $('#sendCodeButton').on('click', requestResetCode);
    function checkPasswordStrength(password) {
        const requirements = {
            length: password.length >= 8,
            uppercase: /[A-Z]/.test(password),
            lowercase: /[a-z]/.test(password),
            number: /[0-9]/.test(password),
            special: /[!@#$%^&*(),.?":{}|<>]/.test(password)
        };
        let strength = 0;
        Object.keys(requirements).forEach(function(key) {
            const valid = requirements[key];
            const requirement = $('#' + key + 'Req');
            requirement.toggleClass('valid', valid).toggleClass('invalid', !valid);
            requirement.find('i').attr('class', valid ? 'fas fa-check-circle' : 'fas fa-circle');
            if (valid) strength++;
        });
        const color = strength < 2 ? 'var(--ui-danger-ink)' : strength < 4 ? 'var(--ui-warn-ink)' : 'var(--ui-success-ink)';
        $('#strengthBar').css({ width: (strength / 5 * 100) + '%', background: color });
        $('#strengthText').text(password ? strength < 2 ? 'Weak' : strength < 4 ? 'Medium' : 'Strong' : '').css('color', color);
        return strength;
    }

    function checkPasswordMatch() {
        const hasConfirmation = confirmPassword.val().length > 0;
        const matches = newPassword.val() === confirmPassword.val();
        $('#passwordMatch').prop('hidden', !hasConfirmation).toggleClass('valid', matches).toggleClass('invalid', !matches);
        $('#passwordMatch i').attr('class', matches ? 'fas fa-check-circle' : 'fas fa-times-circle');
        $('#passwordMatch span').text(matches ? 'Passwords match' : 'Passwords do not match');
        return hasConfirmation && matches;
    }
    newPassword.on('input', function() { checkPasswordStrength($(this).val()); checkPasswordMatch(); });
    confirmPassword.on('input', checkPasswordMatch);
    forms.reset.on('submit', function(event) {
        event.preventDefault();
        if (isBusy() || activeView !== 'reset') return;
        if (!hasActiveReset()) {
            handleResetFailure();
            showNotification('Send an OTP to the email address shown before resetting your password.', 'error');
            return;
        }
        const code = resetCode.val().trim();
        setValidity(resetCode, /^[0-9]{6}$/.test(code) ? '' : 'Enter the 6-digit reset code from your email.');
        if (!forms.reset[0].reportValidity()) return;
        if (newPassword.val().length < 8 || checkPasswordStrength(newPassword.val()) < 3) {
            showNotification('Please choose a stronger password.', 'error');
            return;
        }
        if (!checkPasswordMatch()) {
            showNotification('Passwords do not match.', 'error');
            confirmPassword.trigger('focus');
            return;
        }
        const email = resetEmail.val().trim().toLowerCase();
        const data = { email, code, password: newPassword.val(), confirmPassword: confirmPassword.val() };
        postForm(forms.reset, $('#resetButton'), '/reset-password', data, 'Resetting...', function(response) {
            resetStage = 'expired';
            resetResendAt = 0;
            resetExpiresAt = 0;
            pendingResetEmail = '';
            $('#resetState').text('{}');
            $('#resetEmail').val('');
            returnToSignIn(email, response.message || 'Password reset successful! You can now sign in.');
        });
    });

    $(window).on('pagehide', function() { clearSensitiveFields(allForms); });
    $(window).on('pageshow', function(event) {
        // Back/forward cache must not restore a stale reset request or a
        // form left disabled while its previous request was navigating away.
        if (event.originalEvent && event.originalEvent.persisted) window.location.reload();
    });
    const initialView = body.attr('data-initial-view');
    try { syncSignupState({ signup_state: JSON.parse($('#signupState').text() || '{}') }); }
    catch (error) { /* Missing state must not invent a new resend request. */ }
    try { syncResetState({ reset_state: JSON.parse($('#resetState').text() || '{}') }); }
    catch (error) { /* Missing state cannot replace Cognito's code verification. */ }
    showView(['signup', 'verify', 'reset'].includes(initialView) ? initialView : 'login', false);
    if (activeView === 'reset') {
        if (pendingResetEmail) $('#resetEmail').val(pendingResetEmail);
        updateControls();
        if (['failed', 'expired'].includes(resetStage) && pendingResetEmail) {
            showNotification('This reset request is no longer valid. Request a new code to continue.', 'error');
        }
    }
    if (activeView === 'login') $('#email').trigger('focus');
    checkPasswordMatch();
});
