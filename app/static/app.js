/* app/static/app.js — Frontend Application Logic */

document.addEventListener('DOMContentLoaded', () => {
    // State
    let currentConnectionId = sessionStorage.getItem('connection_id') || null;
    let currentSessionId = sessionStorage.getItem('session_id') || null;

    // DOM Elements
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    const connectionInfo = document.getElementById('connection-info');
    const connectModal = document.getElementById('connect-modal');
    const connectForm = document.getElementById('connect-form');
    const connectError = document.getElementById('connect-error');
    
    const chatForm = document.getElementById('chat-form');
    const chatInput = document.getElementById('chat-input');
    const chatMessages = document.getElementById('chat-messages');
    const sessionsList = document.getElementById('sessions-list');
    
    const btnOpenConnect = document.getElementById('btn-open-connect');
    const btnCloseModal = document.getElementById('btn-close-modal');
    const btnCancelModal = document.getElementById('btn-cancel-modal');
    const btnNewChat = document.getElementById('btn-new-chat');

    // Initialize App
    init();

    async function init() {
        setupEventListeners();
        if (currentConnectionId) {
            await validateConnection(currentConnectionId);
        } else {
            showConnectModal();
        }
        await loadSessions();
        if (currentSessionId) {
            await loadSessionMessages(currentSessionId);
        }
    }

    function setupEventListeners() {
        btnOpenConnect.addEventListener('click', showConnectModal);
        btnCloseModal.addEventListener('click', hideConnectModal);
        btnCancelModal.addEventListener('click', hideConnectModal);
        
        connectForm.addEventListener('submit', handleConnectSubmit);
        chatForm.addEventListener('submit', handleChatSubmit);
        btnNewChat.addEventListener('click', handleNewChat);

        // Click sample chips
        document.addEventListener('click', (e) => {
            if (e.target.classList.contains('chip')) {
                const query = e.target.getAttribute('data-query');
                if (query) {
                    chatInput.value = query;
                    chatForm.dispatchEvent(new Event('submit'));
                }
            }
        });
    }

    function showConnectModal() {
        connectError.classList.add('hidden');
        connectModal.classList.remove('hidden');
    }

    function hideConnectModal() {
        connectModal.classList.add('hidden');
    }

    function updateConnectionStatus(isConnected, info = '') {
        if (isConnected) {
            statusDot.className = 'dot dot-green';
            statusText.textContent = 'Connected';
            connectionInfo.textContent = info ? `(${info})` : '';
        } else {
            statusDot.className = 'dot dot-red';
            statusText.textContent = 'Disconnected';
            connectionInfo.textContent = '';
        }
    }

    // API: Validate Connection
    async function validateConnection(connId) {
        try {
            const res = await fetch(`/api/v1/database/validate/${connId}`);
            if (res.ok) {
                const data = await res.json();
                if (data.is_valid) {
                    updateConnectionStatus(true, connId.substring(0, 8) + '...');
                    return true;
                }
            }
        } catch (e) {
            console.error('Validation error:', e);
        }
        sessionStorage.removeItem('connection_id');
        currentConnectionId = null;
        updateConnectionStatus(false);
        showConnectModal();
        return false;
    }

    // API: Connect Database Submit
    async function handleConnectSubmit(e) {
        e.preventDefault();
        connectError.classList.add('hidden');
        
        const payload = {
            db_type: document.getElementById('db-type').value,
            host: document.getElementById('db-host').value,
            port: parseInt(document.getElementById('db-port').value),
            database_name: document.getElementById('db-name').value,
            username: document.getElementById('db-user').value,
            password: document.getElementById('db-pass').value
        };

        const btnSubmit = document.getElementById('btn-submit-connect');
        btnSubmit.disabled = true;
        btnSubmit.textContent = 'Connecting...';

        try {
            const res = await fetch('/api/v1/database/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const data = await res.json();
            if (res.ok && data.status === 'connected') {
                currentConnectionId = data.connection_id;
                sessionStorage.setItem('connection_id', currentConnectionId);
                updateConnectionStatus(true, `${payload.db_type}@${payload.host}:${payload.port}/${payload.database_name}`);
                hideConnectModal();
            } else {
                throw new Error(data.message || data.detail || 'Failed to connect to database.');
            }
        } catch (err) {
            connectError.textContent = err.message;
            connectError.classList.remove('hidden');
        } finally {
            btnSubmit.disabled = false;
            btnSubmit.textContent = 'Connect Database';
        }
    }

    // API: Load Chat Sessions
    async function loadSessions() {
        try {
            const res = await fetch('/api/v1/chat/sessions?page=1&page_size=20');
            if (res.ok) {
                const data = await res.json();
                renderSessions(data.items || []);
            }
        } catch (e) {
            console.error('Failed to load sessions:', e);
        }
    }

    function renderSessions(sessions) {
        sessionsList.innerHTML = '';
        if (sessions.length === 0) {
            sessionsList.innerHTML = '<li class="session-item">No active sessions</li>';
            return;
        }

        sessions.forEach(s => {
            const li = document.createElement('li');
            li.className = `session-item ${s.session_id === currentSessionId ? 'active' : ''}`;
            li.innerHTML = `
                <span class="session-title-text">💬 ${escapeHtml(s.title)}</span>
            `;
            li.addEventListener('click', () => switchSession(s.session_id));
            sessionsList.appendChild(li);
        });
    }

    async function switchSession(sessionId) {
        currentSessionId = sessionId;
        sessionStorage.setItem('session_id', sessionId);
        await loadSessions();
        await loadSessionMessages(sessionId);
    }

    async function handleNewChat() {
        try {
            const res = await fetch('/api/v1/chat/sessions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: 'New Chat', connection_id: currentConnectionId })
            });

            if (res.ok) {
                const data = await res.json();
                currentSessionId = data.session_id;
                sessionStorage.setItem('session_id', currentSessionId);
                await loadSessions();
                chatMessages.innerHTML = '';
                showWelcomeBanner();
            }
        } catch (e) {
            console.error('Failed to create session:', e);
        }
    }

    async function loadSessionMessages(sessionId) {
        chatMessages.innerHTML = '';
        try {
            const res = await fetch(`/api/v1/chat/sessions/${sessionId}/messages?page=1&page_size=50`);
            if (res.ok) {
                const data = await res.json();
                const messages = data.items || [];
                if (messages.length === 0) {
                    showWelcomeBanner();
                } else {
                    messages.reverse().forEach(m => {
                        appendUserMessage(m.content, m.role === 'user');
                    });
                }
            }
        } catch (e) {
            console.error('Failed to load session messages:', e);
        }
    }

    function showWelcomeBanner() {
        chatMessages.innerHTML = `
            <div class="welcome-card">
                <div class="welcome-icon">🤖</div>
                <h2>Database Chatbot Assistant</h2>
                <p>Ask natural language questions about your connected database. The AI generates safe SQL queries, executes them live, and streams formatted results.</p>
                <div class="sample-queries">
                    <div class="sample-title">Try asking:</div>
                    <button class="chip" data-query="Show me all database connections">"Show me all database connections"</button>
                    <button class="chip" data-query="How many chat sessions are in the database?">"How many chat sessions are in the database?"</button>
                    <button class="chip" data-query="Show active chat sessions ordered by created_at descending">"Show active chat sessions ordered by created_at descending"</button>
                    <button class="chip" data-query="Group chat messages by role and count total for each role">"Group chat messages by role and count total for each role"</button>
                </div>
            </div>
        `;
    }

    // Chat Submit & SSE Reader
    async function handleChatSubmit(e) {
        e.preventDefault();
        const message = chatInput.value.trim();
        if (!message) return;

        if (!currentConnectionId) {
            alert('Please connect to a database first!');
            showConnectModal();
            return;
        }

        chatInput.value = '';
        const welcomeCard = chatMessages.querySelector('.welcome-card');
        if (welcomeCard) welcomeCard.remove();

        // 1. Render User Message
        appendUserBubble(message);

        // 2. Prepare Assistant Message Box with Pipeline Pills Container
        const assistantCard = createAssistantBubble();
        chatMessages.appendChild(assistantCard.container);
        scrollToBottom();

        // 3. Start Streaming Query
        const payload = {
            connection_id: currentConnectionId,
            message: message,
            session_id: currentSessionId || undefined
        };

        try {
            const response = await fetch('/api/v1/chat/query', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop(); // Keep last incomplete chunk

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const eventData = JSON.parse(line.substring(6).trim());
                            handleSSEEvent(eventData, assistantCard);
                        } catch (err) {
                            console.error('SSE parse error:', err, line);
                        }
                    }
                }
            }
        } catch (err) {
            assistantCard.updateError('Network error connecting to stream endpoint.');
        }
    }

    function appendUserBubble(text) {
        const group = document.createElement('div');
        group.className = 'message-group';
        const userBubble = document.createElement('div');
        userBubble.className = 'message-user';
        userBubble.textContent = text;
        group.appendChild(userBubble);
        chatMessages.appendChild(group);
        scrollToBottom();
    }

    function createAssistantBubble() {
        const group = document.createElement('div');
        group.className = 'message-group';

        const card = document.createElement('div');
        card.className = 'message-assistant';

        const pipelineDiv = document.createElement('div');
        pipelineDiv.className = 'pipeline-steps';

        const contentDiv = document.createElement('div');
        contentDiv.className = 'assistant-content';

        card.appendChild(pipelineDiv);
        card.appendChild(contentDiv);
        group.appendChild(card);

        return {
            container: group,
            pipelineDiv: pipelineDiv,
            contentDiv: contentDiv,
            steps: {},
            updateStep(stage, status, label) {
                let pill = this.steps[stage];
                if (!pill) {
                    pill = document.createElement('div');
                    pill.className = `step-pill ${status}`;
                    this.pipelineDiv.appendChild(pill);
                    this.steps[stage] = pill;
                }
                pill.className = `step-pill ${status}`;
                pill.innerHTML = `${status === 'complete' ? '✓' : status === 'active' ? '⏳' : '❌'} ${label}`;
            },
            updateError(msg) {
                const errDiv = document.createElement('div');
                errDiv.className = 'form-error';
                errDiv.style.display = 'block';
                errDiv.textContent = msg;
                this.contentDiv.appendChild(errDiv);
            }
        };
    }

    function handleSSEEvent(event, assistantCard) {
        const stage = event.stage;
        const data = event.data || {};

        switch (stage) {
            case 'received':
                assistantCard.updateStep('received', 'complete', 'Request Received');
                break;
            case 'extracting_schema':
                assistantCard.updateStep('schema', 'active', 'Extracting Schema...');
                if (data.table_count !== undefined) {
                    assistantCard.updateStep('schema', 'complete', `Schema Loaded (${data.table_count} tables)`);
                }
                break;
            case 'generating_sql':
                assistantCard.provider = data.provider || 'gemini';
                assistantCard.updateStep('llm', 'active', `Generating SQL (${assistantCard.provider})...`);
                break;
            case 'validating_sql':
                assistantCard.updateStep('llm', 'complete', `SQL Generated (${assistantCard.provider || 'gemini'})`);
                assistantCard.updateStep('val', 'complete', 'SQL Security Passed (8 checks)');
                break;
            case 'executing':
                assistantCard.updateStep('exec', 'complete', 'Query Executed');
                break;
            case 'complete':
                renderCompleteResult(data, assistantCard);
                loadSessions(); // Refresh session sidebar title
                break;
            case 'error':
                if (data.stage === 'generating_sql' || data.code === 'SQL_GENERATION_FAILED') {
                    assistantCard.updateStep('llm', 'error', 'SQL Generation Failed');
                } else if (data.stage === 'validating_sql' || data.code === 'SQL_VALIDATION_FAILED') {
                    assistantCard.updateStep('val', 'error', 'SQL Validation Failed');
                } else {
                    assistantCard.updateStep('err', 'error', data.code || 'Error');
                }
                const errorDisplay = data.detail && data.detail !== data.message
                    ? `${data.message}\n${data.detail}`
                    : (data.message || 'An error occurred during pipeline execution.');
                assistantCard.updateError(errorDisplay);
                break;
        }
        scrollToBottom();
    }

    function renderCompleteResult(data, assistantCard) {
        const contentDiv = assistantCard.contentDiv;
        contentDiv.innerHTML = '';

        // Cache Badge
        if (data.cache_hit) {
            const badge = document.createElement('span');
            badge.className = 'cache-badge';
            badge.innerHTML = '⚡ Cached Result (&lt;5ms)';
            contentDiv.appendChild(badge);
        }

        // Reasoning / Explanation
        if (data.sql_details && data.sql_details.reasoning) {
            const reasoning = document.createElement('div');
            reasoning.className = 'reasoning-text';
            reasoning.textContent = data.sql_details.reasoning;
            contentDiv.appendChild(reasoning);
        }

        // SQL Code Block
        if (data.sql_details && data.sql_details.sql_query) {
            const sqlBox = document.createElement('pre');
            sqlBox.className = 'sql-box';
            sqlBox.textContent = data.sql_details.sql_query;
            contentDiv.appendChild(sqlBox);
        }

        // Data Table
        if (data.results && data.results.columns && data.results.rows) {
            const tableWrapper = document.createElement('div');
            tableWrapper.className = 'table-wrapper';
            
            const table = document.createElement('table');
            table.className = 'data-table';

            // Thead
            const thead = document.createElement('thead');
            const headerRow = document.createElement('tr');
            data.results.columns.forEach(col => {
                const th = document.createElement('th');
                th.textContent = col;
                headerRow.appendChild(th);
            });
            thead.appendChild(headerRow);
            table.appendChild(thead);

            // Tbody
            const tbody = document.createElement('tbody');
            if (data.results.rows.length === 0) {
                const tr = document.createElement('tr');
                const td = document.createElement('td');
                td.colSpan = data.results.columns.length || 1;
                td.textContent = 'No records found.';
                td.style.textAlign = 'center';
                td.style.color = '#94a3b8';
                tr.appendChild(td);
                tbody.appendChild(tr);
            } else {
                data.results.rows.forEach(row => {
                    const tr = document.createElement('tr');
                    row.forEach(cell => {
                        const td = document.createElement('td');
                        td.textContent = cell !== null ? String(cell) : 'NULL';
                        tr.appendChild(td);
                    });
                    tbody.appendChild(tr);
                });
            }
            table.appendChild(tbody);
            tableWrapper.appendChild(table);
            contentDiv.appendChild(tableWrapper);
        }
    }

    function scrollToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function escapeHtml(str) {
        return str ? str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") : '';
    }
});
