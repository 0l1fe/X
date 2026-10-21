// MathJax Configuration
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\(', '\\)']],
    displayMath: [['$$', '$$'], ['\\[', '\\]']]
  },
  svg: { fontCache: 'global' }
};

// Load MathJax asynchronously
(function loadMathJax() {
  const script = document.createElement('script');
  script.src = 'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js';
  script.async = true;
  document.head.appendChild(script);
})();

(async function main() {
  const feedContainer = document.getElementById('feed');
  const searchInput = document.getElementById('search-input');
  const searchCount = document.getElementById('search-count');

  let allItems = [];

  // ── 1. Fetch archived papers ───────────────────────────────────────────────
  let archiveItems = [];
  try {
    const res = await fetch('./archive.json');
    archiveItems = await res.json();
  } catch (_) {}

  // ── 2. Deduplicate by link and sort newest-first ────────────────────
  const seen = new Set();
  allItems = archiveItems.filter(item => {
    const key = item.link || item.guid || item.title;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((a, b) => new Date(b.pubDate) - new Date(a.pubDate));

  // ── 3. Initial render ────────────────────────────────────────────────────
  renderItems(allItems, feedContainer);

  // ── 4. Real-time search ──────────────────────────────────────────────────
  searchInput.addEventListener('input', () => {
    const query = searchInput.value.trim().toLowerCase();

    if (!query) {
      renderItems(allItems, feedContainer);
      return;
    }

    // Tokenise on whitespace so multi-word queries work as AND
    const tokens = query.split(/\s+/).filter(Boolean);

    const results = allItems.filter(item => {
      const haystack = [
        item.title || '',
        item.author || '',
        (item.categories || []).join(' '),
        item.description || '',
        item.contentSnippet || '',
        item.content || ''
      ].join(' ').toLowerCase();

      return tokens.every(token => haystack.includes(token));
    });

    renderItems(results, feedContainer);
  });
})();

// ── Helpers ──────────────────────────────────────────────────────────────────

function renderItems(items, container) {
  container.innerHTML = '';
  if (!items || items.length === 0) return;

  const fragment = document.createDocumentFragment();
  const dateOptions = {
    weekday: 'short',
    year: 'numeric',
    month: 'short',
    day: 'numeric'
  };

  items.forEach(item => {
    const article = document.createElement('article');
    article.className = 'card mb-4';

    const formattedDate = new Date(item.pubDate).toLocaleDateString('en-GB', dateOptions);
    const authors = item.author ? item.author : '';
    const subjects = (item.categories && item.categories.length > 0)
      ? item.categories.join(', ')
      : '';

    article.innerHTML = `
      <header class="card-header">
        <h2 class="card-title">
          <a href="${item.link}" target="_blank" rel="noopener noreferrer">${item.title}</a>
        </h2>
        <time class="card-meta">${formattedDate}</time>
      </header>
      <div class="card-body">
        <p>${item.contentSnippet || ''}</p>
      </div>
      <footer class="card-footer">
        ${authors ? `<small><strong>Authors:</strong> ${authors}</small><br>` : ''}
        ${subjects ? `<small><strong>Subjects:</strong> ${subjects}</small>` : ''}
      </footer>
    `;
    fragment.appendChild(article);
  });

  container.appendChild(fragment);

  if (window.MathJax?.typesetPromise) {
    window.MathJax.typesetPromise().catch(() => {});
  }
}
