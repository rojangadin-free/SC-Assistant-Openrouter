/*
 * =========================================
 * SHARED JAVASCRIPT (main.js)
 *
 * This file contains all the shared logic
 * for theme, user menu, sidebar, and notifications
 * that is used across chat.html, dashboard.html,
 * and settings.html.
 * =========================================
 */

$(document).ready(function() {
  const body = $('body');

  // ===== Theme Management =====
  const themeToggle = $('#themeToggle');
  function setTheme(theme) {
    if (theme === 'dark') {
      body.attr('data-theme', 'dark');
      if (themeToggle.length) {
        themeToggle.html('<i class="fas fa-sun"></i>');
      }
      localStorage.setItem('theme', 'dark');
    } else {
      body.removeAttr('data-theme');
      if (themeToggle.length) {
        themeToggle.html('<i class="fas fa-moon"></i>');
      }
      localStorage.setItem('theme', 'light');
    }
  }

  const savedTheme = localStorage.getItem('theme') || 'light';
  setTheme(savedTheme);

  if (themeToggle.length) {
    themeToggle.on('click', function() {
      const current = body.attr('data-theme') === 'dark' ? 'dark' : 'light';
      setTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // ===== Account Dropdown =====
  const userAccountButton = $('#userAccountButton');
  const userDropdown = $('#userDropdown');
  const profileButton = $('#profileButton');
  const settingsButton = $('#settingsButton');
  const logoutButton = $('#logoutButton');

  userAccountButton.on('click', function(e) {
    e.stopPropagation();
    const section = userAccountButton.closest('.user-account-section');
    const wasActive = section.hasClass('active');

    // Close all dropdowns
    $('.user-account-section').removeClass('active');
    $('.user-dropdown').removeClass('show');

    // Open the clicked one if it wasn't already active
    if (!wasActive) {
      section.addClass('active');
      userDropdown.addClass('show');
    }
  });

  // Close dropdown when clicking outside
  $(document).on('click', function(e) {
    if (!$(e.target).closest('.user-account-section').length) {
      $('.user-account-section').removeClass('active');
      $('.user-dropdown').removeClass('show');
    }
  });

  // Email row: just close dropdown
  if(profileButton.length) {
    profileButton.on('click', function(e) {
      e.preventDefault();
      $('.user-dropdown').removeClass('show');
      $('.user-account-section').removeClass('active');
    });
  }

  // Settings navigation - UPDATED logic for origin tracking
  if(settingsButton.length) {
    settingsButton.on('click', function(e) {
      e.preventDefault();
      
      // Determine origin based on current URL
      let origin = 'chat';
      if (window.location.pathname.includes('/dashboard')) {
        origin = 'dashboard';
      }
      
      // Navigate with origin parameter
      window.location.href = `/settings?origin=${origin}`;
    });
  }

  // ===== Logout Confirmation =====
  const confirmDialog = $('#confirmDialog');
  const cancelLogout = $('#cancelLogout');
  const confirmLogoutButton = $('#confirmLogoutButton');

  if (logoutButton.length) {
    logoutButton.on('click', function(e) {
      e.preventDefault();
      confirmDialog.addClass('active');
      $('.user-dropdown').removeClass('show');
      $('.user-account-section').removeClass('active');
    });
  }

  if (cancelLogout.length) {
    cancelLogout.on('click', () => confirmDialog.removeClass('active'));
  }

  if (confirmLogoutButton.length && logoutButton.length) {
    confirmLogoutButton.on('click', function() {
      const logoutUrl = logoutButton.attr('href'); // Get URL from the link
      if(logoutUrl) {
        window.location.href = logoutUrl;
      } else {
        window.location.href = '/logout'; // Fallback
      }
    });
  }

  if (confirmDialog.length) {
    confirmDialog.on('click', function(e) {
      if ($(e.target).is('.confirm-dialog')) {
        confirmDialog.removeClass('active');
      }
    });
  }

  // ===== Hamburger / Sidebar (Mobile) =====
  const hamburgerMenu = $('#hamburgerMenu');
  const sidebar = $('#sidebar');

  if (hamburgerMenu.length && sidebar.length) {
    hamburgerMenu.on('click', function(e) {
      e.stopPropagation();
      sidebar.toggleClass('active');
      body.toggleClass('sidebar-open');
    });

    $(document).on('click', function(e) {
      if (window.innerWidth <= 768 && sidebar.hasClass('active')) {
        const isClickInsideSidebar = sidebar[0].contains(e.target);
        const isHamburger = hamburgerMenu[0].contains(e.target);
        if (!isClickInsideSidebar && !isHamburger) {
          sidebar.removeClass('active');
          body.removeClass('sidebar-open');
        }
      }
    });

    $(document).on('keydown', function(e) {
      if (e.key === 'Escape' && sidebar.hasClass('active')) {
        sidebar.removeClass('active');
        body.removeClass('sidebar-open');
      }
    });

    $(window).on('resize', function() {
      if (window.innerWidth > 768) {
        sidebar.removeClass('active');
        body.removeClass('sidebar-open');
      }
    });
  }

  // ===== Notification Helper Function =====
  // Make it globally accessible for other scripts
  window.showNotification = function(message, type = 'success', details = null) {
    const notification = $('#notification');
    if (!notification.length) return;

    const isSuccess = type === 'success';

    notification.find('.notification-title')
      .text(isSuccess ? 'Success' : 'Error');
    
    // Remove old details
    notification.find('.notification-details').remove();

    notification.find('.notification-message')
      .text(message);
    
    notification.find('.notification-icon i')
      .attr('class', isSuccess ? 'fas fa-check' : 'fas fa-exclamation-triangle');

    // Add details if provided
    if (details) {
      const detailsHtml = `<div class="notification-details" style="font-size: 0.8rem; color: var(--text-secondary); margin-top: 4px;">${details}</div>`;
      notification.find('.notification-content').append(detailsHtml);
    }

    notification
      .removeClass('success error')
      .addClass(type)
      .addClass('show');

    // Auto-hide
    setTimeout(() => {
      notification.removeClass('show');
      // Remove details after hiding
      setTimeout(() => {
        notification.find('.notification-details').remove();
      }, 300);
    }, 5000); // 5 seconds
  }
});