// galaxypron - Edge AI Agent popup

const SERVER_URL = 'http://localhost:5000';

function updateStatus(online) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  dot.classList.toggle('online', online);
  text.textContent = online ? 'Connected to server' : 'Server offline';
}

async function checkServer() {
  try {
    const res = await fetch(`${SERVER_URL}/api/status`, { timeout: 3000 });
    if (res.ok) {
      const data = await res.json();
      updateStatus(true);
      if (data.learning) {
        document.getElementById('stat-learned').textContent = data.learning.patterns_learned || 0;
      }
    } else {
      updateStatus(false);
    }
  } catch {
    updateStatus(false);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  // Load event count
  const result = await chrome.storage.local.get(['galaxypron_total_events', 'galaxypron_enabled']);
  document.getElementById('stat-events').textContent = result.galaxypron_total_events || 0;

  const enabled = result.galaxypron_enabled !== false;
  const toggleBtn = document.getElementById('toggle-btn');
  toggleBtn.classList.toggle('inactive', !enabled);
  toggleBtn.textContent = enabled ? 'Learning Active' : 'Learning Paused';

  checkServer();
  setInterval(checkServer, 5000);

  // Toggle learning
  toggleBtn.addEventListener('click', async () => {
    const current = (await chrome.storage.local.get('galaxypron_enabled')).galaxypron_enabled !== false;
    await chrome.storage.local.set({ galaxypron_enabled: !current });
    toggleBtn.classList.toggle('inactive', current);
    toggleBtn.textContent = current ? 'Learning Paused' : 'Learning Active';
  });

  // Open dashboard
  document.getElementById('open-dashboard').addEventListener('click', () => {
    chrome.tabs.create({ url: `${SERVER_URL}/` });
  });
});
