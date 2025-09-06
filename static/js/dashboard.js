$(document).ready(function() {
  const body = $('body');
  const themeToggle = $('#themeToggle');
  const logoutButton = $('#logoutButton');
  const confirmDialog = $('#confirmDialog');
  const cancelLogout = $('#cancelLogout');
  const confirmLogoutButton = $('#confirmLogoutButton');
  const uploadArea = $('#uploadArea');
  const fileInput = $('#fileInput');
  const uploadedFiles = $('#uploadedFiles');
  const fileList = $('#fileList');
  const notification = $('#notification');
  const uploadProgress = $('#uploadProgress');
  const progressBar = $('#progressBar');
  const progressText = $('#progressText');
  
  // ===== Theme toggle =====
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
  setTheme(localStorage.getItem('theme') || 'light');
  themeToggle.on('click', () => setTheme(body.attr('data-theme') === 'dark' ? 'light' : 'dark'));
  
  // ===== Logout dialog =====
  logoutButton.on('click', () => confirmDialog.addClass('active'));
  cancelLogout.on('click', () => confirmDialog.removeClass('active'));
  confirmLogoutButton.on('click', () => window.location.href = '/logout');
  
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
      url: "/upload",
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
            showNotification(`${response.files.length} file(s) uploaded successfully`, "success");
            uploadProgress.hide();
        }, 500); // Small delay to show 100%
        fileInput.val(''); 
      },
      error: function(xhr) {
        let msg = xhr.responseJSON && xhr.responseJSON.message ? xhr.responseJSON.message : "Upload failed";
        showNotification("❌ " + msg, "error");
        uploadProgress.hide();
        fileInput.val('');
      }
    });
  }
  
  // ===== Refresh file list from backend =====
  function refreshFileList() {
    $.get("/files", function(response) {
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
    
    const fileItem = $(`
      <div class="file-item">
        <div class="file-info">
          <div class="file-icon"><i class="fas fa-file-pdf"></i></div>
          <div class="file-details">
            <div class="file-name">${file.name}</div>
            <div class="file-meta">
              <span class="file-size">${sizeKB}</span>
              <span class="file-date">${formattedDate}</span>
            </div>
          </div>
        </div>
        <div class="file-actions">
          <button class="file-action delete" title="Delete"><i class="fas fa-trash"></i></button>
        </div>
      </div>
    `);
    
    fileItem.find('.delete').on('click', function() {
      $.ajax({
        url: `/delete/${file.name}`,
        type: "DELETE",
        success: function(res) {
          showNotification(res.message, "success");
          refreshFileList();
        },
        error: function(err) {
          showNotification("❌ Failed to delete file", "error");
        }
      });
    });
    
    fileList.append(fileItem);
  }
  
  // ===== Notification =====
  function showNotification(message, type='success') {
    const title = type === 'success' ? 'Success' : 'Error';
    const icon = type === 'success' ? 'fa-check' : 'fa-exclamation-triangle';
    
    notification.removeClass('success error').addClass(type);
    notification.find('.notification-icon i').attr('class', `fas ${icon}`);
    notification.find('.notification-title').text(title);
    notification.find('.notification-message').text(message);
    notification.addClass('show');
    
    setTimeout(() => notification.removeClass('show'), 3000);
  }
  
  // ===== Initial load =====
  refreshFileList();
});