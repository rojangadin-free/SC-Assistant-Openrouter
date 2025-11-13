/*
 * =========================================
 * DASHBOARD-SPECIFIC JAVASCRIPT (dashboard.js)
 *
 * This file only contains logic for the
 * /dashboard page. Shared logic is in main.js.
 * =========================================
 */

$(document).ready(function() {
  // Check if showNotification exists, if not, create a fallback
  if (typeof window.showNotification === 'undefined') {
    window.showNotification = function(message, type) {
      console.log(`Notification (${type}): ${message}`);
    };
  }

  // ===== Dashboard Elements =====
  const uploadArea = $('#uploadArea');
  const fileInput = $('#fileInput');
  const uploadedFiles = $('#uploadedFiles');
  const fileList = $('#fileList');
  const uploadProgress = $('#uploadProgress');
  const progressBar = $('#progressBar');
  const progressText = $('#progressText');

  // Dashboard navigation elements
  const overviewSection = $('#overviewSection');
  const uploadSection = $('#uploadSection');
  const usersSection = $('#usersSection'); // New
  
  const overviewMenuItem = $('#overviewMenuItem');
  const uploadsMenuItem = $('#uploadsMenuItem');
  const usersMenuItem = $('#usersMenuItem'); // New
  
  const backToOverview = $('#backToOverview');
  const backToOverviewFromUsers = $('#backToOverviewFromUsers'); // New

  // User list elements
  const userListLoader = $('#userListLoader');
  const userListBody = $('#userListBody');

  // Edit User Modal Elements
  const editUserModal = $('#editUserModal');
  const editUserForm = $('#editUserForm');
  const closeEditUserModal = $('#closeEditUserModal');
  const cancelEditUser = $('#cancelEditUser');
  const saveUserChangesButton = $('#saveUserChanges');


  // ===== Dashboard Navigation =====
  function showSection(sectionName) {
    // Hide all sections
    $('.dashboard-section').removeClass('active');
    
    // Remove active class from all menu items
    $('.menu-item').removeClass('active');
    
    // Show selected section and activate corresponding menu item
    if (sectionName === 'overview') {
      overviewSection.addClass('active');
      overviewMenuItem.addClass('active');
      // Update header title
      $('.header-left span').text('Dashboard');
    } else if (sectionName === 'uploads') {
      uploadSection.addClass('active');
      uploadsMenuItem.addClass('active');
      // Update header title
      $('.header-left span').text('File Management');
      // Refresh file list when showing uploads
      refreshFileList();
    } else if (sectionName === 'users') { // New
      usersSection.addClass('active');
      usersMenuItem.addClass('active');
      // Update header title
      $('.header-left span').text('User Management');
      // Load the user list
      loadUserList();
    }
  }
  
  // Menu item click handlers
  overviewMenuItem.on('click', function() {
    showSection('overview');
  });
  
  uploadsMenuItem.on('click', function() {
    showSection('uploads');
  });

  usersMenuItem.on('click', function() { // New
    showSection('users');
  });
  
  // Back button click handler
  backToOverview.on('click', function() {
    showSection('overview');
  });

  backToOverviewFromUsers.on('click', function() { // New
    showSection('overview');
  });
  
  // ===== Drag and drop =====
  uploadArea.on('dragover', e => { 
    e.preventDefault(); 
    uploadArea.addClass('dragover'); 
  });
  
  uploadArea.on('dragleave', () => uploadArea.removeClass('dragover'));
  
  uploadArea.on('drop', function(e) {
    e.preventDefault();
    uploadArea.removeClass('dragover');
    handleFiles(e.originalEvent.dataTransfer.files);
  });
  
  fileInput.on('change', function() { 
    handleFiles(this.files); 
  });
  
  // ===== Upload handler with progress bar =====
  function handleFiles(files) {
    if (!files.length) return;
    
    let formData = new FormData();
    for (let f of files) formData.append("files[]", f);
    
    // Show progress bar
    uploadProgress.show();
    progressBar.css('width', '0%');
    progressText.text('0%');
    
    $.ajax({
      url: "/upload", // Note: This URL is correct
      type: "POST",
      data: formData,
      processData: false,
      contentType: false,
      xhr: function() {
        var xhr = new window.XMLHttpRequest();
        xhr.upload.addEventListener("progress", function(evt) {
          if (evt.lengthComputable) {
            var percentComplete = Math.round((evt.loaded / evt.total) * 100);
            progressBar.css('width', percentComplete + '%');
            progressText.text(percentComplete + '%');
          }
        }, false);
        return xhr;
      },
      success: function(response) {
        progressBar.css('width', '100%');
        progressText.text('100%');
        setTimeout(() => {
            refreshFileList();
            window.showNotification(`${response.files.length} file(s) uploaded successfully`, "success");
            uploadProgress.hide();
        }, 500); // Small delay to show 100%
        fileInput.val(''); 
      },
      error: function(xhr) {
        let msg = xhr.responseJSON && xhr.responseJSON.message ? xhr.responseJSON.message : "Upload failed";
        window.showNotification(msg, "error");
        uploadProgress.hide();
        fileInput.val('');
      }
    });
  }
  
  // ===== Refresh file list from backend =====
  function refreshFileList() {
    $.get("/files", function(response) { // Note: This URL is correct
      fileList.empty();
      if (!response.success || !response.files || response.files.length === 0) {
        uploadedFiles.hide();
        return;
      }
      uploadedFiles.show();
      response.files.forEach(file => addFileToList(file));
    });
  }
  
  // ===== Add file to UI list with date =====
  function addFileToList(file) {
    const sizeKB = file.size ? ((file.size / 1024).toFixed(1) + " KB") : "";
    
    let formattedDate = 'N/A';
    if (file.uploaded_at) {
      formattedDate = new Date(file.uploaded_at).toLocaleDateString('en-US', {
        year: 'numeric', month: 'long', day: 'numeric'
      });
    }
    
    // ... (rest of file icon logic) ...
    let fileIcon = 'fa-file';
    if (file.name) {
      const extension = file.name.split('.').pop().toLowerCase();
      if (['pdf'].includes(extension)) fileIcon = 'fa-file-pdf';
      else if (['doc', 'docx'].includes(extension)) fileIcon = 'fa-file-word';
      else if (['xls', 'xlsx'].includes(extension)) fileIcon = 'fa-file-excel';
      else if (['ppt', 'pptx'].includes(extension)) fileIcon = 'fa-file-powerpoint';
      else if (['jpg', 'jpeg', 'png', 'gif'].includes(extension)) fileIcon = 'fa-file-image';
      else if (['zip', 'rar'].includes(extension)) fileIcon = 'fa-file-archive';
      else if (['txt', 'md'].includes(extension)) fileIcon = 'fa-file-alt';
    }
    
    const fileItem = $(`
      <div class="file-item">
        <div class="file-info">
          <div class="file-icon"><i class="fas ${fileIcon}"></i></div>
          <div class="file-details">
            <div class="file-name">${file.name}</div>
            <div class="file-meta">
              <span class="file-size">${sizeKB}</span>
              <span class="file-date">${formattedDate}</span>
            </div>
          </div>
        </div>
        <div class="file-actions">
          <button class="file-action view" title="View"><i class="fas fa-eye"></i></button>
          <button class="file-action download" title="Download"><i class="fas fa-download"></i></button>
          <button class="file-action delete" title="Delete"><i class="fas fa-trash"></i></button>
        </div>
      </div>
    `);
    
    // --- NEW: View file action ---
    fileItem.find('.view').on('click', function() {
      const icon = $(this).find('i');
      icon.removeClass('fa-eye').addClass('fa-spinner fa-spin');
      
      $.get(`/api/files/view-url/${file.name}`)
        .done(function(response) {
          if (response.success) {
            window.open(response.url, '_blank');
          } else {
            window.showNotification(response.message, "error");
          }
        })
        .fail(function() {
          window.showNotification("Failed to get viewable link.", "error");
        })
        .always(function() {
          icon.removeClass('fa-spinner fa-spin').addClass('fa-eye');
        });
    });
    
    // --- NEW: Download file action ---
    fileItem.find('.download').on('click', function() {
      const icon = $(this).find('i');
      icon.removeClass('fa-download').addClass('fa-spinner fa-spin');

      $.get(`/api/files/download-url/${file.name}`)
        .done(function(response) {
          if (response.success) {
            // This opens the URL. Because the URL has 'Content-Disposition: attachment',
            // the browser will automatically trigger a download dialog.
            window.open(response.url);
          } else {
            window.showNotification(response.message, "error");
          }
        })
        .fail(function() {
          window.showNotification("Failed to get download link.", "error");
        })
        .always(function() {
          icon.removeClass('fa-spinner fa-spin').addClass('fa-download');
        });
    });
    
    // Delete file action
    fileItem.find('.delete').on('click', function() {
      if (confirm(`Are you sure you want to delete ${file.name}? This will remove it from S3 and the knowledge base.`)) {
        $.ajax({
          url: `/delete/${file.name}`, // Note: This URL is correct
          type: "DELETE",
          success: function(res) {
            window.showNotification(res.message, "success");
            refreshFileList();
          },
          error: function(err) {
            window.showNotification("❌ Failed to delete file", "error");
          }
        });
      }
    });
    
    fileList.append(fileItem);
  }

  // ===== NEW User List Logic =====
  function loadUserList() {
    userListLoader.show();
    userListBody.empty();

    $.get("/api/dashboard/users", function(response) {
      userListLoader.hide();
      if (!response.success || !response.users || response.users.length === 0) {
        userListBody.html('<tr><td colspan="6" style="padding: 1rem; text-align: center; color: var(--text-secondary);">No users found.</td></tr>');
        return;
      }
      
      response.users.forEach(user => {
        const joinedDate = new Date(user.joined).toLocaleDateString('en-US', {
          year: 'numeric', month: 'short', day: 'numeric'
        });
        
        const statusClass = user.status === 'CONFIRMED' ? 'status-confirmed' : 'status-unconfirmed';
        const statusText = user.status.charAt(0) + user.status.slice(1).toLowerCase();
        
        const roleText = user.role.charAt(0).toUpperCase() + user.role.slice(1).toLowerCase();
        
        // ALL INLINE STYLES REMOVED
        const userRow = $(`
          <tr>
            <td>${user.username}</td>
            <td>${user.email}</td>
            <td>
              <span class="${statusClass}">
                ${statusText}
              </span>
            </td>
            <td>${roleText}</td>
            <td>${joinedDate}</td>
            <td>
              <button class="file-action edit-user" title="Edit" 
                data-id="${user.id}" 
                data-username="${user.username}" 
                data-email="${user.email}" 
                data-role="${user.role}">
                <i class="fas fa-pen"></i>
              </button>
              <button class="file-action delete-user" title="Delete" 
                data-id="${user.id}" 
                data-username="${user.username}">
                <i class="fas fa-trash"></i>
              </button>
            </td>
          </tr>
        `);

        userListBody.append(userRow);
      });
      
    }).fail(function() {
      userListLoader.hide();
      userListBody.html('<tr><td colspan="6" style="padding: 1rem; text-align: center; color: var(--error-color);">Failed to load users.</td></tr>');
    });
  }
  
  // --- Edit User Modal Open ---
  userListBody.on('click', '.edit-user', function() {
    const button = $(this);
    $('#editUserCognitoUsername').val(button.data('id'));
    $('#editUserUsername').val(button.data('username'));
    $('#editUserEmail').val(button.data('email'));
    $('#editUserRole').val(button.data('role'));
    editUserModal.addClass('active');
  });

  // --- Edit User Modal Close ---
  function closeEditModal() {
    editUserModal.removeClass('active');
    editUserForm[0].reset();
  }
  closeEditUserModal.on('click', closeEditModal);
  cancelEditUser.on('click', closeEditModal);
  editUserModal.on('click', function(e) {
    if ($(e.target).is('#editUserModal')) {
      closeEditModal();
    }
  });

  // --- Edit User Form Submit ---
  editUserForm.on('submit', function(e) {
    e.preventDefault();
    const originalText = saveUserChangesButton.text();
    saveUserChangesButton.prop('disabled', true).text('Saving...');

    $.ajax({
      url: '/api/dashboard/users/update',
      type: 'POST',
      data: editUserForm.serialize(),
      success: function(response) {
        if (response.success) {
          window.showNotification(response.message, 'success');
          closeEditModal();
          loadUserList(); // Refresh the list
          
          // --- THIS IS THE FIX ---
          // Check if the updated user was the current admin
          if (response.is_self_update) {
            // Update the sidebar text
            $('#userAccountButton span').first().text(response.new_username);
          }
          // --- END FIX ---

        } else {
          window.showNotification(response.message, 'error');
        }
      },
      error: function(xhr) {
        const errorMsg = xhr.responseJSON ? xhr.responseJSON.message : 'An error occurred';
        window.showNotification(errorMsg, 'error');
      },
      complete: function() {
        saveUserChangesButton.prop('disabled', false).text(originalText);
      }
    });
  });

  // --- Delete User Action ---
  userListBody.on('click', '.delete-user', function() {
    const userId = $(this).data('id');
    const username = $(this).data('username');

    if (confirm(`Are you sure you want to delete ${username}?\nThis will also delete all their conversations. This action cannot be undone.`)) {
      $.ajax({
        url: '/api/dashboard/users/delete',
        type: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({ cognito_username: userId }),
        success: function(response) {
          if (response.success) {
            window.showNotification(response.message, 'success');
            loadUserList(); // Refresh the list
          } else {
            window.showNotification(response.message, 'error');
          }
        },
        error: function(xhr) {
          const errorMsg = xhr.responseJSON ? xhr.responseJSON.message : 'An error occurred';
          window.showNotification(errorMsg, 'error');
        }
      });
    }
  });
  
  // ===== Initialize Charts =====
  function initializeCharts() {
    // User Activity Chart
    const userActivityCtx = document.getElementById('userActivityChart');
    if (userActivityCtx) {
      new Chart(userActivityCtx.getContext('2d'), {
        type: 'line',
        data: {
          labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
          datasets: [{
            label: 'Active Users',
            data: [320, 450, 380, 520, 480, 620, 580],
            borderColor: '#1a73e8',
            backgroundColor: 'rgba(26, 115, 232, 0.1)',
            tension: 0.4,
            fill: true
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true } }
        }
      });
    }
    
    // Popular Topics Chart
    const topicsCtx = document.getElementById('topicsChart');
    if (topicsCtx) {
      new Chart(topicsCtx.getContext('2d'), {
        type: 'doughnut',
        data: {
          labels: ['Math', 'Science', 'History', 'Literature', 'Other'],
          datasets: [{
            data: [30, 25, 15, 20, 10],
            backgroundColor: ['#1a73e8', '#34a853', '#fbbc05', '#ea4335', '#9e9e9e']
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'right' } }
        }
      });
    }
  }
  
  // ===== Dashboard data loading =====
  function loadDashboardData() {
    
    // --- FIX: Load statistics data ---
    $.get('/api/dashboard/stats', function(response) {
      if (response.success) {
        // Update statistics cards
        $('.stat-number').eq(0).text(response.data.users.toLocaleString());
        $('.stat-number').eq(1).text(response.data.conversations.toLocaleString());
        $('.stat-number').eq(2).text(response.data.documents.toLocaleString());
        
        // Note: Percentage changes are still static from the HTML.
      }
    }).fail(function() {
      // If API fails, show an error
      console.log('Could not load dashboard stats.');
      $('.stat-number').text('N/A');
    });
    
    // --- FIX: Load recent activities ---
    $.get('/api/dashboard/activities', function(response) {
      const activityList = $('.activity-list');
      activityList.empty(); // Clear any static content

      if (response.success && response.activities.length > 0) {
        response.activities.forEach(activity => {
          const activityItem = $(`
            <div class="activity-item">
              <div class="activity-icon">
                <i class="fas ${activity.icon}"></i>
              </div>
              <div class="activity-content">
                <p>${activity.description}</p>
                <span class="activity-time">${activity.time}</span>
              </div>
            </div>
          `);
          activityList.append(activityItem);
        });
      } else if (!response.success) {
        // If API fails, show an error
        console.log('Could not load recent activities.');
        activityList.html('<p>Could not load activities.</p>');
      } else {
        // If API succeeds but no activities
        activityList.html('<p>No recent activities found.</p>');
      }
    }).fail(function() {
      // If API fails, show an error
      console.log('Could not load recent activities.');
      $('.activity-list').html('<p>Could not load activities.</p>');
    });
  }
  
  // ===== Initial load =====
  initializeCharts();
  
  // **FIX: Re-enabled the function call**
  loadDashboardData();
  
  // Only refresh file list if upload section is active
  if (uploadSection.hasClass('active')) {
    refreshFileList();
  }
  
  // ===== Periodic data refresh =====
  // **FIX: Re-enabled the interval**
  setInterval(function() {
    // Only refresh dashboard data if overview is active
    if (overviewSection.hasClass('active')) {
      loadDashboardData();
    }
  }, 60000); // Refresh every minute
});