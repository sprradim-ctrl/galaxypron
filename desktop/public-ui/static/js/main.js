document.addEventListener('DOMContentLoaded', () => {
    // Socket.io is optional: if the client script is missing/not served, we
    // fall back to HTTP polling so the app still works fully (chat, learning,
    // everything). This prevents a missing socket from killing the UI.
    const socket = (typeof io !== 'undefined')
        ? io()
        : { on: () => {}, emit: () => {}, connected: false };
    (typeof io === 'undefined') && refreshStatus();

    const chatMessages = document.getElementById('chat-messages');
    const chatScroll = document.getElementById('chat-scroll');
    const chatInput = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');
    const newChatBtn = document.getElementById('new-chat-btn');
    const sidebar = document.getElementById('sidebar');
    const welcomeScreen = document.getElementById('welcome-screen');
    const statusDot = document.getElementById('status-dot');
    const agentStatus = document.getElementById('agent-status');

    const userName = 'You';

    // Sidebar toggle
    document.querySelectorAll('#sidebar-toggle').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            sidebar.classList.toggle('collapsed');
        });
    });

    // Start with the drawer closed on small screens (it overlays the chat).
    if (window.innerWidth <= 640) sidebar.classList.add('collapsed');

    // Auto-resize textarea
    chatInput.addEventListener('input', () => {
        chatInput.style.height = 'auto';
        chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + 'px';
    });

    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Suggestion buttons
    document.querySelectorAll('.suggestion-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            chatInput.value = btn.dataset.suggestion;
            sendMessage();
        });
    });

    // Navigation
    function switchView(view) {
        document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
        const navItem = document.querySelector(`.nav-item[data-view="${view}"]`);
        if (navItem) navItem.classList.add('active');

        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        document.getElementById(`view-${view}`).classList.add('active');

        // On phones the drawer overlays the content — close it after any
        // navigation so it never stays stuck covering the screen.
        if (window.innerWidth <= 640) sidebar.classList.add('collapsed');

        if (view === 'learning') loadLearning();
        if (view === 'settings') loadSettings();
    }

    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            if (item.dataset.view) switchView(item.dataset.view);
        });
    });

    // Open the phone/LAN stats page in a new tab
    const mobileBtn = document.getElementById('mobile-btn');
    if (mobileBtn) mobileBtn.addEventListener('click', (e) => {
        e.preventDefault();
        window.open('/mobile', '_blank');
    });

    // Topbar toggle buttons that go back to chat
    document.querySelectorAll('[data-view="chat"]').forEach(btn => {
        btn.addEventListener('click', () => switchView('chat'));
    });

    // Socket.io
    socket.on('connect', () => {
        statusDot.classList.add('online');
        agentStatus.textContent = 'Online';
        refreshStatus();
    });

    socket.on('disconnect', () => {
        statusDot.classList.remove('online');
        agentStatus.textContent = 'Connecting...';
    });

    // Lightweight markdown renderer (bold, lists, line breaks)
    function renderMarkdown(text) {
        let html = escapeHtml(text);
        html = html.replace(/^• (.*)$/gm, '<li>$1</li>');
        html = html.replace(/(?:<li>.*?<\/li>\n?)+/g, '<ul>$&</ul>');
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        // Render generated media (PNG images + GIF videos)
        html = html.replace(/(\/generated\/[\w.\-]+\.(?:png|jpg|gif))/g,
            (m, url) => {
                if (/\.gif$/i.test(url)) return '<video src="' + url + '" autoplay loop muted controls style="max-width:320px;border-radius:10px;margin:8px 0;display:block"></video>';
                return '<img src="' + url + '" style="max-width:320px;border-radius:10px;margin:8px 0;display:block">';
            });
        html = html.replace(/\n/g, '<br>');
        return html;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Message rendering
    function addMessage(text, sender) {
        welcomeScreen?.remove();
        const msg = document.createElement('div');
        msg.className = `message ${sender}`;

        if (sender === 'bot') {
            const avatar = document.createElement('div');
            avatar.className = 'message-avatar';
            avatar.textContent = '🪐';

            const content = document.createElement('div');
            content.className = 'message-content';
            content.innerHTML = renderMarkdown(text);

            msg.appendChild(avatar);
            msg.appendChild(content);
        } else {
            const bubble = document.createElement('div');
            bubble.className = 'user-bubble';
            bubble.textContent = text;
            msg.appendChild(bubble);
        }

        chatMessages.appendChild(msg);
        chatScroll.scrollTop = chatScroll.scrollHeight;
    }

    function showTyping() {
        const typing = document.createElement('div');
        typing.className = 'message bot';
        typing.id = 'typing-msg';
        typing.innerHTML = '<div class="message-avatar">🪐</div><div class="typing-indicator"><span></span><span></span><span></span></div>';
        chatMessages.appendChild(typing);
        chatScroll.scrollTop = chatScroll.scrollHeight;
    }

    function hideTyping() {
        document.getElementById('typing-msg')?.remove();
    }

    async function sendMessage() {
        const message = chatInput.value.trim();
        if (!message) return;

        addMessage(message, 'user');
        chatInput.value = '';
        chatInput.style.height = 'auto';

        showTyping();

        try {
            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message })
            });
            const data = await response.json();
            hideTyping();
            setTimeout(() => addMessage(data.text, 'bot'), 120);
        } catch (err) {
            hideTyping();
            addMessage('Connection error. Is the server running?', 'bot');
        }
    }

    sendBtn.addEventListener('click', sendMessage);

    function showWelcome() {
        chatMessages.innerHTML = '';
        const welcome = document.createElement('div');
        welcome.className = 'welcome-screen';
        welcome.id = 'welcome-screen';
        welcome.innerHTML = `
            <div class="welcome-logo">🪐</div>
            <h1>How can I help you today?</h1>
            <p>Ask galaxypron anything about your browsing, or let it train on your Microsoft Edge habits.</p>
            <div class="suggestion-grid">
                <button class="suggestion-btn" data-suggestion="What have you learned about my browsing?">What have you learned about my browsing?</button>
                <button class="suggestion-btn" data-suggestion="What are my most visited websites?">What are my most visited websites?</button>
                <button class="suggestion-btn" data-suggestion="Where do I spend the most time?">Where do I spend the most time?</button>
                <button class="suggestion-btn" data-suggestion="Suggest websites based on my interests">Suggest websites based on my interests</button>
            </div>
        `;
        chatMessages.appendChild(welcome);
        welcome.querySelectorAll('.suggestion-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                chatInput.value = btn.dataset.suggestion;
                sendMessage();
            });
        });
    }

    newChatBtn.addEventListener('click', showWelcome);
    showWelcome();

    // Shared renders for the Learning view (top websites + preferences)
    function renderTopDomains(stats) {
        const topDomains = document.getElementById('top-domains');
        if (!topDomains) return;
        const domains = (stats.top_domains || []);
        topDomains.innerHTML = '';
        if (domains.length === 0) {
            topDomains.innerHTML = '<div class="empty-state">No browsing data yet. Use Edge with the extension installed.</div>';
        } else {
            domains.forEach(d => {
                const item = document.createElement('div');
                item.className = 'domain-item';
                item.innerHTML = `<span>${escapeHtml(d.domain)}</span><span class="visits">${d.visits} visits</span>`;
                topDomains.appendChild(item);
            });
        }
    }

    function renderPreferences(stats) {
        const prefs = document.getElementById('preferences');
        if (!prefs) return;
        prefs.innerHTML = '';
        const preferences = stats.preferences || {};
        const prefKeys = Object.keys(preferences);
        if (prefKeys.length === 0) {
            prefs.innerHTML = '<div class="empty-state">No preferences detected yet.</div>';
        } else {
            const wrap = document.createElement('div');
            wrap.className = 'preference-wrap';
            prefKeys.forEach(cat => {
                const chip = document.createElement('span');
                chip.className = 'preference-chip';
                chip.textContent = `${cat} · ${preferences[cat].count}`;
                wrap.appendChild(chip);
            });
            prefs.appendChild(wrap);
        }
    }

    // Learning insights (recent activity, browsing patterns, top sites, prefs)
    function loadLearning() {
        fetch('/api/learning/data')
            .then(r => r.json())
            .then(data => {
                renderTopDomains(data);
                renderPreferences(data);

                const activity = document.getElementById('recent-activity');
                activity.innerHTML = '';

                const visits = (data.recent_activity || []);
                if (visits.length === 0) {
                    activity.innerHTML = '<div class="empty-state">No recent activity yet.</div>';
                } else {
                    visits.slice(-10).reverse().forEach(v => {
                        const item = document.createElement('div');
                        item.className = 'activity-item';
                        item.textContent = `${formatTime(v.timestamp)} · ${v.title || v.url || 'Unknown'}`;
                        activity.appendChild(item);
                    });
                }

                const patterns = document.getElementById('learning-patterns');
                patterns.innerHTML = '';
                if (visits.length === 0) {
                    patterns.innerHTML = '<div class="empty-state">No patterns learned yet. Keep browsing!</div>';
                } else {
                    const hours = {};
                    visits.forEach(v => {
                        const h = (v.timestamp || '').slice(11, 13) + ':00';
                        hours[h] = (hours[h] || 0) + 1;
                    });
                    Object.entries(hours).sort().forEach(([hour, count]) => {
                        const row = document.createElement('div');
                        row.className = 'learning-row';
                        row.innerHTML = `<span>${hour}</span><span>${count} visits</span>`;
                        patterns.appendChild(row);
                    });
                }
            });
    }

    // Weekly Brain Report: what the agent learned about the user this week.
    function renderWeeklyReport() {
        fetch('/api/report/weekly')
            .then(r => r.json())
            .then(report => {
                const el = document.getElementById('weekly-report');
                if (!el) return;
                el.innerHTML = '';

                const narrative = document.createElement('div');
                narrative.className = 'report-narrative';
                narrative.innerHTML = escapeHtml(report.narrative || 'Not enough activity this week yet — keep browsing!');
                el.appendChild(narrative);

                const wrap = document.createElement('div');
                wrap.className = 'report-grid';

                const domBlock = document.createElement('div');
                domBlock.className = 'report-block';
                domBlock.innerHTML = '<h4>Top sites this week</h4>';
                const doms = report.top_domains || [];
                if (doms.length === 0) {
                    domBlock.innerHTML += '<div class="empty-state">No browsing recorded this week.</div>';
                } else {
                    const mx = Math.max(1, ...doms.map(d => d.count));
                    doms.forEach(d => {
                        const row = document.createElement('div');
                        row.className = 'report-bar-row';
                        row.innerHTML = `
                            <div class="report-bar-head"><span>${escapeHtml(d.label)}</span><span>${d.count} visits</span></div>
                            <div class="report-bar"><div style="width:${(d.count / mx) * 100}%"></div></div>`;
                        domBlock.appendChild(row);
                    });
                }
                wrap.appendChild(domBlock);

                const catBlock = document.createElement('div');
                catBlock.className = 'report-block';
                catBlock.innerHTML = '<h4>Interests this week</h4>';
                const cats = report.categories || [];
                if (cats.length === 0) {
                    catBlock.innerHTML += '<div class="empty-state">Still forming…</div>';
                } else {
                    const cw = document.createElement('div');
                    cw.className = 'preference-wrap';
                    cats.forEach(c => {
                        const chip = document.createElement('span');
                        chip.className = 'preference-chip';
                        chip.textContent = `${c.label} · ${c.count}`;
                        cw.appendChild(chip);
                    });
                    catBlock.appendChild(cw);
                }
                wrap.appendChild(catBlock);

                const dayBlock = document.createElement('div');
                dayBlock.className = 'report-block';
                dayBlock.innerHTML = '<h4>Busiest days</h4>';
                const days = report.weekdays || [];
                if (days.length === 0) {
                    dayBlock.innerHTML += '<div class="empty-state">No visits to chart.</div>';
                } else {
                    const dmx = Math.max(1, ...days.map(d => d.count));
                    const dw = document.createElement('div');
                    dw.className = 'report-days';
                    days.forEach(d => {
                        const col = document.createElement('div');
                        col.className = 'report-day';
                        col.innerHTML = `
                            <div class="report-day-bar"><div style="height:${(d.count / dmx) * 100}%"></div></div>
                            <span>${escapeHtml(d.label.slice(0, 3))}</span>
                            <b>${d.count}</b>`;
                        dw.appendChild(col);
                    });
                    dayBlock.appendChild(dw);
                }
                wrap.appendChild(dayBlock);

                el.appendChild(wrap);

                const t = report.trainer || {};
                const loss = t.last_loss != null ? Number(t.last_loss).toFixed(4) : '—';
                const words = t.top_words || [];
                const info = document.createElement('div');
                info.className = 'report-trainer';
                info.innerHTML = `
                    <div class="learning-row"><span>Brain activity</span><span>${t.running ? 'Training now' : 'Paused'}</span></div>
                    <div class="learning-row"><span>Device</span><span>${escapeHtml(t.device || 'cpu')}</span></div>
                    <div class="learning-row"><span>Wikipedia articles</span><span>${(t.articles_ingested || 0).toLocaleString()}</span></div>
                    <div class="learning-row"><span>Words known</span><span>${(t.vocabulary_size || 0).toLocaleString()}</span></div>
                    <div class="learning-row"><span>Avg loss</span><span>${loss}</span></div>
                    ${words.length ? `<div class="learning-row"><span>Current concepts</span><span>${escapeHtml(words.join(', '))}</span></div>` : ''}`;
                el.appendChild(info);
            })
            .catch(() => {
                const el = document.getElementById('weekly-report');
                if (el) el.innerHTML = '<div class="empty-state">Could not load the weekly report.</div>';
            });
    }
    setInterval(renderWeeklyReport, 60000);

    // Settings
    function loadSettings() {
        fetch('/api/config')
            .then(r => r.json())
            .then(config => {
                document.getElementById('agent-name').value = config.name || 'galaxypron';
                document.getElementById('personality').value = config.personality || 'helpful';
                document.getElementById('learning-enabled').checked = config.learning_enabled !== false;
            });
    }

    document.getElementById('save-settings').addEventListener('click', () => {
        const config = {
            name: document.getElementById('agent-name').value,
            personality: document.getElementById('personality').value,
            learning_enabled: document.getElementById('learning-enabled').checked
        };
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        })
        .then(r => r.json())
        .then(() => {
            agentStatus.textContent = document.getElementById('agent-name').value;
        });
    });

    function refreshStatus() {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                agentStatus.textContent = data.agent?.name || 'galaxypron';
            });
    }

    function formatTime(iso) {
        if (!iso) return '';
        try {
            const d = new Date(iso);
            const now = new Date();
            const diff = now - d;
            if (diff < 60000) return 'Just now';
            if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
            if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
            return `${Math.floor(diff / 86400000)}d ago`;
        } catch {
            return iso;
        }
    }
});