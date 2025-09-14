$(document).ready(function() {
    // DOM Elements
    const loginForm = $('#loginForm');
    const signupForm = $('#signupForm');
    const forgotPasswordForm = $('#forgotPasswordForm');
    const resetFlowForm = $('#resetFlowForm'); // The new merged form
    const formTitle = $('#formTitle');
    const formSubtitle = $('#formSubtitle');
    const toggleLink = $('#toggleLink');
    const themeToggle = $('#themeToggle');
    const body = $('body');
    const passwordToggle = $('#passwordToggle');
    const passwordInput = $('#password');
    const signupPasswordToggle = $('#signupPasswordToggle');
    const signupPasswordInput = $('#signupPassword');
    const newPasswordToggle = $('#newPasswordToggle');
    const newPasswordInput = $('#newPassword');
    const confirmPasswordToggle = $('#confirmPasswordToggle');
    const confirmPasswordInput = $('#confirmPassword');
    const customCheckbox = $('#customCheckbox');
    const rememberMeCheckbox = $('#rememberMe');
    const notification = $('#notification');
    const strengthBar = $('#strengthBar');
    const strengthText = $('#strengthText');
    const passwordMatch = $('#passwordMatch');
    const codeInputs = $('.code-input'); // Still used in the new form
    const resendCodeLink = $('#resendCodeLink');
    
    // Load remembered email
    const savedEmail = localStorage.getItem('rememberedEmail');
    if (savedEmail) {
        $('#email').val(savedEmail);
        rememberMeCheckbox.prop('checked', true);
        customCheckbox.addClass('checked');
    }
    
    // Theme toggle functionality
    function setTheme(theme) {
        if (theme === 'dark') {
            body.attr('data-theme', 'dark');
            themeToggle.html('<i class="fas fa-sun"></i>');
            localStorage.setItem('theme', 'dark');
        } else {
            body.removeAttr('data-theme');
            themeToggle.html('<i class="fas fa-moon"></i>');
            localStorage.setItem('theme', 'light');
        }
    }
    
    const savedTheme = localStorage.getItem('theme') || 'light';
    setTheme(savedTheme);
    
    themeToggle.on('click', function() {
        const currentTheme = body.attr('data-theme') === 'dark' ? 'dark' : 'light';
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        setTheme(newTheme);
    });
    
    // Password visibility toggles
    passwordToggle.click(function() {
        const type = passwordInput.attr('type') === 'password' ? 'text' : 'password';
        passwordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });
    
    signupPasswordToggle.click(function() {
        const type = signupPasswordInput.attr('type') === 'password' ? 'text' : 'password';
        signupPasswordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });

    newPasswordToggle.click(function() {
        const type = newPasswordInput.attr('type') === 'password' ? 'text' : 'password';
        newPasswordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });
    
    confirmPasswordToggle.click(function() {
        const type = confirmPasswordInput.attr('type') === 'password' ? 'text' : 'password';
        confirmPasswordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });
    
    // Custom checkbox
    customCheckbox.click(function() {
        const isChecked = rememberMeCheckbox.prop('checked');
        rememberMeCheckbox.prop('checked', !isChecked);
        $(this).toggleClass('checked', !isChecked);
    });
    
    rememberMeCheckbox.change(function() {
        customCheckbox.toggleClass('checked', $(this).prop('checked'));
    });
    
    // Switch between login and signup
    $(document).on('click', '#switchToSignup', function(e) {
        e.preventDefault();
        loginForm.hide();
        resetFlowForm.hide();
        signupForm.show();
        formTitle.text("Create Account");
        formSubtitle.text("Sign up to start using Samar College Assistant");
        toggleLink.html('Already have an account? <a href="#" id="switchToLogin">Login here</a>');
    });
    
    $(document).on('click', '#switchToLogin', function(e) {
        e.preventDefault();
        showLoginForm();
    });
    
    // Show forgot password form
    $(document).on('click', '#forgotPasswordLink', function(e) {
        e.preventDefault();
        loginForm.hide();
        signupForm.hide();
        resetFlowForm.hide();
        forgotPasswordForm.show();
        formTitle.text("Forgot Password");
        formSubtitle.text("We'll send you a code to reset your password");
        toggleLink.hide();
        
        const loginEmail = $('#email').val();
        if (loginEmail) {
            $('#resetEmail').val(loginEmail);
        }
    });

    // Back to login from any reset step
    $(document).on('click', '#backToLoginFromForgot, #backToLoginFromReset', function(e) {
        e.preventDefault();
        showLoginForm();
    });
    
    function showLoginForm() {
        forgotPasswordForm.hide();
        signupForm.hide();
        resetFlowForm.hide();
        loginForm.show();
        formTitle.text("Welcome Back");
        formSubtitle.text("Sign in to access Samar College Assistant");
        toggleLink.html('Don\'t have an account? <a href="#" id="switchToSignup">Sign up here</a>').show();
    }
    
    // Code input handling for the new form
    codeInputs.on('input', function() {
        const index = parseInt($(this).data('index'));
        const value = $(this).val();
        if (value.length === 1 && index < 5) {
            codeInputs.eq(index + 1).focus();
        }
    });
    
    codeInputs.on('keydown', function(e) {
        const index = parseInt($(this).data('index'));
        if (e.key === 'Backspace' && $(this).val() === '' && index > 0) codeInputs.eq(index - 1).focus();
        if (e.key === 'ArrowLeft' && index > 0) codeInputs.eq(index - 1).focus();
        if (e.key === 'ArrowRight' && index < 5) codeInputs.eq(index + 1).focus();
    });

    // Handle initial "Send Code" submission
    forgotPasswordForm.on('submit', function(e) {
        e.preventDefault();
        const sendCodeButton = $('#sendCodeButton');
        sendCodeButton.prop('disabled', true).html('<span class="loading"></span>Sending...');
        
        $.post("/forgot-password", forgotPasswordForm.serialize(), function(response) {
            sendCodeButton.prop('disabled', false).html('Send Code');
            if (response.success) {
                showNotification("Verification code sent to your email!", 'success');
                forgotPasswordForm.hide();
                resetFlowForm.show();
                formTitle.text("Reset Your Password");
                formSubtitle.text("Enter the code and your new password below");
                // Pass the email to the new combined form
                $('#resetFlowEmail').val($('#resetEmail').val());
                codeInputs.first().focus();
            } else {
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            sendCodeButton.prop('disabled', false).html('Send Code');
            showNotification("Network error. Please try again.", 'error');
        });
    });
    
    // Resend code
    resendCodeLink.on('click', function(e) {
        e.preventDefault();
        resendCodeLink.css('pointer-events', 'none').text('Sending...');
        // Use the email from the hidden field in the reset flow form
        $.post("/forgot-password", { email: $('#resetFlowEmail').val() }, function(response) {
            if (response.success) {
                showNotification("A new verification code has been sent.", 'success');
                codeInputs.val('');
                codeInputs.first().focus();
            } else {
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            showNotification("Network error. Please try again.", 'error');
        }).always(function() {
             setTimeout(() => {
                resendCodeLink.css('pointer-events', 'auto').text('Resend Code');
            }, 30000); // 30 seconds cooldown
        });
    });
    
    // Password strength/match checkers (no changes here)
    function checkPasswordStrength(password) {
        let strength = 0;
        const requirements = {
            length: password.length >= 8, uppercase: /[A-Z]/.test(password),
            lowercase: /[a-z]/.test(password), number: /[0-9]/.test(password),
            special: /[!@#$%^&*(),.?":{}|<>]/.test(password)
        };
        Object.keys(requirements).forEach(key => {
            const reqEl = $(`#${key}Req`); const iconEl = reqEl.find('i');
            if (requirements[key]) {
                reqEl.addClass('valid').removeClass('invalid');
                iconEl.removeClass('fa-circle').addClass('fa-check-circle');
                strength++;
            } else {
                reqEl.addClass('invalid').removeClass('valid');
                iconEl.removeClass('fa-check-circle').addClass('fa-circle');
            }
        });
        const percentage = (strength / 5) * 100;
        strengthBar.css('width', percentage + '%');
        if (strength < 2) {
            strengthBar.css('background', 'var(--error)');
            strengthText.text('Weak').css('color', 'var(--error)');
        } else if (strength < 4) {
            strengthBar.css('background', 'var(--warning)');
            strengthText.text('Medium').css('color', 'var(--warning)');
        } else {
            strengthBar.css('background', 'var(--success)');
            strengthText.text('Strong').css('color', 'var(--success)');
        }
        return strength;
    }
    
    function checkPasswordMatch() {
        const password = newPasswordInput.val(); const confirmPassword = confirmPasswordInput.val();
        if (confirmPassword.length > 0) {
            passwordMatch.show();
            if (password === confirmPassword) {
                passwordMatch.removeClass('invalid').addClass('valid');
                passwordMatch.find('i').removeClass('fa-times-circle').addClass('fa-check-circle');
                passwordMatch.find('span').text('Passwords match');
                return true;
            } else {
                passwordMatch.removeClass('valid').addClass('invalid');
                passwordMatch.find('i').removeClass('fa-check-circle').addClass('fa-times-circle');
                passwordMatch.find('span').text('Passwords do not match');
                return false;
            }
        } else { passwordMatch.hide(); return false; }
    }
    
    newPasswordInput.on('input', function() {
        checkPasswordStrength($(this).val());
        if (confirmPasswordInput.val()) checkPasswordMatch();
    });
    confirmPasswordInput.on('input', checkPasswordMatch);
    
    // Notification function (no changes here)
    let notificationTimer;
    function showNotification(message, type = 'success') {
        clearTimeout(notificationTimer);
        const notificationTitle = type.charAt(0).toUpperCase() + type.slice(1);
        const notificationIcon = type === 'success' ? 'fa-check' : 'fa-exclamation-triangle';
        notification.removeClass('success error').addClass(type);
        notification.find('.notification-icon i').attr('class', `fas ${notificationIcon}`);
        notification.find('.notification-title').text(notificationTitle);
        notification.find('.notification-message').text(message);
        notification.removeClass('show');
        setTimeout(() => { notification.addClass('show'); }, 10);
        const duration = type === 'error' ? 5000 : 3000;
        notificationTimer = setTimeout(() => { notification.removeClass('show'); }, duration);
    }
    
    // Handle login submission
    loginForm.on('submit', function(e) {
        e.preventDefault();
        if (rememberMeCheckbox.is(':checked')) {
            localStorage.setItem('rememberedEmail', $('#email').val());
        } else {
            localStorage.removeItem('rememberedEmail');
        }
        const loginButton = $('#loginButton');
        loginButton.prop('disabled', true).html('<span class="loading"></span>Signing In...');
        $.post("/login", loginForm.serialize(), function(response) {
            if (response.success) {
                showNotification("Login successful! Redirecting...", 'success');
                setTimeout(() => window.location.href = response.redirect, 1200);
            } else {
                loginButton.prop('disabled', false).html('Sign In');
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            loginButton.prop('disabled', false).html('Sign In');
            showNotification("Network error. Please try again.", 'error');
        });
    });
    
    // Handle signup submission
    signupForm.on('submit', function(e) {
        e.preventDefault();
        const signupButton = $('#signupButton');
        signupButton.prop('disabled', true).html('<span class="loading"></span>Creating Account...');
        $.post("/signup", signupForm.serialize(), function(response) {
            if (response.success) {
                showNotification("Signup successful! Redirecting...", 'success');
                setTimeout(() => window.location.href = response.redirect, 1200);
            } else {
                signupButton.prop('disabled', false).html('Sign Up');
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            signupButton.prop('disabled', false).html('Sign Up');
            showNotification("Network error. Please try again.", 'error');
        });
    });
    
    // Handle the NEW combined reset flow submission
    resetFlowForm.on('submit', function(e) {
        e.preventDefault();
        
        let code = '';
        codeInputs.each(function() { code += $(this).val(); });
        
        if (code.length < 6) {
            showNotification('Please enter the 6-digit verification code.', 'error');
            return;
        }

        if (checkPasswordStrength(newPasswordInput.val()) < 3) {
            showNotification('Please choose a stronger password.', 'error');
            return;
        }
        if (!checkPasswordMatch()) {
            showNotification('Passwords do not match.', 'error');
            return;
        }
        
        const resetButton = $('#resetButton');
        resetButton.prop('disabled', true).html('<span class="loading"></span>Resetting...');
        
        // Serialize the form and add the code to it
        const formData = resetFlowForm.serialize() + '&code=' + code;

        $.post("/reset-password", formData, function(response) {
            if (response.success) {
                showNotification("Password reset successful! You can now log in.", 'success');
                setTimeout(() => {
                    showLoginForm();
                }, 2000);
            } else {
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            showNotification("Network error. Please try again.", 'error');
        }).always(function() {
            resetButton.prop('disabled', false).html('Reset Password');
        });
    });
    
    $('#email').focus();
});