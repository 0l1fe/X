// MathJax configuration (keeps the site's existing mathematical typesetting).
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\(', '\\)']],
    displayMath: [['$$', '$$'], ['\\[', '\\]']]
  },
  svg: { fontCache: 'global' }
};

(function loadMathJax() {
  const script = document.createElement('script');
  script.src = 'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js';
  script.async = true;
  document.head.appendChild(script);
})();

(async function main() {
  const feedContainer = document.getElementById('feed');
  const searchInput = document.getElementById('search-input');
  if (!feedContainer || !searchInput) return;

  let allItems;
  try {
    // One local data source. No browser CORS proxy or rss2json.com dependency.
    const response = await fetch('./archive.json', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    allItems = await response.json();
    if (!Array.isArray(allItems)) throw new Error('Invalid archive format');
  } catch (error) {
    console.error('Unable to load archive.json:', error);
    feedContainer.textContent = 'Unable to load the paper archive. Please try again later.';
    return;
  }

  allItems.sort((a, b) => Date.parse(b.pubDate) - Date.parse(a.pubDate));
  renderItems(allItems, feedContainer);

  searchInput.addEventListener('input', () => {
    const tokens = searchInput.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const found = !tokens.length ? allItems : allItems.filter(item => {
      const haystack = [
        item.title || '',
        item.author || '',
        ...(Array.isArray(item.categories) ? item.categories : []),
        item.description || '',
        item.contentSnippet || '',
        item.content || ''
      ].join(' ').toLowerCase();
      return tokens.every(token => haystack.includes(token));
    });
    renderItems(found, feedContainer);
  });
})();

// The archive's original rss2json records don't have contentSnippet; extract
// the abstract from description/content instead of showing empty cards.
function abstractText(item) {
  const value = String(item.contentSnippet || item.description || item.content || '');
  return value.replace(/^arXiv:\S+\s+(?:Announce Type:\s*\S+\s+)?Abstract:\s*/i, '').trim();
}

function renderItems(items, container) {
  container.replaceChildren();
  const fragment = document.createDocumentFragment();
  const dateOptions = { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' };

  items.forEach(item => {
    const article = document.createElement('article');
    article.className = 'card mb-4';
    const header = document.createElement('header');
    header.className = 'card-header';
    const heading = document.createElement('h2');
    heading.className = 'card-title';
    const anchor = document.createElement('a');
    anchor.href = item.link || '#';
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    anchor.textContent = item.title || 'Untitled paper';
    heading.appendChild(anchor);
    header.appendChild(heading);

    const date = document.createElement('time');
    date.className = 'card-meta';
    const timestamp = Date.parse(String(item.pubDate || '').replace(' ', 'T') +
      (/Z$|[+-]\d\d:?\d\d$/.test(String(item.pubDate || '')) ? '' : 'Z'));
    date.textContent = Number.isFinite(timestamp)
      ? new Date(timestamp).toLocaleDateString('en-GB', dateOptions)
      : String(item.pubDate || '');
    header.appendChild(date);
    article.appendChild(header);

    const body = document.createElement('div');
    body.className = 'card-body';
    const summary = document.createElement('p');
    summary.textContent = abstractText(item);
    body.appendChild(summary);
    article.appendChild(body);

    const footer = document.createElement('footer');
    footer.className = 'card-footer';
    const authors = item.author || '';
    const categories = Array.isArray(item.categories) ? item.categories.join(', ') : '';
    if (authors) {
      const authorInfo = document.createElement('small');
      const label = document.createElement('strong');
      label.textContent = 'Authors: ';
      authorInfo.append(label, document.createTextNode(authors));
      footer.append(authorInfo, document.createElement('br'));
    }
    if (categories) {
      const categoryInfo = document.createElement('small');
      const label = document.createElement('strong');
      label.textContent = 'Subjects: ';
      categoryInfo.append(label, document.createTextNode(categories));
      footer.appendChild(categoryInfo);
    }
    article.appendChild(footer);
    fragment.appendChild(article);
  });

  container.appendChild(fragment);
  if (window.MathJax?.typesetPromise) {
    window.MathJax.typesetPromise().catch(err => console.warn('MathJax:', err));
  }
}
