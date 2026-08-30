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
  const typingIndicator = $('#typingIndicator');
  const messageForm = $('#messageForm');
  const emptyChatState = $('#emptyChatState');
  
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

  $('.faq-btn').on('click', function() {
    const question = $(this).text();
    $('#messageInput').val(question);
    $('#messageForm').submit();
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

  // ── Answer feedback (👍/👎) ─────────────────────────────────────
  //
  // There used to be a flag button here too, opening a form with a required
  // reason. Two controls for one intention: the thumb cost a click, the flag cost
  // a decision and a submit, and they wrote to different tables. Predictably the
  // votes arrived and the reports did not, so the admin's queue was empty while
  // the satisfaction number quietly fell.
  //
  // Now there is one control. The 👎 records the vote; picking a reason
  // afterwards is what files the report. Two different things are being
  // collected — a statistic and a work order — and only the second is worth an
  // admin's attention, so only the second is created when a student explains it.

  //
  // The vote is sent WITH the citations of the answer it judges. A verdict alone
  // says something is wrong; a verdict plus "these are the pages it came from"
  // tells the admin WHICH document to fix. See rag/feedback.py.
  function feedbackButtons(msgId) {
    return `
      <div class="feedback-group" data-msg-id="${msgId}">
        <button class="feedback-btn feedback-up" data-verdict="up" title="This answer helped">
          <i class="far fa-thumbs-up"></i>
        </button>
        <button class="feedback-btn feedback-down" data-verdict="down" title="This answer was wrong or unhelpful">
          <i class="far fa-thumbs-down"></i>
        </button>
      </div>`;
  }

  // The question this answer replied to = the nearest preceding user message.
  // Reading it from the DOM keeps voting on *reloaded* conversations working;
  // tracking it in a variable would only ever know about the current session.
  function questionFor($message) {
    const $prevUser = $message.prevAll('.message.user').first();
    return $prevUser.length ? $prevUser.find('.message-bubble').text().trim() : '';
  }

  // The rendered citation badges carry filename + page in their data/href, so the
  // sources can be recovered from the DOM instead of being threaded through the
  // streaming code and stored per message.
  //
  // The source footer (below) is read as well, because inline badges only appear
  // when the model chose to cite mid-sentence — the footer is always there, so a
  // vote never travels without provenance.
  function sourcesFor($message) {
    const out = [];
    $message.find('.message-bubble a[data-source]').each(function() {
      const file = $(this).data('source');
      const href = $(this).attr('href') || '';
      const page = (href.match(/#page=(\d+)/) || [])[1];
      const label = page ? `${file}|p.${page}` : `${file}`;
      if (file && out.indexOf(label) === -1) out.push(label);
    });
    $message.find('.answer-sources .source-chip').each(function() {
      const label = $(this).data('label');
      if (label && out.indexOf(label) === -1) out.push(label);
    });
    return out;
  }


  // ── Where this answer came from ────────────────────────────────
  //
  // The retrieval pipeline knows the exact file and page behind every answer,
  // and until now that knowledge died inside the prompt. Printing it does three
  // things at once: the student can verify the claim, they can open the page and
  // read the surrounding rules themselves, and a wrong answer arrives with the
  // name of the file to fix instead of a guess.
  //
  // The footer is deliberately quiet — one line, collapsed by default when there
  // are several files — because a citation that shouts competes with the answer.
  function renderSourceFooter($message, citations) {
    if (!citations || !citations.length) return;
    $message.find('.answer-sources').remove();

    const chips = citations.map(function(c) {
      // Deep-link to the page when the viewer supports it; the file itself
      // otherwise. A citation you cannot open is only half a citation.
      const firstPage = (c.pages && c.pages.length) ? c.pages[0] : null;
      let url = '/chat/document/' + encodeURIComponent(c.source);
      if (firstPage && /\.pdf$/i.test(c.source)) url += '#page=' + firstPage;

      const label = (c.label || c.source).replace(/</g, '&lt;');
      return '<a class="source-chip" href="' + url + '" target="_blank" rel="noopener" ' +
             'data-label="' + String(c.label || c.source).replace(/"/g, '&quot;') + '" ' +
             'title="Open ' + String(c.source).replace(/"/g, '&quot;') + '">' +
               '<i class="fas fa-file-pdf"></i> ' + label +
             '</a>';
    }).join('');

    $message.find('.message-row').after(
      '<div class="answer-sources">' +
        '<span class="answer-sources-label"><i class="fas fa-book-open"></i> Based on</span>' +
        '<span class="answer-sources-list">' + chips + '</span>' +
      '</div>'
    );
  }


  $(document).on('click', '.feedback-btn', function() {
    const $btn = $(this);
    const $group = $btn.closest('.feedback-group');
    const msgId = $group.data('msg-id');
    const verdict = $btn.data('verdict');
    const $message = $('#' + msgId);

    // Clicking the already-active thumb is a no-op rather than a toggle-off: an
    // absent vote and a retracted vote are indistinguishable in the data, so
    // there is nothing to gain by allowing it.
    if ($btn.hasClass('voted')) return;

    $group.find('.feedback-btn').removeClass('voted');
    $btn.addClass('voted');

    $.ajax({
      url: '/chat/feedback',
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({
        msg_id:   msgId,
        verdict:  verdict,
        question: questionFor($message),
        answer:   $message.find('.message-bubble').text().trim().substring(0, 1000),
        sources:  sourcesFor($message),
        conv_id:  activeConversationId || '',
      }),
      success: function(res) {
        if (verdict === 'down') {
          // Nothing has been reported yet — the vote is counted, and answering
          // this sheet is what files the report. Ignoring or closing it leaves the
          // downvote as a statistic, which is all a bare thumb honestly is.
          showReasonSheet($message, msgId, (res && res.can_report) === true);

          // A downvote is the moment a student is demonstrably unsatisfied, which
          // makes it the right moment — and the only reliable one — to offer the
          // human fallback. Prompting everyone would be nagging.
          showEscalationOffer($message);
        } else {
          showNotification('Thanks for the feedback!', 'success');
        }
      },
      error: function() {
        $group.find('.feedback-btn').removeClass('voted');
        showNotification('Could not save your feedback.', 'error');
      }
    });
  });


  // ── "What went wrong?" ─────────────────────────────────────────
  //
  // Shown after the 👎, and answering it is what creates the report. A bare thumb
  // is counted but not filed: reporting every one of them meant the admin's queue
  // filled with rows that said nothing beyond "someone disliked this", and the
  // complaints that named a wrong figure were lost in the noise.
  //
  // Inline chips rather than a modal with a <select> and a Submit — the old flag
  // form proved that a wall between a student and their complaint is a wall the
  // complaint does not cross. One tap is the whole cost of filing one here.

  const REASONS = [
    'Wrong information',
    'Outdated / no longer true',
    'Incomplete answer',
    'Not related to my question',
    'Confusing wording',
  ];

  function showReasonSheet($message, msgId, canReport) {
    if ($message.find('.reason-sheet').length) return;

    // A guest cannot file a report — there is no address for the admin to reply
    // to — so asking them what went wrong would collect an answer with nowhere to
    // go. Their vote still counts toward the topic's satisfaction score.
    if (!canReport) return;


    const chips = REASONS.map(function(r) {
      return '<button class="reason-chip" data-reason="' + escapeHtml(r) + '">' +
             escapeHtml(r) + '</button>';
    }).join('');

    $message.append(
      '<div class="reason-sheet" data-msg-id="' + escapeHtml(msgId) + '">' +
        '<div class="reason-sheet-head">' +
          '<span><i class="fas fa-circle-question"></i> What went wrong? <em>(optional)</em></span>' +
          '<button class="reason-dismiss" title="Dismiss">&times;</button>' +
        '</div>' +
        '<div class="reason-chips">' + chips +
          '<button class="reason-chip reason-other" data-reason="Others">Something else…</button>' +
        '</div>' +
        '<div class="reason-other-box" style="display:none;">' +
          '<input type="text" class="reason-other-text" maxlength="300" ' +
                 'placeholder="Tell the admin what was wrong…">' +
          '<button class="reason-other-send">Send</button>' +
        '</div>' +
      '</div>'
    );
  }

  function sendReason($sheet, reason, otherText) {
    const msgId = $sheet.data('msg-id');
    const $message = $('#' + msgId);

    $.ajax({
      url: '/chat/report',
      method: 'POST',
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
      // This request IS the report now, so the outcome has to be told truthfully.
      // Confirming on failure would leave the student believing the admin was
      // notified when nothing was filed — and they would never think to say it
      // again.
      success: function() {
        $sheet.html('<div class="reason-thanks">' +
                      '<i class="fas fa-check-circle"></i> Thanks — the admin has been notified.' +
                    '</div>');
        setTimeout(function() { $sheet.fadeOut(200, function() { $(this).remove(); }); }, 2600);
      },
      error: function() {
        // The sheet stays open so the tap can simply be repeated.
        showNotification('Could not send that. Please try again.', 'error');
      }
    });

  }

  $(document).on('click', '.reason-chip', function() {
    const $chip = $(this);
    const $sheet = $chip.closest('.reason-sheet');

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

  // Dismissable, and dismissing files nothing. The vote still stands, which is
  // the honest record of what the student actually said: this answer was bad,
  // without elaborating.

  $(document).on('click', '.reason-dismiss', function() {
    $(this).closest('.reason-sheet').fadeOut(150, function() { $(this).remove(); });
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
        <button class="escalate-open-btn">Ask a human</button>
        <button class="escalate-dismiss" title="No thanks">&times;</button>
      </div>
    `);

    offer.data('question', question);
    $message.append(offer);
  }

  // Declining does not retract anything: the downvote is already recorded. All
  // this removes is the suggestion, so it is a pure UI dismissal with nothing to
  // tell the server about.

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



  /* ══════════════════════════════════════════════════════════════
     PINNED ANNOUNCEMENTS
     The only thing on this page that speaks before it is spoken to. A student
     who opens the chat during a suspension should not have to think of the
     right question first; by the time they ask "may klase ba bukas?" they have
     often already left the house.

     Polled, not rendered once at page load: the tab stays open for hours, and a
     notice posted at 6am is worthless to someone who loaded the page at 5:50.
     ══════════════════════════════════════════════════════════════ */
  const ANN_POLL_MS = 120000;

  const ANN_KIND = {
    urgent:    { icon: 'fa-triangle-exclamation', word: 'Urgent' },
    important: { icon: 'fa-circle-exclamation',   word: 'Important' },
    info:      { icon: 'fa-circle-info',          word: 'Notice' }
  };

  // Who the dismissals belong to. localStorage is per-BROWSER, not per-account,
  // so on a shared campus PC one student dismissing a suspension notice used to
  // hide it from the next person who logged in on that machine — a student who
  // was never told classes were cancelled. The server sends an opaque tag for
  // the signed-in account and every dismissal key is namespaced with it.
  //
  // Until the first response arrives this is 'anon', which only ever means "show
  // the notice": erring toward showing a suspension twice is not a real cost.
  let annViewer = 'anon';

  // Keyed to the notice's CONTENT as well, not just its id: if an admin edits
  // "suspended until Friday" into "suspended until Monday", the key changes and
  // the banner comes back. Keying on the id alone would silently hide the
  // correction from exactly the students who dismissed the original.
  function annDismissKey(a) {
    return 'annDismissed:' + annViewer + ':' + a.key + ':' +
           (a.title + '|' + (a.body || '') + '|' + (a.expires_on || '')).length +
           ':' + (a.expires_on || '');
  }

  function annIsDismissed(a) {
    try { return localStorage.getItem(annDismissKey(a)) === '1'; }
    catch (e) { return false; }   // private mode: show it, never hide by accident
  }


  function renderAnnouncements(items) {
    const $banner = $('#announcementBanner');
    const visible = (items || []).filter(function(a) { return !annIsDismissed(a); });

    if (!visible.length) { $banner.hide().empty(); return; }

    $banner.html(visible.map(function(a) {
      const kind = ANN_KIND[a.priority] || ANN_KIND.info;
      // The end date is shown because "classes are suspended" without an end is
      // the sentence students over-read into next week.
      const until = a.expires_on
        ? '<span class="ann-notice-until">In effect until ' + escapeHtml(a.expires_on) + '</span>'
        : '';
      return '' +
      '<div class="ann-notice ' + escapeHtml(a.priority || 'info') + '" data-key="' + escapeHtml(a.key) + '">' +
        '<i class="fas ' + kind.icon + '"></i>' +
        '<div class="ann-notice-text">' +
          '<span class="ann-notice-kind">' + kind.word + '</span>' +
          '<span class="ann-notice-title">' + escapeHtml(a.title) + '</span>' +
          (a.body ? '<span class="ann-notice-body">' + escapeHtml(a.body) + '</span>' : '') +
          until +
        '</div>' +
        '<button class="ann-notice-close" title="Dismiss">&times;</button>' +
      '</div>';
    }).join('')).show();

    // Kept so the render can find the record again on dismiss without another
    // round-trip.
    $banner.data('items', visible);
  }

  function loadAnnouncements() {
    // Silent on failure: a broken banner must never block the conversation, and
    // an error box where a notice should be would be worse than nothing.
    $.getJSON('/api/announcements')
      .done(function(res) {
        if (!res || !res.success) return;
        // Learn who this browser is signed in as BEFORE filtering, or the first
        // paint of the page would use the previous account's dismissals.
        annViewer = res.viewer || 'anon';
        renderAnnouncements(res.announcements);
      });

  }

  $(document).on('click', '.ann-notice-close', function() {
    const $notice = $(this).closest('.ann-notice');
    const key = $notice.data('key');
    const items = $('#announcementBanner').data('items') || [];
    const a = items.filter(function(x) { return x.key === key; })[0];
    if (a) {
      try { localStorage.setItem(annDismissKey(a), '1'); } catch (e) { /* ignore */ }
    }
    $notice.slideUp(120, function() {
      $(this).remove();
      if (!$('#announcementBanner .ann-notice').length) $('#announcementBanner').hide();
    });
  });

  loadAnnouncements();
  setInterval(loadAnnouncements, ANN_POLL_MS);



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

    // Feedback only. The flag button that used to sit here is gone: the 👎 leads
    // straight into the same question the flag form asked, so a second control
    // would have been a second way to say one thing.

    const reportRow = !isUser
      ? `<div class="message-report-row">
           ${feedbackButtons(msgId)}
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

    // Rebuild the "Based on" line from the stored citations. Same renderer as the
    // live path, so a reopened answer shows its sources identically instead of
    // looking like an answer that never had any.
    if (!isUser && citations && citations.length) {
      renderSourceFooter($('#' + msgId), citations);
    }

    if (autoScroll && messagesContainer.length > 0) {
      messagesContainer.stop().animate(
        { scrollTop: messagesContainer[0].scrollHeight },
        300
      );
    }

    return msgId;
  }


  // Paint the thumbs a student already chose. Done in one request for the whole
  // thread (see /chat/feedback/batch) and after the messages exist, since it only
  // decorates elements the history just created.
  function restoreVotes(msgIds) {
    if (!msgIds || !msgIds.length) return;

    $.ajax({
      url: '/chat/feedback/batch',
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({ msg_ids: msgIds }),
    }).done(function(res) {
      if (!res || !res.success || !res.votes) return;
      Object.keys(res.votes).forEach(function(mid) {
        const verdict = res.votes[mid];
        if (verdict !== 'up' && verdict !== 'down') return;
        $('#' + mid)
          .find('.feedback-btn.feedback-' + verdict)
          .addClass('voted');
      });
    });
    // No error branch on purpose: an un-restored thumb is a cosmetic loss, and
    // clicking it again simply overwrites the same vote.
  }


  function renderChatHistory(history) {
    messagesContainer.find('.message').remove(); 
    
    if (!Array.isArray(history) || history.length === 0) {
      emptyChatState.show();
      return;
    }
    
    emptyChatState.hide();

    // Collected while painting, then resolved in one request. A reopened thread
    // has to look exactly like the one the student left: same sources under each
    // answer, same thumb still pressed.
    const voteIds = [];

    history.forEach(msg => {
      if (msg.role === 'system') return;
      
      let content = msg.content;
      const hasImageTag = content.includes('[Image Uploaded]');
      content = content.replace(' [Image Uploaded]', '');
      
      const isUser = msg.role === 'user';
      const id = addMessage(
        content, isUser, false, hasImageTag,
        // Older conversations were saved before ids/citations were persisted, so
        // both are optional: those answers still render, they just cannot carry a
        // vote back.
        isUser ? null : (msg.msg_id || null),
        isUser ? null : (msg.citations || null)
      );
      if (!isUser && msg.msg_id) voteIds.push(id);
    });

    restoreVotes(voteIds);

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

    typingIndicator.removeClass('fade-out').show();

    const assistantMessageId = 'msg-' + Date.now() + '-' + Math.floor(Math.random() * 9999);
    
    const htmlPlaceholder = `
      <div class="message assistant" id="${assistantMessageId}">
        <div class="message-row">
          <div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>
          <div class="message-bubble"><div class="streaming-text"></div></div>
        </div>
        <div class="message-report-row">
           ${feedbackButtons(assistantMessageId)}
         </div>
      </div>
    `;

    const formData = new FormData();
    formData.append("msg", message);
    if (imageFile) {
        formData.append("image", imageFile);
    }

    try {
        const response = await fetch('/chat/get', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) throw new Error("Network response was not ok");

        messagesContainer.append(htmlPlaceholder);
        const assistantBubble = $(`#${assistantMessageId}`).find('.streaming-text');

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullAnswerText = "";
        let isFirstToken = true;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const lines = decoder.decode(value, { stream: true }).split('\n');
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const data = JSON.parse(line.substring(6));

                    if (data.type === 'chunk') {
                        if (isFirstToken) {
                            typingIndicator.addClass('fade-out').hide();
                            isFirstToken = false;
                        }
                        fullAnswerText += data.text;
                        assistantBubble.html(renderCitations(fullAnswerText));
                        messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
                    } 
                    else if (data.type === 'done') {
                        // Sources arrive with 'done', not with the chunks: the
                        // footer is a property of the finished answer, and
                        // re-rendering it on every token would make it flicker.
                        renderSourceFooter($('#' + assistantMessageId), data.citations);

                        // Re-key the bubble to the id the answer was SAVED under.
                        // The placeholder id was invented locally so streaming had
                        // somewhere to write; if it stays, a vote cast now is filed
                        // under an id that exists nowhere in the stored history and
                        // vanishes on the next reload.
                        if (data.msg_id) {
                            const $live = $('#' + assistantMessageId);
                            $live.attr('id', data.msg_id);
                            $live.find('.feedback-group').attr('data-msg-id', data.msg_id)
                                                         .data('msg-id', data.msg_id);
                        }


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
                        assistantBubble.html(`<span style="color:red">${data.text}</span>`);
                    }
                }
            }
        }
    } catch (err) {
        console.error("Streaming error:", err);
        if (requestConvId === activeConversationId) {
            addMessage('An error occurred.');
        }
    } finally {
        typingIndicator.addClass('fade-out');
        setTimeout(() => typingIndicator.hide(), 300);
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
          "<div class='conversation-item' style='pointer-events:none; color: #ef4444;'>Failed to load chats</div>"
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
    typingIndicator.hide();

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
      
      typingIndicator.hide();
      conversationLoader.hide();
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
