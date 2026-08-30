/* ===========================================================================
   voice.js — dictation for the chat composer.

   Why this is its own file
   -----------------------
   chat.js is already the largest script in the app and owns streaming, image
   upload, feedback and escalation. Dictation touches exactly one element (the
   textarea) and needs no knowledge of any of that, so it stays out.

   Why the transcript is NOT auto-sent
   ----------------------------------
   The tempting behaviour is: stop talking -> send. It was rejected. Web Speech
   returns the wrong words often enough on Waray-accented English that
   auto-sending means the student watches a question they did not ask get
   answered, and their only recourse is to retype the whole thing. Worse, every
   such mistake is recorded by the analytics as a real question and, if the
   answer is a refusal, filed as a content gap — so a recogniser error becomes a
   line item on the admin's "documents to add" list.

   So the transcript lands in the textarea and the student presses send. One
   extra tap, and the words are theirs.

   The server repairs what the recogniser mangles (rag/dictation.py); this file
   deliberately does NOT pre-clean the text, because the student must be able to
   see and fix exactly what was heard.
   =========================================================================== */

(function () {
  'use strict';

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

  const input = document.getElementById('messageInput');
  const btn = document.getElementById('micBtn');
  if (!input || !btn) return;

  // Unsupported browser: remove the button rather than leave it to fail on tap.
  // Firefox and every in-app webview (Messenger, which is how a large share of
  // students open links) have no SpeechRecognition at all. A dead mic icon is
  // worse than no mic icon — it reads as "the app is broken".
  if (!SR) {
    btn.remove();
    return;
  }

  /* --- Secure context ---------------------------------------------------- *
   * Chrome exposes SpeechRecognition on insecure origins but refuses to run it,
   * so feature detection alone is not enough: over plain http://192.168.x.x the
   * object exists, start() succeeds, and the failure only arrives as an error
   * event. That produced the worst possible symptom — a mic that looks alive,
   * accepts a tap, then blames the student's connection.
   *
   * isSecureContext is true on https AND on localhost, which is exactly the set
   * of origins where dictation can work. Anywhere else the button is removed for
   * the same reason it is removed on Firefox: it cannot work here, and a control
   * that cannot work is worse than no control.
   *
   * The console line is for whoever is testing over the LAN and wondering where
   * the mic went — that person needs a reason, not a missing button.           */
  if (!window.isSecureContext) {
    console.warn(
      'Voice input disabled: speech recognition requires https:// (or ' +
      'localhost). This page was loaded over ' + window.location.protocol +
      '//' + window.location.hostname + '.'
    );
    btn.remove();
    return;
  }

  /* --- Brave -------------------------------------------------------------- *
   * Brave ships Chromium's SpeechRecognition object but not the Google Speech
   * API keys behind it — deliberately, because that API works by uploading audio
   * to Google. So on Brave the recogniser always fails with a bare 'network'
   * error even though permission is granted, the origin is secure and the
   * connection is fine. Nothing in the API tells you this; the object looks
   * perfectly healthy right up until it fails.
   *
   * isBrave() returns a promise, so this resolves a microtask after load rather
   * than synchronously. That is early enough — the button disappears before
   * anyone can tap it — and it is the only reliable detection Brave offers.
   *
   * The button goes for the same reason it goes on Firefox: it cannot work here.
   * The console message names the browsers that do, because "voice is broken" and
   * "voice needs a different browser" are very different problems to the person
   * reading it.                                                                */
  if (navigator.brave && typeof navigator.brave.isBrave === 'function') {
    navigator.brave.isBrave().then((isBrave) => {
      if (isBrave) {
        console.warn(
          'Voice input disabled: Brave does not include the Google speech ' +
          'service that the Web Speech API depends on. Use Chrome or Edge for ' +
          'dictation.'
        );
        btn.remove();
      }
    }).catch(() => { /* not Brave, or the check is unavailable */ });
  }

  /* --- Language ---------------------------------------------------------- *

   * en-PH, not en-US. Philippine English is a supported locale and its
   * acoustic model expects the local accent; en-US mishears "Samar" and every
   * unstressed final syllable. It also code-switches better, which matters
   * because the questions students actually speak are Taglish ("magkano ang
   * tuition sa BSIT") — and rag/language.py already translates those terms
   * server-side, so a half-Tagalog transcript is still answerable.
   *
   * en-US is a FALLBACK, not a second choice: some Chrome builds and Android
   * WebViews have no en-PH voice model provisioned and reject it with a bare
   * 'network' error, which is indistinguishable from a real outage. Retrying in
   * en-US recovers dictation completely for those users, at the cost of a worse
   * accent model — much better than a mic that never works.                    */
  const LOCALES = ['en-PH', 'en-US'];
  let localeIndex = 0;

  let recognition = null;
  let listening = false;
  // A 'network' error is retried exactly once (see onerror). Without a latch the
  // retry could re-fail and retry again, and a student holding a dead mic would
  // sit through an endless quiet loop with no message at all.
  let retried = false;

  // What the textarea held when recording started. Interim results are appended
  // to THIS, never to the live value, or the textarea would accumulate every
  // revision the recogniser makes as it changes its mind mid-sentence.
  let baseText = '';

  function setListening(on) {
    listening = on;
    btn.classList.toggle('listening', on);
    btn.title = on ? 'Stop dictating' : 'Ask by voice';
    btn.setAttribute('aria-label', btn.title);
    btn.innerHTML = on
      ? '<i class="fas fa-stop"></i>'
      : '<i class="fas fa-microphone"></i>';
  }

  function hint(message) {
    // Reuses the composer's own placeholder as the status line: it is already
    // in the student's field of view, and a toast would cover the send button
    // on a 360 px screen.
    if (!message) {
      input.placeholder = input.dataset.placeholder || 'Ask a question…';
      return;
    }
    if (!input.dataset.placeholder) {
      input.dataset.placeholder = input.placeholder;
    }
    input.placeholder = message;
  }

  function build() {
    const r = new SR();
    r.lang = LOCALES[localeIndex];

    // Interim results, so the student sees words appear as they speak. Without
    // them the screen is blank for the whole utterance and people stop and
    // retry, which is how you get half-questions.
    r.interimResults = true;
    r.continuous = false;   // one question per tap; ends on a natural pause
    r.maxAlternatives = 1;

    r.onstart = () => {
      baseText = input.value.trim();
      setListening(true);
      hint('Listening… speak your question');
    };

    r.onresult = (event) => {
      let finalText = '';
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const chunk = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalText += chunk;
        else interim += chunk;
      }
      const spoken = (finalText || interim).trim();
      if (!spoken) return;

      // Dictation demonstrably works, so re-arm the one-shot retry. A genuine
      // network drop an hour from now deserves its own second chance rather than
      // inheriting a latch set by an unrelated failure at page load.
      retried = false;


      input.value = baseText ? `${baseText} ${spoken}` : spoken;
      // Keep the auto-grow behaviour chat.js installed on 'input' — assigning
      // .value does not fire that event.
      input.dispatchEvent(new Event('input', { bubbles: true }));
    };

    r.onerror = (event) => {
      setListening(false);
      switch (event.error) {
        case 'not-allowed':
        case 'service-not-allowed':
          // Permission is remembered by the browser, so re-tapping will fail
          // identically until they change it in site settings. Say so.
          hint('Microphone blocked — allow it in your browser settings');
          break;
        case 'no-speech':
          hint('I did not hear anything — tap the mic and try again');
          break;
        case 'network':
          // 'network' is Chrome's catch-all, and taking it literally was a bug:
          // the page had just loaded, so "check your connection" was visibly
          // false and sent students looking for a problem that was not theirs.
          // The common real cause is a missing voice model for the requested
          // locale, which reports identically.
          //
          // So: retry once in en-US before saying anything. If that works the
          // student never sees an error at all.
          if (!retried && localeIndex + 1 < LOCALES.length) {
            retried = true;
            localeIndex += 1;
            recognition = build();
            hint('Starting the microphone…');
            try {
              recognition.start();
              return;   // no message; the retry owns the outcome
            } catch (e) {
              console.debug('locale retry failed:', e);
            }
          }
          // Only now is it worth mentioning the network — and even then, only
          // when the browser agrees we are offline. Otherwise it is the voice
          // SERVICE that is unreachable, not the student's internet, and saying
          // so stops them from restarting a working router.
          //
          // The online wording names Chrome, because by this point every cause we
          // can act on has been ruled out: permission granted, secure origin,
          // both locales tried, browser reports itself online. What is left is
          // overwhelmingly a Chromium build with the Google speech service
          // stripped out — Brave, and the privacy forks isBrave() cannot detect.
          // "Unavailable" alone would be true and useless; the student has no way
          // to guess that the fix is a different browser.
          hint(navigator.onLine
            ? 'Voice not supported in this browser — try Chrome, or type instead'
            : 'Voice needs a connection — type your question instead');
          break;


        case 'aborted':
          hint('');   // the student stopped it on purpose
          break;
        default:
          hint('Voice input failed — type your question instead');
      }
      setTimeout(() => hint(''), 4000);
    };

    r.onend = () => {
      setListening(false);
      if (input.value.trim()) {
        // Do NOT submit. Leave it focused so the student can correct a misheard
        // word before sending — see the header comment.
        hint('');
        input.focus();
      }
    };

    return r;
  }

  btn.addEventListener('click', () => {
    if (listening) {
      // stop() lets the final result arrive; abort() would discard the words
      // already spoken, which is the opposite of what a stop button means.
      try { recognition && recognition.stop(); } catch (e) { /* already stopped */ }
      return;
    }
    if (!recognition) recognition = build();
    try {
      recognition.start();
    } catch (e) {
      // start() throws InvalidStateError if called while already starting —
      // double-tap on a slow phone. Not an error worth showing.
      console.debug('speech start ignored:', e);
    }
  });

  // Sending while the mic is live would submit a half-sentence and leave the
  // recogniser running into the next question.
  const form = document.getElementById('messageForm');
  if (form) {
    form.addEventListener('submit', () => {
      if (listening) {
        try { recognition.abort(); } catch (e) { /* no-op */ }
        setListening(false);
      }
    }, true);
  }
})();
