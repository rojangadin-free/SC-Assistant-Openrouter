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
  const sidebar = $('#sidebar'); 

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

  // ===== Sidebar Logic (Desktop Collapse / Mobile Close) =====
  const sidebarCollapseBtn = $('#sidebarCollapseBtn');
  const sidebarHeader = $('.sidebar-header'); // Target the whole header for click expanion
  
  // Helper to check if we are in mobile view
  function isMobile() {
    return window.innerWidth <= 768;
  }

  // Function to manage sidebar state based on screen size
  function updateSidebarState() {
    const shouldBeCollapsed = localStorage.getItem('sidebarCollapsed') === 'true';
    
    if (isMobile()) {
      // In mobile, sidebar NEVER uses 'collapsed' class (mini-sidebar).
      // It is either active (visible) or not.
      sidebar.removeClass('collapsed');
      
      // Update Button Icon to 'X' (Close)
      sidebarCollapseBtn.find('i')
        .removeClass('fa-compress')
        .addClass('fa-times');
        
    } else {
      // Desktop: Restore 'collapsed' state from storage
      if (shouldBeCollapsed) {
        sidebar.addClass('collapsed');
      } else {
        sidebar.removeClass('collapsed');
      }
      
      // Update Button Icon to Chevron
      sidebarCollapseBtn.find('i')
        .removeClass('fa-times')
        .addClass('fa-compress');
    }
  }

  // Initial Check
  updateSidebarState();

  // Re-check on resize
  $(window).on('resize', updateSidebarState);

  // Button Click Handler
  if (sidebarCollapseBtn.length) {
    sidebarCollapseBtn.on('click', function(e) {
      e.stopPropagation(); // Prevent bubbling to header click
      
      if (isMobile()) {
        // MOBILE: Close the sidebar completely
        sidebar.removeClass('active');
        body.removeClass('sidebar-open');
      } else {
        // DESKTOP: Toggle collapse state
        sidebar.toggleClass('collapsed');
        const isCollapsed = sidebar.hasClass('collapsed');
        localStorage.setItem('sidebarCollapsed', isCollapsed);
      }
    });
  }

  // Header Click Handler (For expanding only)
  // This allows clicking the logo area to expand the sidebar in Desktop Collapsed mode
  if (sidebarHeader.length) {
    sidebarHeader.on('click', function(e) {
      // Only trigger if desktop AND currently collapsed
      if (!isMobile() && sidebar.hasClass('collapsed')) {
        // Don't trigger if they clicked the button directly (though stopPropagation handles this)
        if ($(e.target).closest('.collapse-btn').length === 0) {
           sidebar.removeClass('collapsed');
           localStorage.setItem('sidebarCollapsed', 'false');
        }
      }
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

  // Settings navigation
  if(settingsButton.length) {
    settingsButton.on('click', function(e) {
      e.preventDefault();
      
      let origin = 'chat';
      if (window.location.pathname.includes('/dashboard')) {
        origin = 'dashboard';
      }
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
      const logoutUrl = logoutButton.attr('href');
      if(logoutUrl) {
        window.location.href = logoutUrl;
      } else {
        window.location.href = '/logout';
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

  // ===== Hamburger / Sidebar (Mobile Toggle) =====
  const hamburgerMenu = $('#hamburgerMenu');

  if (hamburgerMenu.length && sidebar.length) {
    hamburgerMenu.on('click', function(e) {
      e.stopPropagation();
      sidebar.toggleClass('active');
      body.toggleClass('sidebar-open');
    });

    $(document).on('click', function(e) {
      // Close sidebar if clicking outside on mobile
      if (isMobile() && sidebar.hasClass('active')) {
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
  }

  // ===== Notification Helper Function =====
  window.showNotification = function(message, type = 'success', details = null) {
    const notification = $('#notification');
    if (!notification.length) return;

    const isSuccess = type === 'success';

    notification.find('.notification-title')
      .text(isSuccess ? 'Success' : 'Error');
    
    notification.find('.notification-details').remove();

    notification.find('.notification-message')
      .text(message);
    
    notification.find('.notification-icon i')
      .attr('class', isSuccess ? 'fas fa-check' : 'fas fa-exclamation-triangle');

    if (details) {
      const detailsHtml = `<div class="notification-details" style="font-size: 0.8rem; color: var(--text-secondary); margin-top: 4px;">${details}</div>`;
      notification.find('.notification-content').append(detailsHtml);
    }

    notification
      .removeClass('success error')
      .addClass(type)
      .addClass('show');

    setTimeout(() => {
      notification.removeClass('show');
      setTimeout(() => {
        notification.find('.notification-details').remove();
      }, 300);
    }, 5000); 
  }
});