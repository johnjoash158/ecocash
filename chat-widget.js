/**
 * EcoCash Chat Widget
 * Injected into every page. Provides a floating chat button that opens
 * a support chat panel. Messages are sent to the backend which forwards
 * them to the admin via Telegram. The admin can reply via Telegram and
 * the user sees the response in the widget (polled every 3 seconds).
 */
(function () {
    const POLL_INTERVAL = 3000;
    const CONTACT_EMAIL = 'loansecocash8@gmail.com';

    // ── Generate or restore a visitor ID (persists across pages) ────────────
    function getVisitorId() {
        let id = localStorage.getItem('eco_visitor_id');
        if (!id) {
            id = 'v_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);
            localStorage.setItem('eco_visitor_id', id);
        }
        return id;
    }

    const visitorId = getVisitorId();
    let messages    = JSON.parse(localStorage.getItem('eco_chat_msgs') || '[]');
    let pollTimer   = null;
    let isOpen      = false;
    let unread      = 0;

    // ── Inject styles ─────────────────────────────────────────────────────────
    const style = document.createElement('style');
    style.textContent = `
        #eco-chat-btn {
            position: fixed; bottom: 24px; right: 20px; z-index: 9000;
            width: 56px; height: 56px; border-radius: 50%;
            background: #003087; border: none; cursor: pointer;
            box-shadow: 0 4px 16px rgba(0,48,135,.35);
            display: flex; align-items: center; justify-content: center;
            transition: transform .2s, box-shadow .2s;
        }
        #eco-chat-btn:hover { transform: scale(1.08); box-shadow: 0 6px 20px rgba(0,48,135,.45); }
        #eco-chat-btn svg { width: 26px; height: 26px; fill: white; }
        #eco-chat-badge {
            position: absolute; top: -4px; right: -4px;
            background: #d0021b; color: white;
            font-family: 'Nunito', sans-serif; font-size: 11px; font-weight: 800;
            width: 20px; height: 20px; border-radius: 50%;
            display: none; align-items: center; justify-content: center;
            border: 2px solid white;
        }
        #eco-chat-badge.show { display: flex; }

        #eco-chat-panel {
            position: fixed; bottom: 90px; right: 20px; z-index: 9000;
            width: 340px; max-width: calc(100vw - 32px);
            background: white; border-radius: 20px;
            box-shadow: 0 8px 32px rgba(0,0,0,.15);
            display: flex; flex-direction: column;
            overflow: hidden; transform: scale(.9) translateY(10px);
            opacity: 0; pointer-events: none;
            transition: transform .25s ease, opacity .25s ease;
            max-height: 520px;
        }
        #eco-chat-panel.open {
            transform: scale(1) translateY(0);
            opacity: 1; pointer-events: all;
        }
        .eco-chat-header {
            background: #003087; padding: 14px 16px;
            display: flex; align-items: center; gap: 10px;
        }
        .eco-chat-avatar {
            width: 36px; height: 36px; border-radius: 50%;
            background: rgba(255,255,255,.2);
            display: flex; align-items: center; justify-content: center;
            font-size: 18px; flex-shrink: 0;
        }
        .eco-chat-header-info { flex: 1; }
        .eco-chat-header-name {
            color: white; font-family: 'Nunito', sans-serif;
            font-weight: 800; font-size: .95rem;
        }
        .eco-chat-header-status {
            color: rgba(255,255,255,.75);
            font-family: 'Nunito', sans-serif; font-size: .75rem;
        }
        .eco-chat-close {
            background: none; border: none; color: white;
            font-size: 1.3rem; cursor: pointer; line-height: 1;
            padding: 4px; opacity: .8;
        }
        .eco-chat-close:hover { opacity: 1; }
        .eco-chat-messages {
            flex: 1; overflow-y: auto; padding: 14px;
            display: flex; flex-direction: column; gap: 10px;
            min-height: 200px; max-height: 320px;
            background: #f7f9fc;
        }
        .eco-chat-messages::-webkit-scrollbar { width: 4px; }
        .eco-chat-messages::-webkit-scrollbar-thumb { background: #ddd; border-radius: 2px; }
        .eco-msg {
            max-width: 80%; padding: 9px 13px; border-radius: 16px;
            font-family: 'Nunito', sans-serif; font-size: .88rem; line-height: 1.45;
            word-break: break-word;
        }
        .eco-msg.user {
            background: #003087; color: white; align-self: flex-end;
            border-bottom-right-radius: 4px;
        }
        .eco-msg.admin {
            background: white; color: #1a1a1a; align-self: flex-start;
            border-bottom-left-radius: 4px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
        }
        .eco-msg.system {
            background: #eef4ff; color: #555; align-self: center;
            font-size: .78rem; text-align: center; border-radius: 10px;
            padding: 6px 12px;
        }
        .eco-msg-time {
            font-size: .68rem; opacity: .6; margin-top: 3px; display: block;
        }
        .eco-chat-input-row {
            display: flex; gap: 8px; padding: 12px;
            border-top: 1px solid #eee; background: white;
        }
        #eco-chat-input {
            flex: 1; border: 1.5px solid #ddd; border-radius: 22px;
            padding: 9px 14px; font-family: 'Nunito', sans-serif; font-size: .9rem;
            outline: none; resize: none; line-height: 1.4; max-height: 80px;
            transition: border-color .2s;
        }
        #eco-chat-input:focus { border-color: #003087; }
        #eco-chat-send {
            width: 40px; height: 40px; border-radius: 50%;
            background: #003087; border: none; cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0; transition: background .2s; align-self: flex-end;
        }
        #eco-chat-send:hover { background: #002060; }
        #eco-chat-send:disabled { background: #ccc; cursor: not-allowed; }
        #eco-chat-send svg { width: 18px; height: 18px; fill: white; }
        .eco-chat-email-bar {
            padding: 8px 14px; background: #f0f4ff;
            border-top: 1px solid #e0e8ff;
            font-family: 'Nunito', sans-serif; font-size: .75rem;
            color: #555; text-align: center;
        }
        .eco-chat-email-bar a {
            color: #003087; font-weight: 700; text-decoration: none;
        }
        .eco-chat-email-bar a:hover { text-decoration: underline; }
        @media (max-width: 420px) {
            #eco-chat-panel { right: 10px; bottom: 82px; width: calc(100vw - 20px); }
            #eco-chat-btn  { right: 16px; bottom: 20px; }
        }
    `;
    document.head.appendChild(style);

    // ── Build DOM ─────────────────────────────────────────────────────────────
    const btn = document.createElement('button');
    btn.id = 'eco-chat-btn';
    btn.title = 'Chat with Support';
    btn.innerHTML = `
        <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
            <path d="M20 2H4C2.9 2 2 2.9 2 4v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/>
        </svg>
        <span id="eco-chat-badge"></span>
    `;

    const panel = document.createElement('div');
    panel.id = 'eco-chat-panel';
    panel.innerHTML = `
        <div class="eco-chat-header">
            <div class="eco-chat-avatar">💬</div>
            <div class="eco-chat-header-info">
                <div class="eco-chat-header-name">EcoCash Support</div>
                <div class="eco-chat-header-status">We usually reply within minutes</div>
            </div>
            <button class="eco-chat-close" id="eco-chat-close">✕</button>
        </div>
        <div class="eco-chat-messages" id="eco-chat-messages"></div>
        <div class="eco-chat-email-bar">
            Or email us: <a href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a>
        </div>
        <div class="eco-chat-input-row">
            <textarea id="eco-chat-input" placeholder="Type your message..." rows="1"></textarea>
            <button id="eco-chat-send">
                <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
        </div>
    `;

    document.body.appendChild(btn);
    document.body.appendChild(panel);

    const badge     = document.getElementById('eco-chat-badge');
    const messagesEl = document.getElementById('eco-chat-messages');
    const inputEl   = document.getElementById('eco-chat-input');
    const sendBtn   = document.getElementById('eco-chat-send');

    // ── Render messages ───────────────────────────────────────────────────────
    function formatTime(iso) {
        try {
            const d = new Date(iso);
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch { return ''; }
    }

    function renderMessages() {
        messagesEl.innerHTML = '';

        if (messages.length === 0) {
            const welcome = document.createElement('div');
            welcome.className = 'eco-msg system';
            welcome.textContent = '👋 Hi! How can we help you today?';
            messagesEl.appendChild(welcome);
        }

        messages.forEach(m => {
            const wrap = document.createElement('div');
            wrap.className = 'eco-msg ' + m.role;

            // Safely render text — split on newlines, use text nodes to prevent XSS
            m.text.split('\n').forEach((line, i) => {
                if (i > 0) wrap.appendChild(document.createElement('br'));
                wrap.appendChild(document.createTextNode(line));
            });

            const timeSpan = document.createElement('span');
            timeSpan.className = 'eco-msg-time';
            timeSpan.textContent = formatTime(m.ts);
            wrap.appendChild(timeSpan);

            messagesEl.appendChild(wrap);
        });

        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function saveMessages() {
        // Keep last 100 messages
        if (messages.length > 100) messages = messages.slice(-100);
        localStorage.setItem('eco_chat_msgs', JSON.stringify(messages));
    }

    // ── Open / close ──────────────────────────────────────────────────────────
    function openChat() {
        isOpen = true;
        panel.classList.add('open');
        unread = 0;
        badge.classList.remove('show');
        renderMessages();
        inputEl.focus();
        startPolling();
    }

    function closeChat() {
        isOpen = false;
        panel.classList.remove('open');
    }

    btn.addEventListener('click', () => isOpen ? closeChat() : openChat());
    document.getElementById('eco-chat-close').addEventListener('click', closeChat);

    // ── Send message ──────────────────────────────────────────────────────────
    async function sendMessage() {
        const text = inputEl.value.trim();
        if (!text) return;

        inputEl.value = '';
        inputEl.style.height = 'auto';
        sendBtn.disabled = true;

        const msg = { role: 'user', text, ts: new Date().toISOString() };
        messages.push(msg);
        saveMessages();
        renderMessages();

        try {
            await fetch('/api/chat/send', {
                method:  'POST',
                headers: {'Content-Type': 'application/json'},
                body:    JSON.stringify({ visitor_id: visitorId, message: text })
            });
        } catch {
            // Message saved locally; will retry on next poll
        }

        sendBtn.disabled = false;
        inputEl.focus();
    }

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    // Auto-resize textarea
    inputEl.addEventListener('input', () => {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 80) + 'px';
    });

    // ── Poll for admin replies ────────────────────────────────────────────────
    async function pollReplies() {
        try {
            const res  = await fetch(`/api/chat/poll?visitor_id=${visitorId}`);
            const data = await res.json();
            if (data.messages && data.messages.length > 0) {
                let gotNew = false;
                data.messages.forEach(m => {
                    // Avoid duplicates by checking timestamp+role
                    const dup = messages.some(x => x.ts === m.ts && x.role === m.role);
                    if (!dup) {
                        messages.push({ role: 'admin', text: m.text, ts: m.ts });
                        gotNew = true;
                    }
                });
                if (gotNew) {
                    saveMessages();
                    if (isOpen) {
                        renderMessages();
                    } else {
                        unread += data.messages.length;
                        badge.textContent = unread > 9 ? '9+' : unread;
                        badge.classList.add('show');
                    }
                }
            }
        } catch { /* silent */ }
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = setInterval(pollReplies, POLL_INTERVAL);
    }

    // Start polling even when chat is closed so badge updates
    startPolling();

    renderMessages();
})();
