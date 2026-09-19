// galaxypron - Edge AI Agent content script
// Runs on pages to capture user activity signals for learning

// Track time spent reading
let pageLoadTime = Date.now();
let isActive = true;
let scrollDepth = 0;
let maxScrollDepth = 0;

// Report page interactions to the background script
function reportActivity(type, value) {
  chrome.runtime.sendMessage({
    type: 'activity',
    activity_type: type,
    value: value,
    timestamp: new Date().toISOString()
  });
}

// Track visibility changes (tab focus / hidden)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    isActive = true;
    pageLoadTime = Date.now();
    reportActivity('tab_visible', window.location.href);
  } else {
    isActive = false;
    const timeSpent = (Date.now() - pageLoadTime) / 1000;
    reportActivity('tab_hidden', Math.round(timeSpent));
  }
});

// Track scroll depth
let scrollTimeout;
window.addEventListener('scroll', () => {
  clearTimeout(scrollTimeout);
  scrollTimeout = setTimeout(() => {
    const doc = document.documentElement;
    const total = doc.scrollHeight - window.innerHeight;
    const current = window.scrollY;
    scrollDepth = total > 0 ? Math.round((current / total) * 100) : 0;
    
    if (scrollDepth > maxScrollDepth) {
      maxScrollDepth = scrollDepth;
      reportActivity('scroll', `${maxScrollDepth}%`);
    }
  }, 1000);
});

// Track clicks
document.addEventListener('click', (e) => {
  const target = e.target.closest('a, button');
  if (target) {
    reportActivity('click', {
      tag: target.tagName,
      text: (target.textContent || '').trim().slice(0, 100),
      href: target.href || ''
    });
  }
});

// Track search queries (for search engines)
const searchInput = document.querySelector('input[type="search"], input[type="text"]');
if (searchInput && /google|bing|duckduckgo|search/i.test(window.location.hostname)) {
  const form = searchInput.closest('form');
  if (form) {
    form.addEventListener('submit', () => {
      reportActivity('search', searchInput.value);
    });
  }
}

// Reading time estimation
const articleElement = document.querySelector('article, [role="main"], .content, .post, .article');
if (articleElement) {
  const text = articleElement.textContent || '';
  const wordCount = text.split(/\s+/).length;
  if (wordCount > 100) {
    reportActivity('article_view', {
      word_count: wordCount,
      estimated_read_minutes: Math.max(1, Math.round(wordCount / 200))
    });
  }
}

// Private browsing flag
let isPrivate = false;
try {
  if (window.chrome && (chrome.extension || window.webkitRequestFileSystem)) {
    isPrivate = false;
  }
} catch (e) {
  isPrivate = true;
}

// Report page load completion
setTimeout(() => {
  reportActivity('page_load', {
    url: window.location.href,
    title: document.title,
    read_time_seconds: Math.round((Date.now() - pageLoadTime) / 1000),
    content_length: document.body?.innerHTML?.length || 0
  });
}, 3000);

console.log('[galaxypron] Content script loaded on', window.location.hostname);
