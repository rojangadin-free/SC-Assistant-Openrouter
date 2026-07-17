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
  
  // Globals for generation state
  let currentController = null; 
  let currentAssistantMessageId = null;
  let currentFullAnswerText = "";

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

  function addMessage(content, isUser = false, autoScroll = true, hasImage = false) {
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

    const msgId = 'msg-' + Date.now() + '-' + Math.floor(Math.random() * 9999);

    const reportRow = !isUser
      ? `<div class="message-report-row">
           <button class="report-btn" data-msg-id="${msgId}" title="Report this response">
             <i class="fas fa-flag"></i>
           </button>
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

    if (autoScroll && messagesContainer.length > 0) {
      messagesContainer.stop().animate(
        { scrollTop: messagesContainer[0].scrollHeight },
        300
      );
    }
  }

  function renderChatHistory(history) {
    messagesContainer.find('.message').remove(); 
    
    if (!Array.isArray(history) || history.length === 0) {
      emptyChatState.show();
      return;
    }
    
    emptyChatState.hide();

    history.forEach(msg => {
      if (msg.role === 'system') return;
      
      let content = msg.content;
      const hasImageTag = content.includes('[Image Uploaded]');
      content = content.replace(' [Image Uploaded]', '');
      
      addMessage(content, msg.role === 'user', false, hasImageTag);
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

  async function sendMessage(message, imageFile) {
    const assistantMessageId = 'msg-' + Date.now() + '-' + Math.floor(Math.random() * 9999);
    currentAssistantMessageId = assistantMessageId;
    currentFullAnswerText = "";
    
    // 🚀 CREATE THE BUBBLE IMMEDIATELY
    const htmlPlaceholder = `
      <div class="message assistant" id="${assistantMessageId}">
        <div class="message-row">
          <div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>
          <div class="message-bubble"><div class="streaming-text"></div></div>
        </div>
        <div class="message-report-row">
           <button class="report-btn" data-msg-id="${assistantMessageId}" title="Report this response">
             <i class="fas fa-flag"></i>
           </button>
         </div>
      </div>
    `;
    messagesContainer.append(htmlPlaceholder);
    typingIndicator.removeClass('fade-out').show();

    const formData = new FormData();
    formData.append("msg", message);
    if (imageFile) formData.append("image", imageFile);

    currentController = new AbortController();
    const signal = currentController.signal;

    try {
        const response = await fetch('/chat/get', {
            method: 'POST',
            body: formData,
            signal: signal 
        });

        if (!response.ok) throw new Error("Network response was not ok");

        const assistantBubble = $(`#${assistantMessageId}`).find('.streaming-text');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let isFirstToken = true;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const lines = decoder.decode(value, { stream: true }).split('\n');
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const data = JSON.parse(line.substring(6));
                    if (data.type === 'chunk') {
                        if (isFirstToken) { typingIndicator.addClass('fade-out').hide(); isFirstToken = false; }
                        currentFullAnswerText += data.text;
                        assistantBubble.html(renderCitations(currentFullAnswerText));
                        messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
                    }
                }
            }
        }
    } catch (err) {
        if (err.name === 'AbortError') {
            // 🚀 TARGET THE EXISTING BUBBLE
            const bubble = $(`#${assistantMessageId} .message-bubble`);
            bubble.append(`
                <div style="margin-top: 10px; color: #ef4444; font-size: 0.85rem; font-style: italic;">
                    <i class="fas fa-stop-circle"></i> Generation stopped by user.
                </div>
            `);
            messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
        }
    } finally {
        currentController = null;
        typingIndicator.addClass('fade-out');
        setTimeout(() => typingIndicator.hide(), 300);
        sendButton.html('<i class="fas fa-paper-plane"></i>').css({'background-color': '', 'color': ''});
        messageInput.prop('disabled', false);
    }
  }

  // 🚀 INSTANT ABORT HANDLER - Bypass HTML5 Validation
  sendButton.on('click', function(e) {
      if (currentController) {
          e.preventDefault(); // Stop the form submission cycle immediately
          currentController.abort(); // Triggers the AbortError in the catch block instantly
      }
  });

  // --- Submit Handler ---
  messageForm.on('submit', function(e) {
    e.preventDefault();

    if (currentController) return;

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
    
    messageInput.prop('disabled', true);

    // Transform the send button into a Stop button
    sendButton.html('<i class="fas fa-stop"></i>').css({
        'background-color': '#ef4444', 
        'color': 'white'
    });

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

  // ── Report Button ──────────────────────────────────────────────
  let reportTargetMsgId = null;

  $(document).on('click', '.report-btn', function() {
    reportTargetMsgId = $(this).data('msg-id');
    const snippet = $('#' + reportTargetMsgId + ' .message-bubble').text().trim().substring(0, 200);
    $('#reportMsgSnippet').text(snippet ? '"' + snippet + '…"' : '');
    $('#reportReasonSelect').val('');
    $('#reportOtherBox').hide();
    $('#reportOtherText').val('');
    $('#reportModal').fadeIn(150);
  });

  $('#reportReasonSelect').on('change', function() {
    if ($(this).val() === 'Others') {
      $('#reportOtherBox').slideDown(150);
    } else {
      $('#reportOtherBox').slideUp(150);
      $('#reportOtherText').val('');
    }
  });

  $('#reportCancelBtn').on('click', function() {
    $('#reportModal').fadeOut(150);
    reportTargetMsgId = null;
  });

  $('#reportModal').on('click', function(e) {
    if ($(e.target).is('#reportModal')) {
      $('#reportModal').fadeOut(150);
      reportTargetMsgId = null;
    }
  });

  $('#reportSubmitBtn').on('click', function() {
    const reason = $('#reportReasonSelect').val();
    if (!reason) {
      alert('Please select a reason.');
      return;
    }
    const otherText = reason === 'Others' ? $('#reportOtherText').val().trim() : '';
    if (reason === 'Others' && !otherText) {
      alert('Please describe the issue.');
      return;
    }

    const msgSnippet = $('#' + reportTargetMsgId + ' .message-bubble').text().trim().substring(0, 500);
    const convId = activeConversationId || null;

    $('#reportSubmitBtn').prop('disabled', true).text('Submitting…');

    $.ajax({
      url: '/chat/report',
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({
        msg_id:      reportTargetMsgId,
        conv_id:     convId,
        reason:      reason,
        other_text:  otherText,
        msg_snippet: msgSnippet,
      }),
      success: function() {
        $('#reportModal').fadeOut(150);
        reportTargetMsgId = null;
        window.showNotification('Report submitted. Thank you!', 'success');
      },
      error: function() {
        window.showNotification('Failed to submit report. Please try again.', 'error');
      },
      complete: function() {
        $('#reportSubmitBtn').prop('disabled', false).text('Submit Report');
      }
    });
  });

});