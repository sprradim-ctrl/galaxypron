// galaxypron - Edge AI Agent background service worker

const SERVER_URL = 'http://localhost:5000';
const socket = null;

// Track active tab timing
let currentTab = null;
let tabStartTime = Date.now();

// Initialize on install
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    galaxypron_enabled: true,
    galaxypron_server: SERVER_URL,
    galaxypron_total_events: 0
  });
  console.log('[galaxypron] Extension installed. AI agent is ready to learn.');
});

// Function to send data to the local server
async function sendToServer(data) {
  try {
    const settings = await chrome.storage.local.get('galaxypron_server');
    const serverUrl = settings.galaxypron_server || SERVER_URL;
    
    const response = await fetch(`${serverUrl}/api/browser-event`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(data)
    });
    
    if (response.ok) {
      chrome.storage.local.get('galaxypron_total_events', (data2) => {
        const count = (data2.galaxypron_total_events || 0) + 1;
        chrome.storage.local.set({ galaxypron_total_events: count });
      });
    }
  } catch (err) {
    console.log('[galaxypron] Server not reachable, storing locally:', err.message);
    queueOfflineData(data);
  }
}

// Queue data when server is offline
async function queueOfflineData(data) {
  const result = await chrome.storage.local.get('galaxypron_offline_queue');
  const queue = result.galaxypron_offline_queue || [];
  queue.push({ ...data, timestamp: new Date().toISOString() });
  if (queue.length > 200) queue.splice(0, queue.length - 200);
  chrome.storage.local.set({ galaxypron_offline_queue: queue });
}

// Flush offline queue when server comes back
async function flushQueue() {
  const result = await chrome.storage.local.get('galaxypron_offline_queue');
  const queue = result.galaxypron_offline_queue || [];
  if (queue.length === 0) return;
  
  try {
    const settings = await chrome.storage.local.get('galaxypron_server');
    const serverUrl = settings.galaxypron_server || SERVER_URL;
    
    for (const item of queue) {
      await fetch(`${serverUrl}/api/browser-event`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item)
      });
    }
    await chrome.storage.local.set({ galaxypron_offline_queue: [] });
    console.log('[galaxypron] Flushed offline queue:', queue.length);
  } catch (err) {
    // Still offline, try again later
  }
}

// Retry flush every 30 seconds
setInterval(flushQueue, 30000);

// Track tab navigation
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url && tab.url.startsWith('http')) {
    const duration = (Date.now() - tabStartTime) / 1000;
    
    sendToServer({
      type: 'navigation',
      url: tab.url,
      title: tab.title || '',
      tabId: tabId,
      duration: Math.round(duration)
    });
    
    currentTab = tab;
    tabStartTime = Date.now();
  }
});

// Track tab activation (switching between tabs)
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  const tab = await chrome.tabs.get(activeInfo.tabId);
  if (tab && tab.url && tab.url.startsWith('http')) {
    const duration = (Date.now() - tabStartTime) / 1000;
    
    sendToServer({
      type: 'tab_switch',
      url: tab.url,
      title: tab.title || '',
      tabId: activeInfo.tabId,
      duration: Math.round(duration)
    });
    tabStartTime = Date.now();
  }
});

// Get all open tabs periodically
async function reportTabs() {
  const tabs = await chrome.tabs.query({});
  const tabInfo = tabs
    .filter(t => t.url && t.url.startsWith('http'))
    .map(t => ({
      url: t.url,
      title: t.title || '',
      id: t.id
    }));
  
  sendToServer({
    type: 'tabs',
    tabs: tabInfo,
    count: tabInfo.length
  });
}

// Report tabs every minute
setInterval(reportTabs, 60000);

// Handle messages from content script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type) {
    sendToServer({
      ...message,
      url: sender.tab?.url,
      title: sender.tab?.title,
      tabId: sender.tab?.id
    });
    sendResponse({ status: 'received' });
    return true;
  }
  return false;
});

// Browser startup
chrome.runtime.onStartup.addListener(() => {
  console.log('[galaxypron] Edge started. Agent is learning.');
  sendToServer({ type: 'browser_start', timestamp: new Date().toISOString() });
});

console.log('[galaxypron] Background service worker loaded.');
