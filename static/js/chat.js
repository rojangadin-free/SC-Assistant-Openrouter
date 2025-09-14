$(document).ready(function() {
  const logoPath = '/static/images/logo.png'; // Make sure this path is correct
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

  let isNewConversation = true;
  let activeConversationId = null;

  // Enhanced viewport height handling for mobile
  function adjustViewportHeight() {
    const vh = window.innerHeight * 0.01;
    document.documentElement.style.setProperty('--vh', `${vh}px`);
  }

  // Function to handle keyboard visibility on mobile
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

  // Initialize mobile fixes
  function initMobileFixes() {
    adjustViewportHeight();
    handleMobileKeyboard();
  }
  
  window.addEventListener('resize', adjustViewportHeight);
  window.addEventListener('orientationchange', () => setTimeout(adjustViewportHeight, 100));

  initMobileFixes();
  
  // Theme toggle
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
  
  // Logout confirmation
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

  // ⭐ 1. NEW FUNCTION: Renders the entire chat history from an array
  // This function is the core fix for the disappearing messages bug.
  function renderChatHistory(history) {
    messagesContainer.empty(); // Clear all existing messages first
    if (!history || !Array.isArray(history)) {
      return;
    }
    history.forEach(message => {
      // Do not display 'system' role messages in the chat window
      if (message.role === 'system') {
        return; 
      }
      // Use the existing addMessage function to create each bubble
      // Set autoScroll to false to prevent jarring scroll on each message
      addMessage(message.content, message.role === 'user', false);
    });
    // Scroll to the bottom once after all messages have been added
    messagesContainer.stop().animate({ scrollTop: messagesContainer[0].scrollHeight }, 300);
  }
  
  function sendMessage(message) {
    typingIndicator.removeClass('fade-out').show();
    
    $.ajax({
      data: { msg: message },
      type: "POST",
      url: "/get",
      timeout: 30000
    }).done(function(data) {
      // ⭐ 2. MODIFIED: Instead of adding one message, render the full history
      if (data && data.chat_history) {
        renderChatHistory(data.chat_history);
      } else {
        // Fallback for older server versions or errors
        addMessage(data.answer || "Sorry, I couldn't get a response.");
      }
      
      if (data.new_conversation_created) {
        activeConversationId = data.conv_id;
        loadConversations();
      }
    }).fail(function() {
      addMessage("Can you ask the question again? I am having trouble finding an answer to that question.");
    }).always(function() {
      typingIndicator.addClass('fade-out');
      setTimeout(() => typingIndicator.hide(), 300);
      sendButton.prop('disabled', false);
      messageInput.prop('disabled', false);
    });
  }

  messageForm.on('submit', function(e) {
    e.preventDefault();
    const message = messageInput.val().trim();
    if (!message) return;
    
    // We add the user's message immediately for a responsive feel,
    // but the renderChatHistory function will soon redraw it.
    addMessage(message, true);
    messageInput.val('').css('height', 'auto');
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);
    
    if (isNewConversation) {
      $.post("/clear", {}, function() {
        sendMessage(message);
        isNewConversation = false;
      }).fail(function() {
        console.error("Could not clear session, proceeding with new chat.");
        sendMessage(message);
        isNewConversation = false;
      });
    } else {
      sendMessage(message);
    }
  });
  
  function applyActiveHighlight() {
    $('.conversation-item').removeClass('active-conversation');
    if (activeConversationId) {
      $(`.conversation-item[data-id="${activeConversationId}"]`).addClass('active-conversation');
    }
  }

  function loadConversations() {
    $.getJSON("/conversations", function(convs) {
      conversationList.empty();
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
    });
  }
  
  $(document).on('click', '.conversation-item', function(e) {
    if ($(e.target).closest('.delete-btn').length) {
      return;
    }
    const convId = $(this).data('id');
    activeConversationId = convId;
    applyActiveHighlight();
    isNewConversation = false; 

    $.post(`/conversation/${convId}/restore`).done(function() {
      $.getJSON(`/conversation/${convId}`, function(data) {
        // ⭐ 3. MODIFIED: Use the new function to render restored chats
        renderChatHistory(data.messages);
      });
    });
  });
  
  $(document).on('click', '.delete-btn', function(e) {
    e.stopPropagation();
    const convId = $(this).closest('.conversation-item').data('id');
    if (activeConversationId === convId) {
        activeConversationId = null;
    }
    if (confirm("Delete this conversation permanently?")) {
      $.ajax({ url: `/conversation/${convId}/delete`, type: "DELETE" }).done(loadConversations);
    }
  });
  
  $(".new-chat-btn").on("click", function() {
    $.post("/clear", {}, function() {
      messagesContainer.empty();
      addMessage("👋 New conversation started. How can I help you today?");
      activeConversationId = null;
      loadConversations();
      isNewConversation = true;
    });
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
  
  loadConversations();
  messageInput.focus();
});

// Hamburger menu functionality
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