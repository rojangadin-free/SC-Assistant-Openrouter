$(document).ready(function() {
    const loginForm = $('#loginForm');
    const signupForm = $('#signupForm');
    const formTitle = $('#formTitle');
    const formSubtitle = $('#formSubtitle');
    const toggleLink = $('#toggleLink');
    const themeToggle = $('#themeToggle');
    const body = $('body');
    const passwordToggle = $('#passwordToggle');
    const passwordInput = $('#password');
    const signupPasswordToggle = $('#signupPasswordToggle');
    const signupPasswordInput = $('#signupPassword');
    const customCheckbox = $('#customCheckbox');
    const rememberMeCheckbox = $('#rememberMe');
    const notification = $('#notification');
    
    // --- ADDED SECTION START: "Remember Me" Page Load Logic ---
    // Check for a saved email when the page loads
    const savedEmail = localStorage.getItem('rememberedEmail');
    if (savedEmail) {
        $('#email').val(savedEmail);
        rememberMeCheckbox.prop('checked', true);
        customCheckbox.addClass('checked');
    }
    // --- ADDED SECTION END ---

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
    
    // Check for saved theme preference or default to light
    const savedTheme = localStorage.getItem('theme') || 'light';
    setTheme(savedTheme);
    
    // Toggle theme on button click
    themeToggle.on('click', function() {
        const currentTheme = body.attr('data-theme') === 'dark' ? 'dark' : 'light';
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        setTheme(newTheme);
    });
    
    // Password visibility toggle for login
    passwordToggle.click(function() {
        const type = passwordInput.attr('type') === 'password' ? 'text' : 'password';
        passwordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });
    
    // Password visibility toggle for signup
    signupPasswordToggle.click(function() {
        const type = signupPasswordInput.attr('type') === 'password' ? 'text' : 'password';
        signupPasswordInput.attr('type', type);
        $(this).toggleClass('fa-eye fa-eye-slash');
    });
    
    // Custom checkbox functionality
    customCheckbox.click(function() {
        const isChecked = rememberMeCheckbox.prop('checked');
        rememberMeCheckbox.prop('checked', !isChecked);
        $(this).toggleClass('checked', !isChecked);
    });
    
    // Initialize checkbox state
    rememberMeCheckbox.change(function() {
        customCheckbox.toggleClass('checked', $(this).prop('checked'));
    });
    
    // Switch between login and signup using event delegation
    $(document).on('click', '#switchToSignup', function(e) {
        e.preventDefault();
        loginForm.hide();
        signupForm.show();
        formTitle.text("Create Account");
        formSubtitle.text("Sign up to start using Samar College Assistant");
        toggleLink.html('Already have an account? <a href="#" id="switchToLogin">Login here</a>');
    });
    
    $(document).on('click', '#switchToLogin', function(e) {
        e.preventDefault();
        signupForm.hide();
        loginForm.show();
        formTitle.text("Welcome Back");
        formSubtitle.text("Sign in to access Samar College Assistant");
        toggleLink.html('Don\'t have an account? <a href="#" id="switchToSignup">Sign up here</a>');
    });
    
    // Show notification function
    function showNotification(message, type = 'success') {
        const notificationTitle = type === 'success' ? 'Success' : 'Error';
        const notificationIcon = type === 'success' ? 'fa-check' : 'fa-exclamation-triangle';
        
        notification.removeClass('success error').addClass(type);
        notification.find('.notification-icon i').attr('class', `fas ${notificationIcon}`);
        notification.find('.notification-title').text(notificationTitle);
        notification.find('.notification-message').text(message);
        
        notification.addClass('show');
        
        const duration = type === 'error' ? 15000 : 3000;

        setTimeout(() => {
            notification.removeClass('show');
        }, duration);
    }
    
    // Handle login
    loginForm.on('submit', function(e) {
        e.preventDefault();
        
        // --- ADDED SECTION START: "Remember Me" Save Logic ---
        if (rememberMeCheckbox.is(':checked')) {
            // Save email to localStorage if checked
            localStorage.setItem('rememberedEmail', $('#email').val());
        } else {
            // Remove email from localStorage if not checked
            localStorage.removeItem('rememberedEmail');
        }
        // --- ADDED SECTION END ---

        const loginButton = $('#loginButton');
        loginButton.prop('disabled', true);
        loginButton.html('<span class="loading"></span>Signing In...');
        
        $.post("/login", loginForm.serialize(), function(response) {
            if (response.success) {
                showNotification("Login successful! Redirecting...", 'success');
                setTimeout(() => window.location.href = response.redirect, 1200);
            } else {
                loginButton.prop('disabled', false);
                loginButton.html('Sign In');
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            loginButton.prop('disabled', false);
            loginButton.html('Sign In');
            showNotification("Network error. Please try again.", 'error');
        });
    });
    
    // Handle signup
    signupForm.on('submit', function(e) {
        e.preventDefault();
        
        const signupButton = $('#signupButton');
        signupButton.prop('disabled', true);
        signupButton.html('<span class="loading"></span>Creating Account...');
        
        $.post("/signup", signupForm.serialize(), function(response) {
            if (response.success) {
                showNotification("Signup successful! Redirecting...", 'success');
                setTimeout(() => window.location.href = response.redirect, 1200);
            } else {
                signupButton.prop('disabled', false);
                signupButton.html('Sign Up');
                showNotification(response.message, 'error');
            }
        }).fail(function() {
            signupButton.prop('disabled', false);
            signupButton.html('Sign Up');
            showNotification("Network error. Please try again.", 'error');
        });
    });
    
    // Auto-focus first input
    $('#email').focus();
});