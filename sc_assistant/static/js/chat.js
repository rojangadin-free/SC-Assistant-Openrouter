/*
 * =========================================
 * CHAT-SPECIFIC JAVASCRIPT (chat.js)
 *
 * This file only contains logic for the
 * /chat page. Shared logic is in main.js.
 * =========================================
 */

$(document).ready(function() {
  // Check if showNotification exists, if not, create a fallback
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
  
  const conversationList = $("#conversationList");
  const conversationLoader = $('#conversationLoader');
  const conversationHistoryLoader = $('#conversationHistoryLoader');

  let isNewConversation = true;
  let activeConversationId = null;
  let isHistoryLoading = false;

  // --- Mobile Height & Keyboard Fixes ---
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
  // --- End Mobile Fixes ---


  // --- Chat rendering helpers ---
  function addMessage(content, isUser = false, autoScroll = true) {
    const processedContent = isUser
      ? content.replace(/</g, '&lt;').replace(/>/g, '&gt;')
      : marked.parse(content);

    const avatar = isUser
      ? '<div class="avatar"><i class="fas fa-user"></i></div>'
      : `<div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>`;

    const html = `
      <div class="message ${isUser ? 'user' : 'assistant'}">
        ${isUser ? '' : avatar}
        <div class="message-bubble">${processedContent}</div>
        ${isUser ? avatar : ''}
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
    messagesContainer.empty();
    if (!Array.isArray(history)) return;

    history.forEach(msg => {
      if (msg.role === 'system') return;
      addMessage(msg.content, msg.role === 'user', false);
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

  // --- Chat Send Logic ---
  function sendMessage(message) {
    const requestIsNew = isNewConversation;
    const requestConvId = activeConversationId;

    typingIndicator.removeClass('fade-out').show();

    $.ajax({
      url: '/chat/get', // Note: Updated URL
      type: 'POST',
      data: { msg: message },
    })
      .done(function(data) {
        const responseConvId = data.new_conversation_created
          ? data.conv_id
          : requestConvId;

        const isStillInNewChat =
          requestIsNew && activeConversationId === null;
        const isStillInSameChat =
          activeConversationId === responseConvId;

        if (isStillInNewChat || isStillInSameChat) {
          if (data && data.answer) {
            addMessage(data.answer, false);
          }

          if (data.new_conversation_created) {
            activeConversationId = data.conv_id;
            isNewConversation = false;
            localStorage.setItem(
              'activeConversationId',
              activeConversationId
            );
            addConversationToSidebar(
              data.conv_id,
              data.new_conv_title
            );
          }
        }
      })
      .fail(function() {
        if (requestConvId === activeConversationId) {
          addMessage(
            'An error occured.'
          );
        }
      })
      .always(function(dataOrXhr, textStatus) {
        let convIdFromResponse = null;
        if (
          textStatus === 'success' &&
          dataOrXhr.new_conversation_created
        ) {
          convIdFromResponse = dataOrXhr.conv_id;
        }
        const effectiveRequestConvId = requestIsNew
          ? convIdFromResponse
          : requestConvId;

        if (activeConversationId === effectiveRequestConvId) {
          typingIndicator.addClass('fade-out');
          setTimeout(() => typingIndicator.hide(), 300);
          sendButton.prop('disabled', false);
          messageInput.prop('disabled', false).focus(); // Auto-focus here
        }
      });
  }

  messageForm.on('submit', function(e) {
    e.preventDefault();
    const message = messageInput.val().trim();
    if (!message) return;

    addMessage(message, true);
    messageInput.val('').css('height', 'auto');
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);

    sendMessage(message);
  });

  // --- Conversation History Logic ---
  function loadConversations() {
    if (isHistoryLoading) return;
    isHistoryLoading = true;

    conversationHistoryLoader.show();
    conversationList.empty();

    $.getJSON('/chat/conversations') // Note: Updated URL
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

    messagesContainer.empty();
    conversationLoader.show();
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);
    typingIndicator.hide();

    $.post(`/chat/conversation/${convId}/restore`) // Note: Updated URL
      .done(function() {
        $.getJSON(`/chat/conversation/${convId}`) // Note: Updated URL
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
            messageInput.prop('disabled', false).focus(); // Auto-focus here
          });
      })
      .fail(function() {
        addMessage(
          'An error occurred while switching conversations.'
        );
        conversationLoader.hide();
        sendButton.prop('disabled', false);
        messageInput.prop('disabled', false).focus(); // Auto-focus here
      });
  }

  $(document).on('click', '.conversation-item', function(e) {
    if ($(e.target).closest('.delete-btn').length) return;
    const convId = $(this).data('id');
    loadSpecificConversation(convId);
  });

  function startNewChat() {
    $.post('/chat/clear', function() { // Note: Updated URL
      messagesContainer.empty();
      activeConversationId = null;
      isNewConversation = true;
      localStorage.removeItem('activeConversationId');
      applyActiveHighlight();
      sendButton.prop('disabled', false);
      messageInput.prop('disabled', false).focus(); // Auto-focus here
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
        url: `/chat/conversation/${convId}/delete`, // Note: Updated URL
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

  // --- Page Initialization ---
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
        startNewChat(); // Default to a new chat if no saved ID
      }
    }
    loadConversations();
  }

  initializeChat();
});