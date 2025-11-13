/*
 * =========================================
 * SETTINGS-SPECIFIC JAVASCRIPT (settings.js)
 *
 * This file only contains logic for the
 * /settings page. Shared logic is in main.js.
 * =========================================
 */

$(document).ready(function() {
  // Check if showNotification exists, if not, create a fallback
  if (typeof window.showNotification === 'undefined') {
    window.showNotification = function(message, type, details) {
      console.log(`Notification (${type}): ${message}`, details || '');
    };
  }
  
  const updateProfileForm = $('#updateProfileForm');
  const changePasswordForm = $('#changePasswordForm');

  const deleteAccountBtn = $('#deleteAccountBtn');
  const deleteAccountModal = $('#deleteAccountModal');
  const confirmDeleteBtn = $('#confirmDeleteBtn');
  const cancelDeleteBtn = $('#cancelDeleteBtn');

  // --- Error Message Helper ---
  function getErrorMessage(xhr, fallback) {
    try {
      const res = xhr.responseJSON;
      if (res) {
        if (res.error && res.error.message) return res.error.message;
        if (res.message) return res.message;
        if (res.code) return `AWS Error (${res.code}): ${res.message || 'Unknown AWS error'}`;
      }
    } catch (e) {
      console.error('Error parsing response:', e);
    }
    
    if (xhr.responseText) {
      try {
        const match = xhr.responseText.match(/<title>(.*?)<\/title>/i);
        if (match) return match[1];
      } catch (e) {}
    }
    
    console.error('Full error response:', xhr);
    return fallback || 'An unknown error occurred.';
  }

  // --- Update Profile ---
  updateProfileForm.on('submit', function(e) {
    e.preventDefault();
    
    const formData = {
      username: $('#username').val().trim()
    };
    
    if (!formData.username) {
      window.showNotification('Username is required', 'error');
      return;
    }
    if (formData.username.length < 3) {
      window.showNotification('Username must be at least 3 characters long', 'error');
      return;
    }
    
    const submitBtn = updateProfileForm.find('button[type="submit"]');
    const originalText = submitBtn.text();
    submitBtn.prop('disabled', true).text('Updating...');
    
    $.ajax({
      url: '/settings/update-profile', // Note: This URL is correct
      method: 'POST',
      data: formData,
      dataType: 'json', 
      beforeSend: function(xhr) {
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
      },
      success: function(response) {
        const type = response.success ? 'success' : 'error';
        const message = response.message || 'Profile update response received.';
        
        if (response.success) {
          const newUsername = $('#username').val();
          // Update username in sidebar
          const sidebarUsername = $('#userAccountButton span').first();
          if (sidebarUsername.length) {
            sidebarUsername.text(newUsername);
          }
          window.showNotification(message, 'success');
        } else {
          const details = response.details || response.error || null;
          window.showNotification(message, 'error', details);
        }
      },
      error: function(xhr) {
        const errorMsg = getErrorMessage(xhr, 'Failed to update profile. Please try again.');
        window.showNotification(errorMsg, 'error');
      },
      complete: function() {
        submitBtn.prop('disabled', false).text(originalText);
      }
    });
  });

  // --- Change Password ---
  changePasswordForm.on('submit', function(e) {
    e.preventDefault();
    
    const formData = {
      current_password: $('#current_password').val(),
      new_password: $('#new_password').val()
    };
    
    if (!formData.current_password || !formData.new_password) {
      window.showNotification('Please fill in all password fields', 'error');
      return;
    }
    if (formData.new_password.length < 6) {
      window.showNotification('New password must be at least 6 characters long', 'error');
      return;
    }
    
    const submitBtn = changePasswordForm.find('button[type="submit"]');
    const originalText = submitBtn.text();
    submitBtn.prop('disabled', true).text('Changing...');
    
    $.ajax({
      url: '/settings/change-password', // Note: This URL is correct
      method: 'POST',
      data: formData,
      success: function(response) {
        const type = response.success ? 'success' : 'error';
        const message = response.message || 'Password change response received.';
        
        window.showNotification(message, type);
        
        if (response.success) {
          changePasswordForm[0].reset();
        }
      },
      error: function(xhr) {
        const errorMsg = getErrorMessage(xhr, 'Failed to change password. Please try again.');
        window.showNotification(errorMsg, 'error');
      },
      complete: function() {
        submitBtn.prop('disabled', false).text(originalText);
      }
    });
  });

  // --- Delete Account Modal ---
  deleteAccountBtn.on('click', function() {
    deleteAccountModal.addClass('active');
  });

  cancelDeleteBtn.on('click', function() {
    deleteAccountModal.removeClass('active');
  });

  deleteAccountModal.on('click', function(e) {
    if ($(e.target).is('#deleteAccountModal')) {
      deleteAccountModal.removeClass('active');
    }
  });

  $(document).on('keydown', function(e) {
    if (e.key === 'Escape' && deleteAccountModal.hasClass('active')) {
      deleteAccountModal.removeClass('active');
    }
  });

  confirmDeleteBtn.on('click', function() {
    const originalText = confirmDeleteBtn.text();
    confirmDeleteBtn.prop('disabled', true).text('Deleting...');
    
    $.ajax({
      url: '/settings/delete-account', // Note: This URL is correct
      method: 'DELETE',
      success: function(response) {
        const type = response.success ? 'success' : 'error';
        const message = response.message || 'Account deletion request completed.';
        
        window.showNotification(message, type);
        
        if (response.success) {
          setTimeout(() => {
            window.location.href = '/auth'; // Go to auth page
          }, 2000);
        } else {
          deleteAccountModal.removeClass('active');
        }
      },
      error: function(xhr) {
        const errorMsg = getErrorMessage(xhr, 'Failed to delete account. Please try again.');
        window.showNotification(errorMsg, 'error');
        deleteAccountModal.removeClass('active');
      },
      complete: function() {
        confirmDeleteBtn.prop('disabled', false).text(originalText);
      }
    });
  });

});