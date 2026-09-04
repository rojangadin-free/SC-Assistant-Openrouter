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

  // Dashboard navigation elements. The landing screen is called "Overview" in
  // the UI; its ids are all `analytics*` because it began as two screens — a
  // former "Overview" that showed the same satisfaction / ratings / unanswered
  // figures this one computes as KPIs — and the wiring kept the surviving name.
  const analyticsSection = $('#analyticsSection');
  const uploadSection = $('#uploadSection');
  const usersSection = $('#usersSection'); // New

  const uploadsMenuItem = $('#uploadsMenuItem');
  const usersMenuItem = $('#usersMenuItem'); // New



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
    if (sectionName === 'analytics') {
      // Delegated to the template, which owns the chart-building and the
      // first-open guard. Duplicating the section-switch here is what allowed
      // the landing screen to be shown without its data ever being loaded.
      if (window.showAnalytics) window.showAnalytics();
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
  
  // Menu item click handlers. #analyticsMenuItem is bound in the template, next
  // to the chart code it has to coordinate with.
  uploadsMenuItem.on('click', function() {

    showSection('uploads');
  });

  usersMenuItem.on('click', function() { // New
    showSection('users');
  });
  



  // ═══════════════════════════════════════════════════════════════
  //  WHERE YOU WERE
  //
  //  Every section lives in one HTML document and is switched by adding
  //  .active, so a reload always landed back on the default screen. That is
  //  fine on a page you visit; it is wrong on a page you WORK in. Uploading a
  //  document, reloading to check it indexed, and being thrown back to Overview
  //  each time is the loop this removes.
  //
  //  What is stored is the MENU ITEM ID, not a section name. Every one of the
  //  ten screens is opened by clicking a menu item, and each of those handlers
  //  already does the whole job — switch section, retitle the header, load the
  //  data, build charts on first reveal. Restoring by replaying that click
  //  reuses all of it. Storing "uploads" instead would mean a second mapping
  //  from name to behaviour, which is the duplication that let the old Overview
  //  be displayed without its data ever being fetched.
  // ═══════════════════════════════════════════════════════════════
  const LAST_SECTION_KEY = 'dashboardLastSection';

  // Private browsing and a full quota both make localStorage throw on write, and
  // a dashboard must not fail to navigate because it could not take a note.
  function rememberSection(menuItemId) {
    try { localStorage.setItem(LAST_SECTION_KEY, menuItemId); } catch (e) { /* ignore */ }
  }
  window.rememberSection = rememberSection;

  // Delegated, and keyed on the id suffix every section's menu item shares.
  // Binding to each item individually would mean editing this list every time a
  // screen is added — and the failure mode of forgetting is silent. It also
  // deliberately cannot match the "Open Chatbot" link: that is an <a
  // target="_blank"> with no MenuItem id, so it leaves the dashboard entirely
  // and is not a place to be restored to.
  $(document).on('click', '.menu-item[id$="MenuItem"]', function() {
    rememberSection(this.id);
  });

  // Replay the stored click. Returns false when there is nothing usable to
  // restore, so the caller can fall back to the landing screen.
  //
  // The id is checked against the DOM before it is used. localStorage outlives
  // deployments: a section that has been renamed or removed since the value was
  // written would otherwise hide every section and show none, and the admin
  // would be looking at an empty page with no way to tell why.
  window.restoreLastSection = function() {
    let saved = null;
    try { saved = localStorage.getItem(LAST_SECTION_KEY); } catch (e) { return false; }
    if (!saved || !/^[A-Za-z]+MenuItem$/.test(saved)) return false;

    const $item = $('#' + saved);
    if (!$item.length) return false;

    $item.trigger('click');

    // Trust the OUTCOME, not the attempt. If that id exists but its click
    // handler was never bound, the trigger does nothing at all and this must
    // report failure so the caller falls back to Overview.
    //
    // The obvious test — "is any .dashboard-section active?" — is useless here
    // and quietly wrong: #analyticsSection ships with class="... active" in the
    // markup, so it answers yes even when the trigger did nothing. That would
    // return true, skip the showAnalytics() fallback, and leave Overview
    // on screen with its loaders never run: header still "Dashboard", KPIs
    // still "—", canvases blank. Exactly the broken-looking landing screen the
    // fallback exists to prevent.
    //
    // So ask about the item that was clicked instead. Every section's handler
    // adds .active to its own menu item, and only the handler can have done it.
    return $item.hasClass('active');
  };


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
  
  // ===== Upload handler =====
  //
  // Two distinct stages, because they are two distinct things and collapsing
  // them is what made the old bar dishonest:
  //
  //   1. TRANSFER  — bytes to the server. Fast on a LAN. This is all the old
  //                  code measured, which is why it read 100% seconds into a
  //                  multi-minute job.
  //   2. INDEXING  — OCR, chunking, embedding, upsert. The slow part, now run in
  //                  a background thread and polled from /upload/status/<id>.
  //
  // Percentages and phase labels are computed server-side in rag/jobs.py. The
  // client only renders them, so the meaning of the bar lives in exactly one
  // place.
  const POLL_INTERVAL_MS = 1200;
  let pollTimer = null;

  function setStageLabel(text) {
    uploadProgress.find('h4').text(text);
  }

  function renderJob(job) {
    progressBar.css('width', job.percent + '%');
    progressText.text(job.percent + '%');

    const running = job.files.filter(f => f.status === 'running');
    if (job.status === 'running') {
      setStageLabel(running.length
        ? `Indexing — ${running[0].label}`
        : 'Indexing…');
    }

    // Per-file rows: with a multi-file upload the single aggregate bar cannot
    // say which file is slow or which one failed.
    const rows = job.files.map(f => {
      const cls = f.status === 'error' ? 'file-progress-error'
                : f.status === 'done'  ? 'file-progress-done'
                : '';
      const detail = f.error
        ? `<span class="file-progress-message">${f.error}</span>`
        : `<span class="file-progress-message">${f.label}${f.detail ? ' — ' + f.detail : ''}</span>`;
      return `
        <div class="file-progress-row ${cls}">
          <div class="file-progress-head">
            <span class="file-progress-name">${f.filename}</span>
            <span class="file-progress-pct">${f.percent}%</span>
          </div>
          <div class="progress-bar-container">
            <div class="progress-bar" style="width:${f.percent}%"></div>
          </div>
          ${detail}
        </div>`;
    }).join('');
    $('#fileProgressList').html(rows);
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function pollJob(jobId) {
    $.getJSON(`/upload/status/${jobId}`)
      .done(function(res) {
        if (!res.success || !res.job) {
          // The job record is gone but the indexing may well have finished, so
          // refresh before giving up. Returning without one is what left the
          // list stale until a manual reload.
          stopPolling();
          refreshFileList();
          return;
        }

        const job = res.job;
        renderJob(job);

        if (!job.done) {
          pollTimer = setTimeout(() => pollJob(jobId), POLL_INTERVAL_MS);
          return;
        }

        // Terminal. Only now is the file actually searchable, so only now does
        // the list get refreshed and the bar get dismissed.
        stopPolling();
        refreshFileList();

        const ok = job.files.filter(f => f.status === 'done');
        const bad = job.files.filter(f => f.status === 'error');

        if (bad.length && ok.length) {
          setStageLabel('Finished with errors');
          window.showNotification(
            `${ok.length} file(s) indexed, ${bad.length} failed. See details above.`,
            "error");
        } else if (bad.length) {
          setStageLabel('Failed');
          window.showNotification(job.error || "Indexing failed", "error");
        } else {
          setStageLabel('Done');
          const chunks = ok.reduce((n, f) => n + (f.chunks || 0), 0);
          window.showNotification(
            `${ok.length} file(s) indexed — ${chunks} passages added`, "success");
        }

        // Failures stay on screen: an error the admin never saw is an error that
        // gets repeated. Success clears itself.
        if (!bad.length) {
          setTimeout(() => { uploadProgress.hide(); $('#fileProgressList').empty(); }, 1500);
        }
      })
      .fail(function(xhr) {
        stopPolling();
        // 404 means the job is gone (pruned, or a restart wiped it) — terminal,
        // so stop rather than hammering the endpoint.
        const msg = xhr.status === 404
          ? "Lost track of this upload. Check the file list to see if it completed."
          : "Could not read indexing progress.";
        setStageLabel('Status unavailable');
        window.showNotification(msg, "error");
        // Losing the progress record says nothing about whether the file was
        // indexed, and telling the admin to check a list that was never
        // refreshed is not much of an instruction.
        refreshFileList();
      });

  }

  function handleFiles(files) {
    if (!files.length) return;

    stopPolling();

    let formData = new FormData();
    for (let f of files) formData.append("files[]", f);

    uploadProgress.show();
    progressBar.css('width', '0%');
    progressText.text('0%');
    $('#fileProgressList').empty();
    setStageLabel('Uploading…');

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
            // Deliberately capped at 5%: transfer is a small fraction of the
            // work, and the remaining 95% belongs to indexing. Letting bytes
            // drive the bar to 100% is the original bug.
            var pct = Math.round((evt.loaded / evt.total) * 5);
            progressBar.css('width', pct + '%');
            progressText.text(pct + '%');
          }
        }, false);
        return xhr;
      },
      success: function(response) {
        fileInput.val('');
        if (!response.job_id) {
          // No job id: nothing to poll, so do not pretend to track it.
          uploadProgress.hide();
          refreshFileList();
          window.showNotification(response.message || "Upload finished", "success");
          return;
        }
        setStageLabel('Indexing…');
        pollJob(response.job_id);
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
    // cache:false matters here. This is a plain GET with no cache headers, so the
    // browser is free to serve the response it already has — and the request that
    // matters most is the one fired the moment an upload finishes, when the cached
    // copy is precisely the list without the new file in it. That is the "I have to
    // reload the page to see it" symptom: the reload was a cache-buster.
    // jQuery appends a `_=<timestamp>` so every call goes to the server.
    $.ajax({
      url: "/files",
      cache: false,
      dataType: "json"
    }).done(function(response) {
      fileList.empty();
      if (!response.success || !response.files || response.files.length === 0) {
        uploadedFiles.hide();
        return;
      }
      uploadedFiles.show();
      // Newest first, so a file that was just indexed is at the top of the list
      // instead of somewhere in the middle of a DynamoDB scan's arbitrary order.
      response.files
        .slice()
        .sort((a, b) => (b.uploaded_at || '').localeCompare(a.uploaded_at || ''))
        .forEach(file => addFileToList(file));
    }).fail(function() {
      // Silence here would read as "no files uploaded", which is a different and
      // much more alarming message than "the list could not be loaded".
      window.showNotification("Could not refresh the file list.", "error");
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
  
  // ===== Charts =====
  // Deliberately none here.
  //
  // This file used to build two charts from hardcoded arrays — "Active Users"
  // rising through the week, a topic breakdown of Math/Science/History — against
  // canvas ids that no longer existed in the template. So they rendered nowhere,
  // and on the day someone added the missing canvas the dashboard would have
  // started reporting invented numbers as fact.
  //
  // Real charts live on the Overview screen and are drawn from
  // /admin/analytics/api/overview, on open rather than on page load: Chart.js
  // measures a `display:none` canvas as 0×0 and keeps that size forever.
  
  // ===== Triage queue + activity feed (both live on Overview) =====
  //
  // Only the queue and the feed are rendered here. The satisfaction / votes /
  // unanswered figures that /api/dashboard/attention also returns are NOT drawn
  // any more: the KPI row on the same screen computes those three from the same
  // stores, and two copies of one number on one screen is just a way to display
  // a contradiction. `health` is left in the response because the endpoint is
  // public API and the tests assert on it.


  // Rows are clickable: a panel that reports "3 students are waiting" and then
  // makes the admin find the right sidebar item is doing half a job. Section
  // names come from the API and are mapped to the existing menu items here.
  const ATTENTION_MENU = {
    escalations:  '#escalationsMenuItem',
    gaps:         '#gapsMenuItem',
    feedback:     '#feedbackMenuItem',
    conflicts:    '#conflictsMenuItem',
    calendar:     '#calendarMenuItem',
    announcements:'#announcementsMenuItem',
    reports:      '#reportsMenuItem',
    uploads:      '#uploadsMenuItem',
    users:        '#usersMenuItem'
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function loadAttention() {
    $.get('/api/dashboard/attention', function(res) {
      if (!res.success) { return; }

      const rows = res.attention || [];
      const waiting = rows.filter(r => r.count > 0);
      const $list = $('#attentionList').empty();

      if (!waiting.length) {
        // Worth saying out loud. A blank panel reads as "not loaded"; this reads
        // as "you are done", which is the whole point of the screen.
        $list.html(
          '<p class="sc-lead--sm" style="margin:0;">' +
          '<i class="fas fa-circle-check" style="color:var(--ui-success-ink);"></i> ' +
          'Nothing is waiting. No unanswered questions, no complaints, no one queued for a reply.' +
          '</p>');
      } else {
        waiting.forEach(function(r) {
          const target = ATTENTION_MENU[r.section];
          const colour = r.tone === 'bad' ? '#ef4444'
                       : r.tone === 'warn' ? '#f59e0b' : '#3b82f6';
          $list.append(
            '<div class="attention-row" data-target="' + escapeHtml(target || '') + '">' +
              '<span class="attention-count" style="color:' + colour + ';">' + r.count + '</span>' +
              '<span class="attention-label">' +
                '<i class="fas ' + escapeHtml(r.icon) + '"></i> ' + escapeHtml(r.label) +
              '</span>' +
              (target ? '<i class="fas fa-chevron-right attention-go"></i>' : '') +
            '</div>');
        });
      }

      // res.health is deliberately ignored. Satisfaction, vote count and
      // unanswered count are rendered once on this screen, by the KPI row above
      // this panel, from /admin/analytics/api/overview. Writing them here as
      // well gave two elements holding the same fact from two requests, which is
      // how a dashboard ends up disagreeing with itself mid-refresh.
    }).fail(function() {

      $('#attentionList').html(
        '<p class="sc-lead--sm" style="margin:0;color:var(--ui-danger-ink);">' +
        'Could not load the queue.</p>');
    });
  }

  $(document).on('click', '.attention-row', function() {
    const target = $(this).data('target');
    if (target) $(target).trigger('click');
  });

  $(document).on('click', '#refreshTriageBtn', function() { loadDashboardData(); });

  let triageLoadedOnce = false;

  function loadDashboardData() {
    triageLoadedOnce = true;
    loadAttention();


    $.get('/api/dashboard/activities?limit=10', function(response) {
      const activityList = $('.activity-list');
      activityList.empty();

      if (response.success && response.activities.length > 0) {
        if (response.window_days) {
          $('#activityWindowNote').text(
            'Newest first, from the last ' + response.window_days + ' days.');
        }
        response.activities.forEach(function(activity) {
          // `text` is built server-side and contains intentional <strong> for
          // the filename or person, so it is inserted as HTML. Everything
          // interpolated into it is escaped in rag/activity.py — the browser
          // must not be the only thing standing between a PDF filename and the
          // DOM.
          const tone = activity.tone === 'bad' ? ' activity-item--bad'
                     : activity.tone === 'warn' ? ' activity-item--warn'
                     : activity.tone === 'ok' ? ' activity-item--ok' : '';
          const clickable = ATTENTION_MENU[activity.section] ? ' activity-item--link' : '';
          activityList.append(
            '<div class="activity-item' + tone + clickable + '" ' +
                 'data-target="' + (ATTENTION_MENU[activity.section] || '') + '">' +
              '<div class="activity-icon"><i class="fas ' + escapeHtml(activity.icon) + '"></i></div>' +
              '<div class="activity-content">' +
                '<p>' + activity.text + '</p>' +
                '<span class="activity-time">' + escapeHtml(activity.time) + '</span>' +
              '</div>' +
            '</div>');
        });
      } else if (!response.success) {
        activityList.html('<p class="sc-lead--sm">Could not load activity.</p>');
      } else {
        activityList.html(
          '<p class="sc-lead--sm">Nothing has happened in the last ' +
          (response.window_days || 30) + ' days.</p>');
      }
    }).fail(function() {
      $('.activity-list').html('<p class="sc-lead--sm">Could not load activity.</p>');
    });
  }

  // An activity row opens the screen that deals with it, same as the queue above.
  $(document).on('click', '.activity-item--link', function() {
    const target = $(this).data('target');
    if (target) $(target).trigger('click');
  });

  // The template's showAnalytics() re-reads the queue every time the screen is
  // opened. Exposed rather than duplicated so there is one implementation.
  window.loadTriage = loadDashboardData;

  // ===== Initial load =====
  // Overview is the section shown on a first visit, so these panels are visible
  // immediately and must be populated without waiting for a click.
  //
  // The template's showAnalytics() already runs on load and calls loadTriage()
  // — calling it here too would fire every one of these requests twice on the
  // landing screen. So this is a fallback, not the normal path: deferred to the
  // next tick (jQuery ready handlers all run in this one) and skipped if the
  // template got there first. If that inline script ever fails to run, these
  // panels still fill instead of sitting on "Loading…" forever.
  setTimeout(function() {
    if (!triageLoadedOnce) loadDashboardData();
  }, 0);

  
  // Only refresh file list if upload section is active
  if (uploadSection.hasClass('active')) {
    refreshFileList();
  }
  
  // ===== Periodic data refresh =====
  // Only while the screen holding these panels is actually on display. Polling a
  // hidden section burns requests to update a DOM nobody is looking at — which
  // now matters more than it used to: a reload can land on any section, so this
  // is often false for the whole life of the page.
  setInterval(function() {
    if (analyticsSection.hasClass('active')) {
      loadDashboardData();
    }
  }, 60000); // Refresh every minute

});