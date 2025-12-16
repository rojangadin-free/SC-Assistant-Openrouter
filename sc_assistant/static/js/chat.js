/*
 * =========================================
 * CHAT-SPECIFIC JAVASCRIPT (chat.js)
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

  // --- Image Upload Elements ---
  const imageInput = $('#imageInput');
  const uploadBtn = $('#uploadBtn');
  const previewContainer = $('#imagePreviewContainer');
  const previewImg = $('#imagePreview');
  const removeImageBtn = $('#removeImageBtn');

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


  // --- Image Upload Handlers ---
  uploadBtn.on('click', function() {
    imageInput.click();
  });

  imageInput.on('change', function() {
    if (this.files && this.files[0]) {
        const reader = new FileReader();
        reader.onload = function(e) {
            previewImg.attr('src', e.target.result);
            previewContainer.css('display', 'flex'); // Show preview
        }
        reader.readAsDataURL(this.files[0]);
    }
  });

  removeImageBtn.on('click', function() {
    imageInput.val('');
    previewContainer.hide();
  });


  // --- Chat rendering helpers ---
  function addMessage(content, isUser = false, autoScroll = true, hasImage = false) {
    let processedContent = '';
    
    if (isUser) {
       // Sanitize user input
       processedContent = content.replace(/</g, '&lt;').replace(/>/g, '&gt;');
       if (hasImage) {
           // Add a clean badge for the image
           processedContent += ' <br><span style="font-size:0.85em; color:var(--text-secondary); display:inline-flex; align-items:center; margin-top:5px;"><i class="fas fa-paperclip" style="margin-right:4px;"></i> Image Attached</span>';
       }
    } else {
       // Markdown for assistant
       processedContent = marked.parse(content);
    }

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
      
      // Clean up history tags
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

  // --- Chat Send Logic ---
  function sendMessage(message, imageFile) {
    const requestIsNew = isNewConversation;
    const requestConvId = activeConversationId;

    typingIndicator.removeClass('fade-out').show();

    // Prepare FormData
    const formData = new FormData();
    formData.append("msg", message);
    if (imageFile) {
        formData.append("image", imageFile);
    }

    $.ajax({
      url: '/chat/get',
      type: 'POST',
      data: formData,
      processData: false,
      contentType: false,
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
          addMessage('An error occurred.');
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
          messageInput.prop('disabled', false);
          
          if (window.innerWidth > 768) {
            messageInput.focus();
          }
        }
      });
  }

  // --- Submit Handler ---
  messageForm.on('submit', function(e) {
    e.preventDefault();
    const message = messageInput.val().trim();
    
    // 1. Get file immediately
    const hasImage = imageInput[0].files.length > 0;
    const imageFile = hasImage ? imageInput[0].files[0] : null;

    if (!message && !hasImage) return;

    // 2. Add message to UI immediately
    addMessage(message, true, true, hasImage);
    
    // 3. Clear Inputs & Preview IMMEDIATELY
    messageInput.val('').css('height', 'auto');
    if (hasImage) {
        imageInput.val(''); // Clear file input
        previewContainer.hide(); // Hide preview box
    }
    
    // 4. Disable controls
    sendButton.prop('disabled', true);
    messageInput.prop('disabled', true);

    // 5. Send
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

    messagesContainer.empty();
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
      messagesContainer.empty();
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