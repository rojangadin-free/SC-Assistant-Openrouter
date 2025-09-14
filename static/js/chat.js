$(document).ready(function() {
  const logoPath = '/static/images/logo.png';
  const messagesContainer = $('#messagesContainer');
  const messageInput = $('#messageInput');
  const sendButton = $('#sendButton');
  const typingIndicator = $('#typingIndicator');
  const messageForm = $('#messageForm');
  const themeToggle = $('#themeToggle');
  const body = $('body');
  const logoutButton = $('#logoutButton');
  const confirmDialog = $('#confirmDialog');
  const cancelLogout = $('#cancelLogout');
  const confirmLogoutButton = $('#confirmLogoutButton');
  const conversationList = $("#conversationList");
  const conversationLoader = $('#conversationLoader');
  const conversationHistoryLoader = $('#conversationHistoryLoader');

  let isNewConversation = true;
  let activeConversationId = null;
  let isHistoryLoading = false;

  // ... (All helper functions like adjustViewportHeight, handleMobileKeyboard, setTheme, etc., remain unchanged) ...
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
        messagesContainer[0].scrollTop = messagesContainer[0].scrollHeight;
      }, 100);
    }
    viewport.addEventListener('resize', viewportHandler);
  }

  function initMobileFixes() {
    adjustViewportHeight();
    handleMobileKeyboard();
  }

  window.addEventListener('resize', adjustViewportHeight);
  window.addEventListener('orientationchange', () => setTimeout(adjustViewportHeight, 100));

  initMobileFixes();

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
  themeToggle.on('click', function() {
    const current = body.attr('data-theme') === 'dark' ? 'dark' : 'light';
    setTheme(current === 'dark' ? 'light' : 'dark');
  });

  logoutButton.on('click', function(e) {
    e.preventDefault();
    confirmDialog.addClass('active');
  });
  cancelLogout.on('click', () => confirmDialog.removeClass('active'));
  confirmLogoutButton.on('click', function() {
    const logoutUrl = logoutButton.find('a').attr('href');
    window.location.href = logoutUrl;
  });
  confirmDialog.on('click', function(e) {
    if ($(e.target).is('.confirm-dialog')) {
      confirmDialog.removeClass('active');
    }
  });

  function addMessage(content, isUser = false, autoScroll = true) {
    let processedContent = isUser
      ? content.replace(/</g, "&lt;").replace(/>/g, "&gt;")
      : marked.parse(content);

    const avatar = isUser
      ? '<div class="avatar"><i class="fas fa-user"></i></div>'
      : `<div class="avatar"><img src="${logoPath}" alt="AI Assistant"></div>`;

    const messageHtml = `
      <div class="message ${isUser ? 'user' : 'assistant'}">
        ${isUser ? '' : avatar}
        <div class="message-bubble">${processedContent}</div>
        ${isUser ? avatar : ''}
      </div>
    `;

    messagesContainer.append(messageHtml);

    if (autoScroll) {
      messagesContainer.stop().animate({ scrollTop: messagesContainer[0].scrollHeight}, 300);
    }
  }

  function renderChatHistory(history) {
    messagesContainer.empty();
    if (!history || !Array.isArray(history)) {
      return;
    }
    history.forEach(message => {
      if (message.role === 'system') {
        return;
      }
      addMessage(message.content, message.role === 'user', false);
    });
    messagesContainer.stop().animate({ scrollTop: messagesContainer[0].scrollHeight }, 300);
  }

  function sendMessage(message) {
    const requestIsNew = isNewConversation;
    const requestConvId = activeConversationId;

    typingIndicator.removeClass('fade-out').show();

    $.ajax({
      data: { msg: message },
      type: "POST",
      url: "/get",
      timeout: 30000
    }).done(function(data) {
      const responseConvId = data.new_conversation_created ? data.conv_id : requestConvId;
      const isStillInNewChat = requestIsNew && activeConversationId === null;
      const isStillInSameChat = activeConversationId === responseConvId;

      if (isStillInNewChat || isStillInSameChat) {
        if (data && data.chat_history) {
          renderChatHistory(data.chat_history);
        }
        if (data.new_conversation_created) {
          activeConversationId = data.conv_id;
          isNewConversation = false;
          // ⭐ CHANGE: Save new conversation ID to localStorage
          localStorage.setItem('activeConversationId', activeConversationId);
        }
      }
      loadConversations();
    }).fail(function() {
      if (requestConvId === activeConversationId) {
        addMessage("Can you ask the question again? I am having trouble finding an answer to that question.");
      }
    }).always(function(dataOrXhr, textStatus) {
      let convIdFromResponse = null;
      if (textStatus === 'success' && dataOrXhr.new_conversation_created) {
        convIdFromResponse = dataOrXhr.conv_id;
      }
      const effectiveRequestConvId = requestIsNew ? convIdFromResponse : requestConvId;

      if (activeConversationId === effectiveRequestConvId) {
        typingIndicator.addClass('fade-out');
        setTimeout(() => typingIndicator.hide(), 300);
        sendButton.prop('disabled', false);
        messageInput.prop('disabled', false);
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

  function applyActiveHighlight() {
    $('.conversation-item').removeClass('active-conversation');
    if (activeConversationId) {
      $(`.conversation-item[data-id="${activeConversationId}"]`).addClass('active-conversation');
    }
  }

  function loadConversations() {
    if (isHistoryLoading) {
      return;
    }
    isHistoryLoading = true;

    conversationHistoryLoader.show();
    conversationList.empty();

    $.getJSON("/conversations")
      .done(function(convs) {
        if (!convs || convs.length === 0) {
          conversationList.append("<div class='conversation-item' style='pointer-events:none;'>No past chats</div>");
          return;
        }
        convs.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
        convs.forEach(c => {
          const safeTitle = (c.title || 'Untitled Chat').replace(/</g, "&lt;").replace(/>/g, "&gt;");
          const item = $(`
            <div class="conversation-item" data-id="${c.conv_id}">
              <div class="conv-main">
                <i class="fas fa-comment-alt"></i>
                <span>${safeTitle}</span>
              </div>
              <div class="conv-actions">
                <button class="delete-btn" title="Delete"><i class="fas fa-trash"></i></button>
              </div>
            </div>
          `);
          conversationList.append(item);
        });
        applyActiveHighlight();
      })
      .fail(function() {
        conversationList.append("<div class='conversation-item' style='pointer-events:none; color: #ef4444;'>Failed to load chats</div>");
      })
      .always(function() {
        isHistoryLoading = false;
        conversationHistoryLoader.hide();
      });
  }

  // ⭐ NEW: Refactored function to load a specific conversation
  function loadSpecificConversation(convId) {
    if (convId === activeConversationId) {
      return; // Don't reload if it's already active
    }

    activeConversationId = convId;
    isNewConversation = false;

    // Save the active ID to localStorage
    localStorage.setItem('activeConversationId', convId);
    applyActiveHighlight();

    messagesContainer.empty();
    conversationLoader.show();
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);
    typingIndicator.hide();

    $.post(`/conversation/${convId}/restore`).done(function() {
      $.getJSON(`/conversation/${convId}`, function(data) {
        renderChatHistory(data.messages);
      }).fail(function() {
        addMessage("Sorry, I couldn't load this conversation. Please try again.");
      }).always(function() {
        conversationLoader.hide();
        sendButton.prop('disabled', false);
        messageInput.prop('disabled', false);
      });
    }).fail(function() {
      addMessage("An error occurred while switching conversations.");
      conversationLoader.hide();
      sendButton.prop('disabled', false);
      messageInput.prop('disabled', false);
    });
  }

  // ⭐ CHANGE: The click handler now uses the refactored function
  $(document).on('click', '.conversation-item', function(e) {
    if ($(e.target).closest('.delete-btn').length) {
      return;
    }
    const convId = $(this).data('id');
    loadSpecificConversation(convId);
  });

  function startNewChat() {
    $.post("/clear", function() {
      messagesContainer.empty();
      activeConversationId = null;
      isNewConversation = true;
      // ⭐ CHANGE: Remove the saved conversation ID from localStorage
      localStorage.removeItem('activeConversationId');
      applyActiveHighlight();
      sendButton.prop('disabled', false);
      messageInput.prop('disabled', false);
      typingIndicator.hide();
      conversationLoader.hide();
    });
  }

  $(document).on('click', '.delete-btn', function(e) {
    e.stopPropagation();
    const convId = $(this).closest('.conversation-item').data('id');
    const wasActive = (activeConversationId === convId);

    if (confirm("Delete this conversation permanently?")) {
      $.ajax({
        url: `/conversation/${convId}/delete`,
        type: "DELETE"
      }).done(function() {
        if (wasActive) {
          startNewChat(); // This will also clear localStorage
        }
        loadConversations();
      });
    }
  });

  $(".new-chat-btn").on("click", function() {
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
    this.style.height = newHeight + 'px';
  });

  // ⭐ NEW: Logic to run on page load
  function initializeChat() {
    const savedConvId = localStorage.getItem('activeConversationId');
    if (savedConvId) {
      // If there's a saved conversation, load it
      loadSpecificConversation(savedConvId);
    }
    // Always load the conversation history list for the sidebar
    loadConversations();
  }

  initializeChat(); // Call the initialization function
});

// ... (The hamburger menu functionality at the end of the file remains unchanged) ...
document.addEventListener('DOMContentLoaded', function() {
  const hamburgerMenu = document.getElementById('hamburgerMenu');
  const sidebar = document.getElementById('sidebar');
  const body = document.body;

  if (hamburgerMenu && sidebar) {
    hamburgerMenu.addEventListener('click', function(e) {
      e.stopPropagation();
      sidebar.classList.toggle('active');
      body.classList.toggle('sidebar-open');
    });

    document.addEventListener('click', function(e) {
      if (window.innerWidth <= 768 && sidebar.classList.contains('active')) {
        const isClickInsideSidebar = sidebar.contains(e.target);
        if (!isClickInsideSidebar) {
          sidebar.classList.remove('active');
          body.classList.remove('sidebar-open');
        }
      }
    });

    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && sidebar.classList.contains('active')) {
        sidebar.classList.remove('active');
        body.classList.remove('sidebar-open');
      }
    });

    window.addEventListener('resize', () => {
       if (window.innerWidth > 768) {
          sidebar.classList.remove('active');
          body.classList.remove('sidebar-open');
       }
    });
  }
});