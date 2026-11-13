/*
 * =========================================
 * CHAT-SPECIFIC JAVASCRIPT (chat.js)
 * =========================================
 */

$(document).ready(function() {
  // --- MARKDOWN CONFIGURATION ---
  if (typeof marked !== 'undefined') {
    marked.setOptions({
      breaks: true, 
      gfm: true     
    });
  }

  if (typeof window.showNotification === 'undefined') {
    window.showNotification = function(message, type) {
      console.log(`Notification (${type}): ${message}`);
    };
  }

  const logoPath = '/static/images/logo.png';
  const messagesContainer = $('#messagesContainer');
  const messageInput = $('#messageInput');
  const sendButton = $('#sendButton');
  // The indicator is no longer a fixed element in the page, so it cannot be
  // cached: it is created inside each assistant bubble and removed with the
  // first token. These look it up on use, and are safe no-ops when nothing is
  // in flight — which is what the "clear any leftovers" callers below rely on.
  //
  // Only one request can be in flight (the send button is disabled for the
  // duration), so a fixed id is still unambiguous.
  const typingIndicator = () => $('#typingIndicator');

  // The dots and the caption, as they appear inside a bubble. Built here rather
  // than in chat.html so the bubble and the indicator that belongs to it are
  // created together; see the note where the old markup used to live.
  const thinkingBubble = () => `
    <div class="typing-indicator" id="typingIndicator">
      <div class="typing-dots"><span></span><span></span><span></span></div>
      <span class="typing-phase" id="typingPhase" aria-live="polite"></span>
    </div>`;

  // Captions come from the SSE `phase` events published by rag/progress.py, so
  // they are never ahead of the work: "Reading 34 pages" appears when the
  // cross-encoder starts, not on a timer. A missing indicator is not an error —
  // a phase event can arrive in the same read as the first chunk.
  function setTypingPhase(text) {
    $('#typingPhase').text(text || '');
  }

  // Faded, then removed. Removing it matters: it lives inside the answer bubble
  // now, so an indicator merely hidden would keep a blank flex row above the
  // text for the life of the message.
  function clearTypingIndicator() {
    const $ind = typingIndicator();
    if (!$ind.length) return;
    // The id goes first, so a request that finishes while this one is still
    // fading cannot find the outgoing node and re-caption it.
    $ind.removeAttr('id').addClass('fade-out');
    setTimeout(() => $ind.remove(), 220);
    messagesContainer.find('.message.is-thinking').removeClass('is-thinking');
  }



  const messageForm = $('#messageForm');
  const emptyChatState = $('#emptyChatState');
  const faqSuggestions = $('#faqSuggestions');
  let suggestionRequestId = 0;
  
  const conversationList = $("#conversationList");
  const conversationLoader = $('#conversationLoader');
  const conversationHistoryLoader = $('#conversationHistoryLoader');

  const imageInput = $('#imageInput');
  const uploadBtn = $('#uploadBtn');
  const previewContainer = $('#imagePreviewContainer');
  const previewImg = $('#imagePreview');
  const removeImageBtn = $('#removeImageBtn');

  let isNewConversation = true;
  let activeConversationId = null;
  let isHistoryLoading = false;

  function adjustViewportHeight() {
    const vh = window.innerHeight * 0.01;
    document.documentElement.style.setProperty('--vh', `${vh}px`);
  }

  function handleMobileKeyboard() {
    if (!window.visualViewport) return;
    const viewport = window.visualViewport;

    function viewportHandler() {
      const heightDifference = window.innerHeight - viewport.height;
      if (heightDifference > 150) {
        document.body.classList.add('keyboard-open');
      } else {
        document.body.classList.remove('keyboard-open');
      }
      setTimeout(() => {
        if(messagesContainer.length > 0) {
          messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
        }
      }, 100);
    }
    viewport.addEventListener('resize', viewportHandler);
  }

  function initMobileFixes() {
    adjustViewportHeight();
    handleMobileKeyboard();
  }

  window.addEventListener('resize', adjustViewportHeight);
  window.addEventListener('orientationchange', () =>
    setTimeout(adjustViewportHeight, 100)
  );

  initMobileFixes();

  uploadBtn.on('click', function() {
    imageInput.click();
  });

  imageInput.on('change', function() {
    if (this.files && this.files[0]) {
        const reader = new FileReader();
        reader.onload = function(e) {
            previewImg.attr('src', e.target.result);
            previewContainer.css('display', 'flex'); 
        }
        reader.readAsDataURL(this.files[0]);
    }
  });

  removeImageBtn.on('click', function() {
    imageInput.val('');
    previewContainer.hide();
  });

  function refreshSuggestions() {
    const requestId = ++suggestionRequestId;
    $.getJSON('/chat/api/suggestions').done(function(response) {
      if (requestId !== suggestionRequestId || !emptyChatState.is(':visible') ||
          String(messageInput.val() || '').trim() || faqSuggestions.find(':focus').length) return;
      if (!response || !response.success || !Array.isArray(response.suggestions)) return;
      const questions = [...new Set(response.suggestions.filter(question =>
        typeof question === 'string' && question.trim() && question.length <= 200
      ).map(question => question.trim()))].slice(0, 3);
      if (questions.length !== 3) return;
      faqSuggestions.empty();
      questions.forEach(question => {
        $('<button>', { type: 'button', class: 'faq-btn' }).text(question).appendTo(faqSuggestions);
      });
    });
  }

  faqSuggestions.on('click', '.faq-btn', function() {
    messageInput.val($(this).text()).trigger('input');
    messageForm.trigger('submit');
  });


  function renderCitations(rawText) {
    const placeholders = [];
    const uniqueSources = []; 

    function makeBadge(filename, rawPage, precedingText) {
      const cleanPage = (rawPage || '').replace(/\.0$/, '').trim();
      const file = filename.trim();
      
      let sourceIndex = uniqueSources.indexOf(file);
      if (sourceIndex === -1) {
        uniqueSources.push(file);
        sourceIndex = uniqueSources.length - 1;
      }
      const citationNumber = sourceIndex + 1;
      const displayText = `[${citationNumber}]`;
      const titleText = cleanPage ? `Click to view ${file}, page ${cleanPage}` : `Click to view ${file}`;
      
      let url = `/chat/document/${encodeURIComponent(file)}`;
      
      if (cleanPage && file.toLowerCase().endsWith('.pdf')) {
        let bestWord = "";
        
        if (precedingText) {
          const allWords = precedingText.split(/\s+/);
          const lastWords = allWords.slice(-8);
          
          for (let w of lastWords) {
            let cleanWord = w.replace(/[^\w-]/g, '');
            if (isNaN(cleanWord) && cleanWord.length > bestWord.length) {
              bestWord = cleanWord;
            }
          }
        }
        
        if (bestWord.length > 3) {
          url += `#page=${cleanPage}&search=${encodeURIComponent(bestWord)}`;
        } else {
          url += `#page=${cleanPage}`;
        }
      }
      
      const badge = (
        `<a href="${url}" target="_blank" ` +
        `data-source="${file}" ` +
        `title="${titleText}" ` +
        `style="background: transparent; border: none; padding: 0 2px; font-size: 0.85em; cursor: pointer; white-space: nowrap; text-decoration: none; color: #007bff; font-weight: bold;">` +
        `<sup>${displayText}</sup>` +
        `</a>`
      );
      
      const key = `CITATIONPLACEHOLDER${placeholders.length}END`;
      placeholders.push({ key, badge });
      return key;
    }

    const withPlaceholders = rawText.replace(
      /\[SOURCE:\s*([^\]|]+?)(?:\s*\|\s*([^\]]+))?\s*\]/gi,
      function(match, filename, rawPageStr, offset, fullString) {
        const precedingText = fullString.slice(0, offset);

        if (!rawPageStr || !rawPageStr.trim()) {
          return makeBadge(filename, '', precedingText);
        }

        const pageMatches = rawPageStr.match(/\d+(\.\d+)?/g);

        if (!pageMatches || pageMatches.length === 0) {
          return makeBadge(filename, '', precedingText);
        }

        return pageMatches.map(p => makeBadge(filename, p, precedingText)).join('');
      }
    );

    let html = marked.parse(withPlaceholders);

    placeholders.forEach(({ key, badge }) => {
      html = html.split(key).join(badge);
    });

    return html;
  }

  $(document).on('click', '.citation-badge', function() {
    const filename = $(this).data('source');
    if (!filename) return;

    const $badge = $(this);
    const originalHtml = $badge.html();

    $badge.html('<i class="fas fa-spinner fa-spin"></i>&nbsp;Opening…').css('pointer-events', 'none');

    $.get(`/api/files/view-url/${encodeURIComponent(filename)}`)
      .done(function(response) {
        if (response.success && response.url) {
          window.open(response.url, '_blank');
        } else {
          window.showNotification('Could not open document.', 'error');
        }
      })
      .fail(function() {
        window.showNotification('Failed to fetch document link.', 'error');
      })
      .always(function() {
        $badge.html(originalHtml).css('pointer-events', '');
      });
  });

  // Reporting opens options locally. Only choosing a reason submits a report.
  const canReport = $('.chat-section').attr('data-can-report') === 'true';

  function reportButton(msgId, pending = false) {
    return `
      <button type="button" class="report-btn" data-msg-id="${escapeHtml(msgId)}"
              title="Report this answer" aria-label="Report this answer" aria-expanded="false" ${pending ? 'disabled' : ''}>
        <i class="far fa-flag" aria-hidden="true"></i>
      </button>`;
  }

  // The question this answer replied to = the nearest preceding user message.
  // Reading it from the DOM keeps reporting on reloaded conversations working;
  // tracking it in a variable would only ever know about the current session.
  function questionFor($message) {
    const $prevUser = $message.prevAll('.message.user').first();
    return $prevUser.length ? $prevUser.find('.message-bubble').text().trim() : '';
  }

  // The rendered citation badges carry filename + page in their data/href, so the
  // sources can be recovered from the DOM instead of being threaded through the
  // streaming code and stored per message.
  //
  // Inline badges only appear when the model chose to cite mid-sentence, so they
  // cannot be the only source of provenance. The footer used to be the fallback;
  // now that it is gone, the citations are stashed on the message element instead
  // (see rememberCitations) and read back here so a report retains its sources.
  function sourcesFor($message) {
    const out = [];
    $message.find('.message-bubble a[data-source]').each(function() {
      const file = $(this).data('source');
      const href = $(this).attr('href') || '';
      const page = (href.match(/#page=(\d+)/) || [])[1];
      const label = page ? `${file}|p.${page}` : `${file}`;
      if (file && out.indexOf(label) === -1) out.push(label);
    });
    ($message.data('citations') || []).forEach(function(label) {
      if (label && out.indexOf(label) === -1) out.push(label);
    });
    return out;
  }


  // ── Where this answer came from ────────────────────────────────
  //
  // There used to be a "Based on <file>, pp. 6, 26, 43…" footer under every
  // answer. It is gone: the inline [n] badges already do the job better, because
  // they attribute one sentence rather than the whole reply, and the page list was
  // a line of noise nobody was going to act on — least of all on a phone, where it
  // ran off the edge of the screen.
  //
  // What is NOT gone is the provenance itself, which the report handler depends on.
  // The citations are kept on the message element as plain data, in the same
  // "file|p.N" shape the chips used to expose via data-label, so the payload sent
  // with a report is unchanged. Nothing is rendered.
  function rememberCitations($message, citations) {
    if (!citations || !citations.length) return;
    $message.data('citations', citations.map(function(c) {
      return String(c.label || c.source);
    }));
  }


  $(document).on('click', '.report-btn', function() {
    const $btn = $(this);
    if ($btn.prop('disabled')) return;
    const $message = $btn.closest('.message');
    const $sheet = $message.children('.reason-sheet');
    if ($sheet.length) closeReportSheet($sheet, true);
    else showReasonSheet($message, $btn.data('msg-id'));
  });


  // Reason chips submit directly; opening or closing these options files nothing.

  const REASONS = [
    'Wrong information',
    'Outdated / no longer true',
    'Incomplete answer',
    'Not related to my question',
    'Confusing wording',
  ];

  function showReasonSheet($message, msgId) {
    if ($message.find('.reason-sheet').length) return;
    const chips = REASONS.map(function(r) {
      return '<button type="button" class="reason-chip" data-reason="' + escapeHtml(r) + '">' +
              escapeHtml(r) + '</button>';
    }).join('');
    const $sheet = $(
      '<div class="reason-sheet" role="region" aria-label="Report options">' +
        '<div class="reason-sheet-head">' +
          '<span>Report this answer</span>' +
          '<button type="button" class="reason-dismiss" title="Close report options" aria-label="Close report options">&times;</button>' +
        '</div>' +
        (canReport
          ? '<p class="reason-help">Choose a reason to send a report to the administrator.</p>' +
            '<div class="reason-chips">' + chips +
              '<button type="button" class="reason-chip reason-other" data-reason="Others">Something else...</button>' +
            '</div>' +
            '<div class="reason-other-box" style="display:none;">' +
              '<input type="text" class="reason-other-text" maxlength="300" aria-label="Report details" ' +
                      'placeholder="Tell the admin what was wrong...">' +
              '<button type="button" class="reason-other-send">Send</button>' +
            '</div>'
          : '<p class="reason-help">Sign in to submit reports.</p>' +
            '<a class="report-sign-in">Sign in</a>') +
      '</div>'
    );
    $sheet.attr('id', 'report-reasons-' + msgId).data('msg-id', msgId).appendTo($message);
    $sheet.find('.report-sign-in').attr('href', $('#guestSignInButton').attr('href') || '/auth');
    $message.find('.report-btn').attr('aria-expanded', 'true').attr('aria-controls', $sheet.attr('id'));
    showEscalationOffer($message);
    $message.children('.escalate-offer').appendTo($sheet);
    $sheet[0].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function closeReportSheet($sheet, restoreFocus = false) {
    if ($sheet.data('reportPending')) return;
    const $button = $sheet.closest('.message').find('.report-btn');
    $sheet.remove();
    $button.attr('aria-expanded', 'false').removeAttr('aria-controls');
    if (restoreFocus) $button.trigger('focus');
  }

  function sendReason($sheet, reason, otherText) {
    if (!canReport || $sheet.data('reportPending')) return;
    const msgId = $sheet.data('msg-id');
    const $message = $sheet.closest('.message');
    const $button = $message.find('.report-btn');
    const focused = document.activeElement;
    $sheet.data('reportPending', true).attr('aria-busy', 'true');
    $sheet.find('button, input').prop('disabled', true);
    $button.prop('disabled', true);

    $.ajax({
      url: '/chat/report',
      method: 'POST',
      timeout: 15000,
      contentType: 'application/json',
      data: JSON.stringify({
        msg_id:      msgId,
        conv_id:     activeConversationId || null,
        reason:      reason,
        other_text:  otherText || '',
        msg_snippet: $message.find('.message-bubble').text().trim().substring(0, 500),
        question:    questionFor($message),
        sources:     sourcesFor($message),
      }),
      success: function(res) {
        if (!res || res.status !== 'ok') {
          showNotification((res && (res.message || res.error)) || 'Could not submit the report.', 'error');
          return;
        }
        $sheet.html('<div class="reason-thanks" role="status">' +
                       '<i class="fas fa-check-circle" aria-hidden="true"></i> Report sent to the administrator.' +
                     '</div>');
        setTimeout(function() {
          if ($sheet[0].isConnected) closeReportSheet($sheet);
        }, 2600);
      },
      error: function(xhr) {
        // The sheet stays open so the tap can simply be repeated.
        showNotification((xhr.responseJSON && (xhr.responseJSON.message || xhr.responseJSON.error))
          || 'Could not send that. Please try again.', 'error');
      },
      complete: function() {
        $sheet.data('reportPending', false).removeAttr('aria-busy');
        $sheet.find('button, input').prop('disabled', false);
        $button.prop('disabled', false);
        if (document.activeElement === document.body) {
          if (focused && focused.isConnected && $sheet[0].contains(focused)) $(focused).trigger('focus');
          else if ($button.is(':visible')) $button.trigger('focus');
        }
      }
    });

  }

  $(document).on('click', '.reason-chip', function() {
    const $chip = $(this);
    const $sheet = $chip.closest('.reason-sheet');
    if ($sheet.data('reportPending')) return;

    if ($chip.hasClass('reason-other')) {
      $sheet.find('.reason-chips').slideUp(120);
      $sheet.find('.reason-other-box').slideDown(120).find('.reason-other-text').focus();
      return;
    }
    sendReason($sheet, $chip.data('reason'), '');
  });

  $(document).on('click', '.reason-other-send', function() {
    const $sheet = $(this).closest('.reason-sheet');
    const text = $sheet.find('.reason-other-text').val().trim();
    if (!text) { $sheet.find('.reason-other-text').focus(); return; }
    sendReason($sheet, 'Others', text);
  });

  $(document).on('keydown', '.reason-other-text', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); $(this).siblings('.reason-other-send').click(); }
  });

  $(document).on('click', '.reason-dismiss', function() {
    closeReportSheet($(this).closest('.reason-sheet'), true);
  });
  $(document).on('keydown', '.reason-sheet, .report-btn', function(event) {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    const $sheet = $(this).hasClass('reason-sheet') ? $(this) : $(this).closest('.message').children('.reason-sheet');
    if ($sheet.length) closeReportSheet($sheet, true);
  });


  // ── Ask a human ────────────────────────────────────────────────
  //
  // rag/gaps.py already logs unanswered questions so the DOCUMENTS get fixed.
  // That does nothing for the student staring at the screen today. This offers
  // the other half: route the question to a real office and get a reply.
  function showEscalationOffer($message) {
    if ($message.find('.escalate-offer').length) return;

    const question = questionFor($message);
    if (!question) return;

    // Dismissable. This is an unsolicited prompt attached to an answer the
    // student already judged, so it has to be possible to decline it — otherwise
    // the only ways to clear it are to use it or to leave the conversation, and a
    // suggestion you cannot say no to reads as nagging.
    const offer = $(`
      <div class="escalate-offer">
        <span><i class="fas fa-user-tie"></i> Want a person to answer this?</span>
        <button type="button" class="escalate-open-btn">Ask a human</button>
        <button type="button" class="escalate-dismiss" title="No thanks" aria-label="Dismiss staff help offer">&times;</button>
      </div>
    `);

    offer.data('question', question);
    $message.append(offer);
  }

  // Dismissing the human-help offer does not submit or retract a report.

  $(document).on('click', '.escalate-dismiss', function() {
    $(this).closest('.escalate-offer').fadeOut(150, function() { $(this).remove(); });
  });

  let escalateQuestion = '';
  let escalateAnswer = '';


  $(document).on('click', '.escalate-open-btn', function() {

    const $offer = $(this).closest('.escalate-offer');
    const $message = $(this).closest('.message');
    escalateQuestion = $offer.data('question') || questionFor($message);
    escalateAnswer = $message.find('.message-bubble').text().trim().substring(0, 300);

    $('#escalateQuestionPreview').text('"' + escalateQuestion.substring(0, 160) + '"');
    $('#escalateNote').val('');
    $('#escalateContact').val('');
    $('#escalateModal').fadeIn(150);
  });

  // The office list comes from the server (rag/escalation.py ROUTES) so a new

  // office is added in one place and the dropdown can never offer a value the
  // API would reject.
  $.getJSON('/chat/escalate/routes')
    .done(function(res) {
      if (!res || !res.success) return;
      const $sel = $('#escalateRoute').empty();
      Object.keys(res.routes).forEach(function(key) {
        $sel.append(`<option value="${key}">${res.routes[key]}</option>`);
      });
    });

  $('#escalateCancelBtn').on('click', function() {
    $('#escalateModal').fadeOut(150);
  });

  $('#escalateModal').on('click', function(e) {
    if ($(e.target).is('#escalateModal')) $('#escalateModal').fadeOut(150);
  });

  $('#escalateSubmitBtn').on('click', function() {
    const $btn = $(this);
    $btn.prop('disabled', true).text('Sending…');

    $.ajax({
      url: '/chat/escalate',
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({
        question: escalateQuestion,
        answer:   escalateAnswer,
        route:    $('#escalateRoute').val(),
        contact:  $('#escalateContact').val().trim(),
        note:     $('#escalateNote').val().trim(),
        conv_id:  activeConversationId || '',
      }),
      success: function(res) {
        $('#escalateModal').fadeOut(150);
        showNotification(res.message || 'Sent. Someone will reply soon.', 'success');
        $('.escalate-offer').remove();
        // Reveal the inbox immediately. The student has just been told a reply is
        // coming, so this is the one moment where pointing at *where* it will
        // arrive costs nothing and prevents them from hunting for it later.
        loadReplies();
      },
      error: function(xhr) {
        const msg = (xhr.responseJSON && xhr.responseJSON.message)
          || 'Could not send your question.';
        showNotification(msg, 'error');
      },
      complete: function() {
        $btn.prop('disabled', false).text('Send to office');
      }
    });
  });


  // ── Replies inbox ──────────────────────────────────────────────
  //
  // "Ask a human" ends with a promise: an office will reply. Until now the reply
  // was stored where only the admin could see it, so the app made a promise it
  // could not keep — the student had to watch their email and hope. This is the
  // other end of that pipe.
  //
  // Polled rather than pushed: replies are written by a human minutes-to-days
  // later, so a socket would idle for hours to save a request that costs almost
  // nothing. The poll is slow (2 min) for the same reason.
  const REPLIES_POLL_MS = 120000;

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Absolute dates ("Aug 20") rather than "3 days ago": a student comparing the
  // reply against an office deadline needs the date, not the distance.
  function shortDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function renderReplies(items) {
    const $list = $('#repliesList').empty();

    if (!items.length) {
      $list.append(
        '<div class="replies-empty">' +
          "No questions sent to an office yet.<br>" +
          'When an answer is missing, use <strong>Ask a human</strong> and the reply will appear here.' +
        '</div>'
      );
      return;
    }

    items.forEach(function(it) {
      const answered = it.status === 'answered' && it.reply;
      // A pending row is still shown. Silence is the thing students actually
      // complain about, and "sent, waiting" is information — it proves the
      // question was not lost, which is most of what they want to know.
      const body = answered
        ? '<div class="reply-body">' + escapeHtml(it.reply) + '</div>'
        : '<div class="reply-waiting">Waiting for a reply from the office…</div>';

      $list.append(
        '<div class="reply-item' + (it.unread ? ' unread' : '') + '" data-id="' + escapeHtml(it.id) + '">' +
          '<div class="reply-q">' + escapeHtml(it.question) + '</div>' +
          '<div class="reply-meta">' +
            '<span class="reply-status ' + (answered ? 'answered' : 'pending') + '">' +
              (answered ? 'Answered' : 'Waiting') +
            '</span>' +
            '<span>' + escapeHtml(it.route_label) + '</span>' +
            '<span>' + shortDate(it.answered_at || it.created_at) + '</span>' +
          '</div>' +
          body +
        '</div>'
      );
    });
  }

  function loadReplies() {
    // Fails silently. The inbox is secondary to the conversation on screen, and
    // an error toast for a background poll would be noise the student cannot act
    // on.
    $.getJSON('/chat/escalations/mine')
      .done(function(res) {
        if (!res || !res.success) return;
        const items = res.items || [];
        const unread = res.unread || 0;

        // The button appears only once there is something to open, so the header
        // stays clean for the majority of students who never escalate.
        $('#repliesToggle').toggle(items.length > 0);
        $('#repliesBadge').text(unread > 9 ? '9+' : unread).toggle(unread > 0);
        renderReplies(items);
      });
  }

  $(document).on('click', '#repliesToggle', function(e) {
    e.stopPropagation();
    const $panel = $('#repliesPanel');
    if ($panel.is(':visible')) { $panel.fadeOut(120); return; }
    // Refresh on open: the list may be two minutes stale, and the one moment the
    // student is looking is the wrong moment to show old data.
    loadReplies();
    $panel.fadeIn(120);
  });

  $(document).on('click', '#repliesCloseBtn', function() {
    $('#repliesPanel').fadeOut(120);
  });

  // Click-away close. The panel overlays the conversation, so leaving it open
  // when attention moved back to the chat would hide the thing being read.
  $(document).on('click', function(e) {
    if (!$(e.target).closest('#repliesPanel, #repliesToggle').length) {
      $('#repliesPanel').hide();
    }
  });

  // Reading the answer is the acknowledgement — an explicit "mark as read" button
  // would be a second click for something the student has already done.
  $(document).on('click', '.reply-item.unread', function() {
    const $item = $(this);
    const id = $item.data('id');
    if (!id) return;
    $.post('/chat/escalations/' + encodeURIComponent(id) + '/read')
      .done(function() {
        $item.removeClass('unread');
        const left = $('.reply-item.unread').length;
        $('#repliesBadge').text(left > 9 ? '9+' : left).toggle(left > 0);
      });
  });

  loadReplies();
  setInterval(loadReplies, REPLIES_POLL_MS);

  // The installed app's "My replies" shortcut (see the manifest in
  // sc_assistant/pwa.py) launches /chat?replies=1. Without this the shortcut
  // would land the student on an ordinary chat screen with the inbox still
  // closed — an advertised entry point that silently does nothing.
  if (new URLSearchParams(window.location.search).get('replies') === '1') {
    loadReplies();
    $('#repliesPanel').fadeIn(120);
    // Drop the parameter so a refresh, or restoring the app from the task
    // switcher, does not keep re-opening the panel over the conversation.
    history.replaceState({}, '', window.location.pathname);
  }



  // `savedMsgId`/`citations` are supplied when replaying a stored conversation.
  // The id has to be the one the server saved the answer under, otherwise the
  // rebuilt thumbs are keyed to a fresh random id and the vote the student cast
  // yesterday can never be found again.
  function addMessage(content, isUser = false, autoScroll = true, hasImage = false,
                      savedMsgId = null, citations = null) {

    emptyChatState.hide();


    let processedContent = '';
    
    if (isUser) {
       processedContent = content.replace(/</g, '&lt;').replace(/>/g, '&gt;');
       if (hasImage) {
           processedContent += ' <br><span style="font-size:0.85em; color:var(--text-secondary); display:inline-flex; align-items:center; margin-top:5px;"><i class="fas fa-paperclip" style="margin-right:4px;"></i> Image Attached</span>';
       }
    } else {
       processedContent = renderCitations(content);
    }

    const userAvatarUrl = $('body').data('avatar');

    const avatar = isUser
      ? `<div class="avatar"><img src="${userAvatarUrl}" class="profile-avatar-chat" alt="User"></div>`
      : `<div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>`;

    const msgId = savedMsgId || ('msg-' + Date.now() + '-' + Math.floor(Math.random() * 9999));

    const reportRow = !isUser
      ? `<div class="message-report-row">
           ${reportButton(msgId)}
         </div>`
      : '';


    const html = `
      <div class="message ${isUser ? 'user' : 'assistant'}" id="${msgId}">
        <div class="message-row">
          ${isUser ? '' : avatar}
          <div class="message-bubble">${processedContent}</div>
          ${isUser ? avatar : ''}
        </div>
        ${reportRow}
      </div>
    `;

    messagesContainer.append(html);

    // A replayed answer keeps its provenance too, so reporting an old answer in a
    // reopened conversation still reports which file it came from.
    if (!isUser && citations && citations.length) {
      rememberCitations($('#' + msgId), citations);
    }

    if (autoScroll && messagesContainer.length > 0) {
      messagesContainer.stop().animate(
        { scrollTop: messagesContainer[0].scrollHeight },
        300
      );
    }

    return msgId;
  }


  function renderChatHistory(history) {
    messagesContainer.find('.message').remove(); 
    
    if (!Array.isArray(history) || history.length === 0) {
      emptyChatState.show();
      refreshSuggestions();
      return;
    }
    
    emptyChatState.hide();

    history.forEach(msg => {
      if (msg.role === 'system') return;
      
      let content = msg.content;
      const hasImageTag = content.includes('[Image Uploaded]');
      content = content.replace(' [Image Uploaded]', '');
      
      const isUser = msg.role === 'user';
      addMessage(
        content, isUser, false, hasImageTag,
        // Older conversations were saved before ids/citations were persisted, so
        // both are optional; reports still include the displayed question/answer.
        isUser ? null : (msg.msg_id || null),
        isUser ? null : (msg.citations || null)
      );
    });

    if (messagesContainer.length > 0) {
      messagesContainer.stop().animate(
        { scrollTop: messagesContainer[0].scrollHeight },
        300
      );
    }
  }


  function applyActiveHighlight() {
    $('.conversation-item').removeClass('active-conversation');
    if (activeConversationId) {
      $(
        `.conversation-item[data-id="${activeConversationId}"]`
      ).addClass('active-conversation');
    }
  }

  function addConversationToSidebar(convId, title) {
    const noChats = conversationList.find(
      ".conversation-item:contains('No past chats')"
    );
    if (noChats.length) noChats.remove();

    const safeTitle = (title || 'Untitled Chat')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    const item = $(`
      <div class="conversation-item" data-id="${convId}">
        <div class="conv-main">
          <i class="fas fa-comment-alt"></i>
          <span>${safeTitle}</span>
        </div>
        <div class="conv-actions">
          <button class="delete-btn" title="Delete">
            <i class="fas fa-trash"></i>
          </button>
        </div>
      </div>
    `);

    conversationList.prepend(item);
    applyActiveHighlight();
  }

  // 🚀 Fetch Readable Stream Implementation
  async function sendMessage(message, imageFile) {
    const requestIsNew = isNewConversation;
    const requestConvId = activeConversationId;

    const assistantMessageId = 'msg-' + Date.now() + '-' + Math.floor(Math.random() * 9999);

    // The bubble is created with the dots already in it. There is no caption to
    // clear first, which the old shared indicator needed: a fresh element per
    // request cannot inherit the page count from the previous answer.
    //
    // `is-thinking` marks it as not-yet-an-answer, and is what hides the empty
    // streaming div and the report action until there is something to read.
    const htmlPlaceholder = `
      <div class="message assistant is-thinking" id="${assistantMessageId}">
        <div class="message-row">
          <div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>
          <div class="message-bubble">
            ${thinkingBubble()}
            <div class="streaming-text"></div>
          </div>
        </div>
        <div class="message-report-row">
           ${reportButton(assistantMessageId, true)}
         </div>
      </div>
    `;

    // Appended NOW, before the request goes out — not after the response headers
    // come back as it used to be. The dots are the acknowledgement that the
    // question was sent, so they have to appear on the keypress; waiting for the
    // server means the slowest questions, the ones that most need a "working on
    // it", are the ones that sit with no feedback at all.
    messagesContainer.append(htmlPlaceholder);
    messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;

    const formData = new FormData();
    formData.append("msg", message);
    if (imageFile) {
        formData.append("image", imageFile);
    }


    let activeReader = null;
    let streamCompleted = false;
    try {
        const response = await fetch('/chat/get', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const failure = await response.json().catch(() => ({}));
            throw new Error(failure.error || "The assistant is temporarily unavailable. Please retry.");
        }

        // The bubble is already on screen — appended before the request went out —
        // so this only has to find where to write.
        const assistantBubble = $(`#${assistantMessageId}`).find('.streaming-text');

        const reader = response.body.getReader();
        activeReader = reader;
        const decoder = new TextDecoder();
        let fullAnswerText = "";
        let isFirstToken = true;
        // Whatever arrived without its terminating newline yet. See below.
        let sseBuffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            // A read() returns whatever bytes happen to have arrived — NOT one
            // SSE event. Several events can land in one read, and a single event
            // can be cut in half across two.
            //
            // This used to split each read on '\n' and JSON.parse() anything
            // beginning with 'data: ', so half a line ("data: {\"type\": \"ph")
            // threw a SyntaxError out of this loop and took the rest of the
            // stream with it. On localhost every yield arrives in its own read,
            // so it never happened; through a proxy, where writes are coalesced
            // and re-chunked, it is routine — which is why the deployed app lost
            // its captions.
            //
            // So: accumulate, process only COMPLETE lines, and keep the partial
            // remainder for the next read.
            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split('\n');
            sseBuffer = lines.pop();

            for (const line of lines) {
                // Comment lines (': keep-alive') are heartbeats, not events.
                if (line.startsWith('data: ')) {
                    let data;
                    try {
                        data = JSON.parse(line.substring(6));
                    } catch (parseErr) {
                        // One malformed event must not end the answer.
                        console.warn('Skipped an unreadable stream event:', parseErr);
                        continue;
                    }


                    // What the pipeline is doing right now. Published from the
                    // real phase boundaries (rag/progress.py), so the caption is
                    // never ahead of the work and the page count in "Reading 34
                    // pages" is the pool retrieval actually returned.
                    //
                    // Handled before 'chunk' because it is the only event that
                    // arrives while the student is still waiting.
                    if (data.type === 'phase') {
                        if (isFirstToken) setTypingPhase(data.label);
                        continue;
                    }

                    if (data.type === 'chunk') {
                        if (isFirstToken) {
                            // Removed, not hidden. The dots share the bubble with
                            // the answer now, so leaving the node behind would keep
                            // a blank row above the text — and a caption fading out
                            // over the first words reads as a second answer.
                            clearTypingIndicator();
                            isFirstToken = false;
                        }

                        fullAnswerText += data.text;
                        assistantBubble.html(renderCitations(fullAnswerText));
                        messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
                    } 
                    else if (data.type === 'done') {
                        streamCompleted = true;
                        // Sources arrive with 'done', not with the chunks: they
                        // describe the finished answer.
                        rememberCitations($('#' + assistantMessageId), data.citations);

                        // Re-key the bubble to the id the answer was SAVED under.
                        // The placeholder id was invented locally so streaming had
                        // somewhere to write; if it stays, a report is filed
                        // under an id that exists nowhere in the stored history and
                        // vanishes on the next reload.
                        const $live = $('#' + assistantMessageId);
                        if (data.msg_id) {
                            $live.attr('id', data.msg_id);
                            $live.find('.report-btn').attr('data-msg-id', data.msg_id)
                                                    .data('msg-id', data.msg_id);
                        }
                        $live.find('.report-btn').prop('disabled', false);


                        const responseConvId = data.new_conversation_created ? data.conv_id : requestConvId;
                        const isStillInNewChat = requestIsNew && activeConversationId === null;
                        const isStillInSameChat = activeConversationId === responseConvId;


                        if (isStillInNewChat || isStillInSameChat) {
                            if (data.new_conversation_created) {
                                activeConversationId = data.conv_id;
                                isNewConversation = false;
                                localStorage.setItem('activeConversationId', activeConversationId);
                                addConversationToSidebar(data.conv_id, data.new_conv_title);
                            }
                        }
                    } 
                    else if (data.type === 'error') {
                        throw new Error(data.text || "The answer was interrupted. Please retry.");
                    }
                }
            }
        }
        if (!streamCompleted || !fullAnswerText.trim()) {
            throw new Error("The answer was interrupted before completion. Please retry.");
        }
    } catch (err) {
        console.error("Streaming error:", err);

        // Take the placeholder away, but ONLY if it never received a word. The
        // bubble now exists from the moment the question was sent, so a request
        // that dies before the first token would otherwise leave an empty bubble
        // sitting above the error message.
        //
        // A partly-streamed answer is left alone: it is real text the student can
        // read, and deleting it to replace it with "An error occurred." would
        // throw away the more useful of the two.
        const $pending = $('#' + assistantMessageId);
        $pending.find('.message-report-row').remove();
        if ($pending.find('.streaming-text').text().trim() === '') $pending.remove();

        if (requestConvId === activeConversationId) {
            addMessage(err.message || 'The answer was interrupted. Please retry.');
        }
    } finally {
        if (activeReader) {
            try { await activeReader.cancel(); } catch (_) { /* Connection already closed. */ }
            activeReader.releaseLock();
        }
        // Covers every exit the loop did not: a stream that ended with no chunk
        // at all, and the error path above when a partial answer was kept.
        clearTypingIndicator();
        sendButton.prop('disabled', false);
        messageInput.prop('disabled', false);
        if (window.innerWidth > 768) {
            messageInput.focus();
        }
    }
  }

  // --- Submit Handler ---
  messageForm.on('submit', function(e) {
    e.preventDefault();
    const message = messageInput.val().trim();
    
    const hasImage = imageInput[0].files.length > 0;
    const imageFile = hasImage ? imageInput[0].files[0] : null;

    if (!message && !hasImage) return;

    addMessage(message, true, true, hasImage);
    
    messageInput.val('').css('height', 'auto');
    if (hasImage) {
        imageInput.val('');
        previewContainer.hide();
    }
    
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);

    sendMessage(message, imageFile);
  });

  // --- Conversation History Loading ---
  function loadConversations() {
    if (isHistoryLoading) return;
    isHistoryLoading = true;

    conversationHistoryLoader.show();
    conversationList.empty();

    $.getJSON('/chat/conversations')
      .done(function(convs) {
        if (!convs || convs.length === 0) {
          conversationList.append(
            "<div class='conversation-item' style='pointer-events:none;'>No past chats</div>"
          );
          return;
        }
        convs
          .sort(
            (a, b) =>
              new Date(b.created_at) - new Date(a.created_at)
          )
          .forEach(c => {
            const safeTitle = (c.title || 'Untitled Chat')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;');
            const item = $(`
              <div class="conversation-item" data-id="${c.conv_id}">
                <div class="conv-main">
                  <i class="fas fa-comment-alt"></i>
                  <span>${safeTitle}</span>
                </div>
                <div class="conv-actions">
                  <button class="delete-btn" title="Delete">
                    <i class="fas fa-trash"></i>
                  </button>
                </div>
              </div>
            `);
            conversationList.append(item);
          });
        applyActiveHighlight();
      })
      .fail(function() {
        conversationList.append(
          "<div class='conversation-item' style='pointer-events:none; color: var(--ui-danger-ink);'>Failed to load chats</div>"
        );
      })
      .always(function() {
        isHistoryLoading = false;
        conversationHistoryLoader.hide();
      });
  }

  function loadSpecificConversation(convId) {
    if (convId === activeConversationId) return;

    activeConversationId = convId;
    isNewConversation = false;

    localStorage.setItem('activeConversationId', convId);
    applyActiveHighlight();

    messagesContainer.find('.message').remove();
    emptyChatState.hide();
    conversationLoader.show();
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);
    // The `.message` removal above already takes the in-flight bubble and its
    // dots with it. This clears the `is-thinking` bookkeeping and is a no-op when
    // nothing was in flight.
    clearTypingIndicator();

    $.post(`/chat/conversation/${convId}/restore`)
      .done(function() {
        $.getJSON(`/chat/conversation/${convId}`)
          .done(function(data) {
            renderChatHistory(data.messages);
          })
          .fail(function() {
            addMessage(
              "Sorry, I couldn't load this conversation. Please try again."
            );
          })
          .always(function() {
            conversationLoader.hide();
            sendButton.prop('disabled', false);
            messageInput.prop('disabled', false);
            
            if (window.innerWidth > 768) {
              messageInput.focus();
            }
          });
      })
      .fail(function() {
        addMessage(
          'An error occurred while switching conversations.'
        );
        conversationLoader.hide();
        sendButton.prop('disabled', false);
        messageInput.prop('disabled', false);
        
        if (window.innerWidth > 768) {
          messageInput.focus();
        }
      });
  }

  $(document).on('click', '.conversation-item', function(e) {
    if ($(e.target).closest('.delete-btn').length) return;
    const convId = $(this).data('id');
    loadSpecificConversation(convId);
  });

  function startNewChat() {
    $.post('/chat/clear', function() {
      messagesContainer.find('.message').remove();
      emptyChatState.fadeIn(200); 
      activeConversationId = null;
      isNewConversation = true;
      localStorage.removeItem('activeConversationId');
      applyActiveHighlight();
      sendButton.prop('disabled', false);
      messageInput.prop('disabled', false);
      
      if (window.innerWidth > 768) {
        messageInput.focus();
      }
      
      clearTypingIndicator();
      conversationLoader.hide();
      refreshSuggestions();
    });
  }

  $(document).on('click', '.delete-btn', function(e) {
    e.stopPropagation();
    const convId = $(this)
      .closest('.conversation-item')
      .data('id');
    const wasActive = activeConversationId === convId;

    if (confirm('Delete this conversation permanently?')) {
      $.ajax({
        url: `/chat/conversation/${convId}/delete`,
        type: 'DELETE',
      }).done(function() {
        if (wasActive) startNewChat();
        loadConversations();
      });
    }
  });

  $('.new-chat-btn').on('click', function() {
    startNewChat();
  });

  messageInput.on('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      messageForm.submit();
    }
  });

  messageInput.on('input', function() {
    this.style.height = 'auto';
    const newHeight = Math.min(this.scrollHeight, 120);
    this.style.height = `${newHeight}px`;
  });

  function initializeChat() {
    const shouldStartNew =
      $('body').data('start-new') === true ||
      $('body').data('start-new') === 'true';

    if (shouldStartNew) {
      startNewChat();
    } else {
      const savedConvId = localStorage.getItem(
        'activeConversationId'
      );
      if (savedConvId) {
        loadSpecificConversation(savedConvId);
      } else {
        startNewChat();
      }
    }
    loadConversations();
  }

  initializeChat();

});
