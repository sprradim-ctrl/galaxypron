document.addEventListener('DOMContentLoaded', () => {
    // Socket.io is optional: if the client script is missing/not served, we
    // fall back to HTTP polling so the app still works fully (chat, training
    // controls, everything). This prevents a missing socket from killing the UI.
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

        if (view === 'dashboard') loadDashboard();
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

    // Initialise 24/7 continuous trainer UI + poll for live updates (fast poll
    // + snappy bar transition = "instant" refresh)
    refreshTrainer();
    setInterval(refreshTrainer, 400);

    // Dashboard
    function loadDashboard() {
        loadTraining();
        renderWeeklyReport();
        fetch('/api/learning/data')
            .then(r => r.json())
            .then(data => {
                const stats = data.stats || {};
                document.getElementById('total-visits').textContent = stats.total_visits || 0;
                document.getElementById('unique-domains').textContent = stats.unique_domains || 0;
                document.getElementById('patterns-count').textContent = stats.patterns_learned || 0;
                document.getElementById('topics-count').textContent = stats.topics_tracked || 0;

                const topDomains = document.getElementById('top-domains');
                const prefs = document.getElementById('preferences');
                topDomains.innerHTML = '';
                prefs.innerHTML = '';

                const domains = (data.top_domains || []);
                if (domains.length === 0) {
                    topDomains.innerHTML = '<div class="empty-state">No browsing data yet. Use Edge with the extension installed.</div>';
                } else {
                    domains.forEach(d => {
                        const item = document.createElement('div');
                        item.className = 'domain-item';
                        item.innerHTML = `<span>${d.domain}</span><span class="visits">${d.visits} visits</span>`;
                        topDomains.appendChild(item);
                    });
                }

                const preferences = data.preferences || {};
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
            });
    }

    // Training model
    function loadTraining() {
        fetch('/api/training')
            .then(r => r.json())
            .then(data => {
                document.getElementById('model-examples').textContent =
                    data.examples_seen != null ? data.examples_seen : 0;
                const dev = String(data.device || 'gpu').toUpperCase();
                document.getElementById('model-device').textContent = dev;

                const info = document.getElementById('training-info');
                info.innerHTML = '';
                const rows = [
                    ['Model', data.model || 'BrowsingNet v1'],
                    ['Device', data.device || 'gpu'],
                    ['Threads', data.threads || 1],
                    ['Trainable params', data.trainable_params || 0],
                    ['Examples seen', data.examples_seen != null ? data.examples_seen.toLocaleString() : 0],
                    ['Epochs', data.epochs || 0],
                    ['Last loss', data.loss_history && data.loss_history.length
                        ? data.loss_history[data.loss_history.length - 1] : '—'],
                    ['Last trained', data.last_train || 'not yet']
                ];
                rows.forEach(([label, value]) => {
                    const row = document.createElement('div');
                    row.className = 'learning-row';
                    row.innerHTML = `<span>${label}</span><span>${escapeHtml(String(value))}</span>`;
                    info.appendChild(row);
                });

                if (data.category_weights) {
                    const catWrap = document.createElement('div');
                    catWrap.className = 'preference-wrap';
                    catWrap.style.marginTop = '10px';
                    Object.entries(data.category_weights)
                        .sort((a, b) => b[1] - a[1])
                        .slice(0, 5)
                        .forEach(([cat, w]) => {
                            const chip = document.createElement('span');
                            chip.className = 'preference-chip';
                            chip.textContent = `${cat} · ${Math.round(w * 100)}%`;
                            catWrap.appendChild(chip);
                        });
                    info.appendChild(catWrap);
                }

                renderTextModel(data.text_model);
            })
            .catch(() => {
                document.getElementById('model-device').textContent = '—';
            });
    }

    // Word2Vec text model progress bar (learns from every Wikipedia source)
    function renderTextModel(tm) {
        const panel = document.getElementById('text-model-panel');
        if (!panel) return;
        if (!tm || tm.vocab_size == null) {
            panel.style.display = 'none';
            return;
        }
        panel.style.display = '';
        const vocab = tm.vocab_size || 0;
        const epochs = tm.epochs || 0;
        const words = tm.words_trained || 0;
        const lossArr = tm.loss_history || [];
        const loss = lossArr.length ? lossArr[lossArr.length - 1] : null;

        document.getElementById('tm-vocab').textContent = vocab.toLocaleString();
        document.getElementById('tm-epochs').textContent = epochs.toLocaleString();
        document.getElementById('tm-words').textContent = words.toLocaleString();
        document.getElementById('tm-loss').textContent = loss != null ? loss.toFixed(4) : '—';

        const pct = Math.min(100, (words / 100000) * 100);
        document.getElementById('tm-vocab-pct').textContent = pct.toFixed(1) + '%';
        document.getElementById('tm-words-bar').style.width = pct + '%';
        document.getElementById('tm-words-label').textContent =
            words.toLocaleString() + ' words trained across ' + vocab.toLocaleString() +
            ' vocab words · ' + epochs.toLocaleString() + ' articles';
    }

    // Learning
    function loadLearning() {
        fetch('/api/learning/data')
            .then(r => r.json())
            .then(data => {
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

    // Training mode: GPU only (fixed). The button group below manages
    // learning intensity and grammar training; GPU is always the device.

    // Training controls: intensity slider (above) + mode buttons (below)
    const trainButtons = document.getElementById('train-buttons');
    const intensityRange = document.getElementById('intensity-range');
    const intensityValue = document.getElementById('intensity-value');
    const intensityTitle = document.getElementById('intensity-title');
    const intensityLabel = document.getElementById('intensity-label');
    const intensityNote = document.getElementById('intensity-note');

    // The currently selected training mode button
    let currentTrainKey = null;

    function setIntensityDisplay(v) {
        v = Math.max(1, Math.min(100, v));
        intensityRange.value = v;
        intensityValue.textContent = v + '%';
        intensityLabel.textContent = v + '% intensity';
    }

    function activeTrainNote(key) {
        const notes = {
            learn: 'Controls the 24/7 Wikipedia learning speed (longer cycles, less waiting between sources).',
            cpu: 'Trains the neural network on the CPU.',
            gpu: 'Uses the GPU via DirectML (CUDA first on NVIDIA) — the standard setting.',
            'cpu+gpu': 'Uses the GPU when available (CUDA/DirectML), else runs on CPU.',
            grammar: 'Learns sentence patterns and connective-word usage from every ingested source.',
        };
        return notes[key] || '';
    }

    function sendTrainControl(key, value) {
        const payload = { intensity: value };
        if (key === 'gpu') payload.mode = 'gpu';
        else if (key === 'cpu') payload.mode = 'cpu';
        else if (key === 'cpu+gpu') payload.mode = 'cpu+gpu';
        else if (key === 'grammar') { payload.grammar = true; payload.grammar_intensity = value; }
        else if (key === 'learn') { /* mode unchanged, intensity only */ }

        fetch('/api/system/train-control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(ctl => {
            // reflect the real train device on the dashboard immediately
            const devEl = document.getElementById('model-device');
            if (devEl) devEl.textContent = String(ctl.device || 'gpu').toUpperCase();
            const note = document.getElementById('train-mode-note');
            if (note && ctl.device_note) note.textContent = ctl.device_note;
        })
        .catch(() => {});
    }

    if (trainButtons) {
        // Pressing a mode button selects that mode and applies the current
        // slider intensity (1-100%) to it.
        trainButtons.addEventListener('click', (e) => {
            const btn = e.target.closest('.train-btn');
            if (!btn) return;
            const key = btn.dataset.train;
            currentTrainKey = key;
            trainButtons.querySelectorAll('.train-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            intensityTitle.textContent = 'Intensity';
            intensityNote.textContent = activeTrainNote(key);
            sendTrainControl(key, parseInt(intensityRange.value, 10));
        });

        // The intensity slider is always visible and live-updates the value.
        intensityRange.addEventListener('input', () => {
            const v = parseInt(intensityRange.value, 10);
            setIntensityDisplay(v);
            if (currentTrainKey) sendTrainControl(currentTrainKey, v);
        });
    }

    // Coverage: random articles vs. a systematic sweep of ALL of Wikipedia.
    // The full sweep walks every article in alphabetical order exactly once and
    // resumes where it left off after restarts (cursor saved server-side), so
    // the 6.9M-article goal is genuinely reachable.
    const coverageSelect = document.getElementById('coverage-select');
    if (coverageSelect) {
        coverageSelect.addEventListener('change', () => {
            const coverage = coverageSelect.value;
            coverageSelect.disabled = true;
            fetch('/api/system/train-control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    coverage,
                    reset_sweep: coverage === 'full'
                })
            })
            .then(r => r.json())
            .then(() => { coverageSelect.disabled = false; })
            .catch(() => {
                coverageSelect.disabled = false;
                coverageSelect.value = coverage === 'full' ? 'random' : 'full';
            });
        });
    }

    function syncTrainControls(controls) {
        if (!controls || !intensityRange) return;
        if (currentTrainKey === 'grammar') {
            setIntensityDisplay(controls.grammar_intensity || 30);
        } else {
            setIntensityDisplay(controls.intensity || 30);
        }
        if (coverageSelect && controls.coverage) {
            coverageSelect.value = controls.coverage;
        }
    }

    // Reflect server state once on load
    fetch('/api/system/train-control')
        .then(r => r.json())
        .then(c => {
            if (c && c.intensity) setIntensityDisplay(c.intensity);
        }).catch(() => {});

    // 24/7 continuous trainer
    const trainerToggle = document.getElementById('trainer-toggle');
    if (trainerToggle) {
        trainerToggle.addEventListener('click', () => {
            const running = trainerToggle.dataset.running === '1';
            const action = running ? 'stop' : 'start';
            const desired = !running;
            // Immediate feedback so the button never looks unresponsive
            trainerToggle.disabled = true;
            trainerToggle.textContent = running ? 'Stopping…' : 'Starting…';
            const stateEl = document.getElementById('tr-state');
            fetch('/api/system/trainer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action })
            })
            .then(r => r.json())
            .then(t => {
                // Reflect the *requested* state immediately, then poll for real one
                trainerToggle.dataset.running = desired ? '1' : '0';
                trainerToggle.textContent = desired ? 'Stop' : 'Start';
                if (stateEl) stateEl.textContent = desired ? 'Training…' : 'Off';
                trainerToggle.disabled = false;
            })
            .catch(() => {
                trainerToggle.disabled = false;
                trainerToggle.textContent = running ? 'Stop' : 'Start';
                if (stateEl) stateEl.textContent = 'Error — click Start to retry';
            })
            .finally(() => refreshTrainer());
        });
    }

    const flashBtn = document.getElementById('flash-learn-btn');
    const flashLabel = document.getElementById('flash-learn-label');
    const flashSub = document.getElementById('flash-learn-sub');
    if (flashBtn) {
        flashBtn.addEventListener('click', () => {
            if (!trainerToggle) return;
            trainerToggle.click();
            const running = trainerToggle.dataset.running === '1';
            setFlashy(running);
        });
    }
    function setFlashy(running) {
        if (!flashBtn) return;
        flashBtn.classList.toggle('running', running);
        if (flashLabel) flashLabel.textContent = running ? 'Stop Learning' : 'Start Learning';
        if (flashSub) flashSub.textContent = running ? 'Training is active — click to pause' : 'Power on 24/7 Wikipedia training';
    }

    function refreshTrainer() {
        fetch('/api/system/trainer')
            .then(r => r.json())
            .then(t => {
                if (!trainerToggle) return;
                const kb = t.knowledge || {};
                const c = t.cycle || {};
                const running = !!t.running;
                trainerToggle.dataset.running = running ? '1' : '0';
                trainerToggle.textContent = running ? 'Stop' : 'Start';
                setFlashy(running);
                document.getElementById('tr-sources').textContent = kb.articles_ingested || 0;
                document.getElementById('tr-cycle').textContent =
                    (c.sources_since_break || 0) + '/' + (c.sources_per_cycle || 450);
                document.getElementById('tr-breaks').textContent = c.breaks_taken || 0;
                document.getElementById('tr-vocab').textContent = kb.vocabulary_size || 0;

                // Wikipedia articles learned progress bar (goal: the full
                // English Wikipedia ~6.9M articles; the server owns the number)
                const articlesGoal = (t.controls && t.controls.article_goal) || 6900000;
                const articlesLearned = kb.articles_ingested || 0;
                const articlesPct = Math.min(100, (articlesLearned / articlesGoal) * 100);
                const artBar = document.getElementById('tr-articles-bar');
                if (artBar) artBar.style.width = articlesPct + '%';
                const artPctEl = document.getElementById('tr-articles-pct');
                if (artPctEl) artPctEl.textContent = Math.round(articlesPct) + '%';
                const artLabel = document.getElementById('tr-articles-label');
                if (artLabel) artLabel.textContent =
                    articlesLearned.toLocaleString() + ' / ' + articlesGoal.toLocaleString() + ' articles';

                const stateEl = document.getElementById('tr-state');
                if (c.on_break) {
                    stateEl.textContent = 'On break — ' + c.break_remaining + 's left';
                } else if (running) {
                    stateEl.textContent = 'Training…';
                } else {
                    stateEl.textContent = 'Off';
                }
                // "Sources Downloaded this cycle" progress bar (goal = cycleGoal,
                // then a break). The real count only rises in batch fetches, so we
                // animate a smoothed counter toward it to climb continuously like
                // 1,4,6,9,14… instead of snapping in 20/40/60 jumps.
                const cycleGoal = c.sources_per_cycle || 450;
                const cycleNow = c.sources_since_break || 0;
                runCycleCounter(running, cycleNow, cycleGoal, c.on_break, c.break_remaining);

                renderTextModel(t.text_model);
                syncTrainControls(t.controls);
            }).catch(() => {});
    }

    // Smoothly climbs the displayed "sources this cycle" number toward the real
    // fetched target, so the bar updates rapidly and continuously.
    let _cycleAnim = { display: 0, raf: 0 };
    function runCycleCounter(running, target, goal, onBreak, breakRemaining) {
        const bar = document.getElementById('tr-cycle-bar');
        const pctEl = document.getElementById('tr-cycle-pct');
        const labelEl = document.getElementById('tr-cycle-label');
        if (!bar || !labelEl) return;

        if (!running) {
            _cycleAnim.display = 0;
            bar.style.width = '0%';
            if (pctEl) pctEl.textContent = '0%';
            labelEl.textContent = '0 / ' + goal + ' sources (off)';
            return;
        }
        // clamp target so the bar never overshoots during a break
        const tgt = Math.min(Math.max(target, 0), goal);
        if (_cycleAnim.raf) cancelAnimationFrame(_cycleAnim.raf);

        const step = () => {
            const cur = _cycleAnim.display;
            const diff = tgt - cur;
            // move a chunk of the remaining gap each frame -> continuous climb
            const next = Math.abs(diff) < 0.5 ? tgt : cur + diff * 0.18;
            _cycleAnim.display = next;
            const pct = Math.min(100, (next / goal) * 100);
            bar.style.width = pct + '%';
            if (pctEl) pctEl.textContent = Math.round(pct) + '%';
            labelEl.textContent = onBreak
                ? 'On 25s break — ' + breakRemaining + 's left'
                : Math.round(next) + ' / ' + goal + ' sources (then 25s break)';
            if (Math.abs(tgt - next) > 0.5) {
                _cycleAnim.raf = requestAnimationFrame(step);
            } else {
                _cycleAnim.raf = 0;
                if (onBreak) labelEl.textContent = 'On 25s break — ' + breakRemaining + 's left';
            }
        };
        _cycleAnim.raf = requestAnimationFrame(step);
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