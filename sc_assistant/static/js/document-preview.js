// Read the app's theme without loading chat scripts or external resources.
try {
  if (localStorage.getItem('theme') === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
} catch (error) {
  // The preview remains readable when browser storage is disabled.
}

document.addEventListener('DOMContentLoaded', function() {
  const content = document.querySelector('.preview-content');
  const status = document.getElementById('citation-location');
  if (!content || !status) return;

  function unavailable(message) {
    status.textContent = message;
    status.scrollIntoView({block: 'nearest', behavior: 'instant'});
  }

  function locatePassage() {
    content.querySelectorAll('mark.citation-highlight').forEach(function(mark) {
      mark.replaceWith(document.createTextNode(mark.textContent));
    });
    content.normalize();
    status.hidden = true;
    status.textContent = '';

    const params = new URLSearchParams(window.location.hash.slice(1));
    const quote = (params.get('quote') || '').trim();
    if (!params.has('quote') && !params.has('citation')) return;

    status.hidden = false;
    if (quote.length < 8 || quote.length > 300) {
      unavailable('This citation has no usable passage location. The document is open for reading.');
      return;
    }

    // Only source text is searched, not our headings, table captions or notices.
    // Keep offsets into real text nodes so highlighting never parses source HTML.
    const nodes = [];
    let text = '';
    content.querySelectorAll('p, li, h2, h3, h4, h5, h6').forEach(function(block) {
      if (text) text += '\n';
      const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const start = text.length;
        text += node.textContent;
        nodes.push({node, start, end: text.length});
      }
    });

    const pattern = quote.split(/\s+/u).map(function(word) {
      return word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('\\s+');
    const leading = /^[\p{L}\p{N}_]/u.test(quote) ? '(?<![\\p{L}\\p{N}_])' : '';
    const trailing = /[\p{L}\p{N}_]$/u.test(quote) ? '(?![\\p{L}\\p{N}_])' : '';
    const search = new RegExp(leading + pattern + trailing, 'giu');
    const match = search.exec(text);
    if (!match) {
      unavailable('The cited text was not found in this preview. The document may have changed, or the text may be inside an image. Download the original to check it.');
      return;
    }
    // A repeated phrase is not an exact destination. Never silently pick one.
    search.lastIndex = match.index + 1;
    if (search.exec(text)) {
      unavailable('The cited text appears more than once, so an exact passage could not be selected. The document is open for reading.');
      return;
    }

    const end = match.index + match[0].length;
    let firstMark;
    nodes.forEach(function(part) {
      const from = Math.max(match.index, part.start);
      const to = Math.min(end, part.end);
      if (from >= to) return;
      const selected = part.node.splitText(from - part.start);
      selected.splitText(to - from);
      const mark = document.createElement('mark');
      mark.className = 'citation-highlight';
      selected.replaceWith(mark);
      mark.appendChild(selected);
      if (!firstMark) firstMark = mark;
    });

    status.textContent = 'The cited passage is highlighted in the document.';
    if (firstMark) {
      firstMark.tabIndex = -1;
      firstMark.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
      firstMark.focus({preventScroll: true});
    }
  }

  // Wait for styles and the brand image before measuring the destination.
  if (document.readyState === 'complete') locatePassage();
  else window.addEventListener('load', locatePassage, {once: true});
  window.addEventListener('hashchange', locatePassage);
});
