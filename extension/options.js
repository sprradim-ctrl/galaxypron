document.getElementById('save').addEventListener('click', async () => {
  const url = document.getElementById('server-url').value.trim();
  await chrome.storage.local.set({ galaxypron_server: url || 'http://localhost:5000' });
  const msg = document.getElementById('saved-msg');
  msg.style.display = 'block';
  setTimeout(() => msg.style.display = 'none', 2000);
});

document.addEventListener('DOMContentLoaded', async () => {
  const result = await chrome.storage.local.get('galaxypron_server');
  document.getElementById('server-url').value = result.galaxypron_server || 'http://localhost:5000';
});
