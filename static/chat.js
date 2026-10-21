/* Market Terminal — stock Q&A assistant (floating panel, all routes).
 * Talks to POST /api/chat; the server grounds answers in the dashboard's own data.
 * Self-contained: builds its own DOM, needs marked + DOMPurify loaded first.
 */
(() => {
  'use strict';

  const STORE = 'mt-chat-v1';
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const md = (s) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(s)) : esc(s).replace(/\n/g, '<br>'));
  const currentSymbol = () => {
    const m = (location.hash || '').match(/^#\/(?:ticker|trade)\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]).toUpperCase() : null;
  };

  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(STORE) || '[]'); } catch { history = []; }
  const save = () => { try { sessionStorage.setItem(STORE, JSON.stringify(history.slice(-40))); } catch { /* private mode */ } };
  let busy = false;

  const style = document.createElement('style');
  style.textContent = `
    #chat-panel .msg-md p { margin: 0 0 .5em; } #chat-panel .msg-md p:last-child { margin-bottom: 0; }
    #chat-panel .msg-md ul, #chat-panel .msg-md ol { margin: .25em 0 .5em 1.1em; list-style: disc; }
    #chat-panel .msg-md ol { list-style: decimal; }
    #chat-panel .msg-md table { font-size: 11px; border-collapse: collapse; margin: .4em 0; display: block; overflow-x: auto; }
    #chat-panel .msg-md th, #chat-panel .msg-md td { border: 1px solid #1e2632; padding: 2px 6px; text-align: left; }
    #chat-panel .msg-md code { font-family: 'IBM Plex Mono', monospace; background: #151b25; padding: 0 3px; border-radius: 3px; }
    #chat-panel .msg-md strong { color: #f8fafc; }
    @keyframes chatDot { 0%, 80%, 100% { opacity: .2; } 40% { opacity: 1; } }
    #chat-panel .dot { animation: chatDot 1.2s infinite; } #chat-panel .dot:nth-child(2) { animation-delay: .2s; } #chat-panel .dot:nth-child(3) { animation-delay: .4s; }
  `;
  document.head.appendChild(style);

  const root = document.createElement('div');
  root.innerHTML = `
    <button id="chat-launch" type="button" aria-label="Open stock assistant"
      class="fixed bottom-4 right-4 z-40 flex items-center gap-2 bg-accent text-ink-950 font-semibold text-xs px-3 py-2 rounded-full shadow-lg hover:brightness-110">
      <span aria-hidden="true">✦</span> Ask
    </button>
    <section id="chat-panel" role="dialog" aria-label="Stock assistant"
      class="hidden fixed z-40 bottom-0 right-0 sm:bottom-4 sm:right-4 w-full sm:w-[420px] h-[85vh] sm:h-[600px] max-h-screen panel flex flex-col shadow-2xl">
      <div class="panel-h">
        <span>Stock Assistant <span id="chat-ctx" class="normal-case tracking-normal text-slate-500"></span></span>
        <span class="flex gap-3 normal-case tracking-normal">
          <button id="chat-clear" type="button" class="hover:text-slate-200">Clear</button>
          <button id="chat-close" type="button" aria-label="Close" class="hover:text-slate-200 text-sm leading-none">✕</button>
        </span>
      </div>
      <div id="chat-log" class="flex-1 overflow-y-auto p-3 space-y-3 text-[13px] leading-relaxed"></div>
      <form id="chat-form" class="border-t border-ink-700 p-2 flex gap-2 items-end">
        <textarea id="chat-input" rows="1" maxlength="4000" placeholder="Ask about any stock…"
          class="flex-1 resize-none bg-ink-850 border border-ink-700 rounded px-2 py-1.5 text-[13px] focus:outline-none focus:border-accent max-h-32"></textarea>
        <button id="chat-send" type="submit" class="bg-accent text-ink-950 font-semibold text-xs px-3 py-2 rounded disabled:opacity-40">Send</button>
      </form>
    </section>`;
  document.body.appendChild(root);

  const $ = (id) => document.getElementById(id);
  const panel = $('chat-panel'), log = $('chat-log'), input = $('chat-input'), send = $('chat-send');

  function suggestions() {
    const s = currentSymbol();
    const qs = s
      ? [`Give me a quick read on ${s}`, `Is ${s} overbought right now?`, `How has ${s} done over the last 3 months?`]
      : ['How is the market doing today?', 'Compare NVDA and AMD on valuation and momentum', 'Which large caps are moving the most?'];
    return `<div class="text-slate-500 text-xs space-y-2">
      <p>Ask anything about stocks, ETFs or the market. Answers use the same live data as the dashboard.</p>
      ${qs.map((q) => `<button type="button" data-q="${esc(q)}" class="block w-full text-left px-2 py-1.5 rounded border border-ink-700 hover:border-accent hover:text-slate-200">${esc(q)}</button>`).join('')}
    </div>`;
  }

  function bubble(m) {
    if (m.role === 'user') {
      return `<div class="flex justify-end"><div class="bg-ink-700 rounded-lg px-3 py-2 max-w-[85%] whitespace-pre-wrap">${esc(m.content)}</div></div>`;
    }
    const tools = m.tools?.length ? `<div class="text-[10px] text-slate-500 num mt-1">data: ${esc(m.tools.join(', '))}</div>` : '';
    const tone = m.error ? 'border-down/50 text-red-300' : 'border-ink-700';
    return `<div><div class="msg-md bg-ink-850 border ${tone} rounded-lg px-3 py-2">${m.error ? esc(m.content) : md(m.content)}</div>${tools}</div>`;
  }

  function render() {
    const s = currentSymbol();
    $('chat-ctx').textContent = s ? `· viewing ${s}` : '';
    log.innerHTML = history.length ? history.map(bubble).join('') : suggestions();
    if (busy) log.insertAdjacentHTML('beforeend', '<div class="text-slate-500 text-lg leading-none px-1"><span class="dot">•</span><span class="dot">•</span><span class="dot">•</span></div>');
    log.scrollTop = log.scrollHeight;
    send.disabled = busy;
  }

  async function ask(text) {
    text = text.trim();
    if (!text || busy) return;
    history.push({ role: 'user', content: text });
    busy = true; save(); render();
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 180000);
      const res = await fetch('/api/chat', {
        method: 'POST', signal: ctrl.signal,
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        // Only real turns go to the server; error bubbles are local.
        body: JSON.stringify({
          symbol: currentSymbol(),
          messages: history.filter((m) => !m.error).map(({ role, content }) => ({ role, content })),
        }),
      }).finally(() => clearTimeout(timer));
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || body.message || `HTTP ${res.status}`);
      history.push({ role: 'assistant', content: body.reply, tools: body.tools_used });
    } catch (e) {
      history.push({ role: 'assistant', error: true, content: e.name === 'AbortError' ? 'The assistant took too long to answer. Try again.' : e.message || 'Request failed' });
    } finally {
      busy = false; save(); render();
    }
  }

  function open() {
    panel.classList.remove('hidden'); $('chat-launch').classList.add('hidden');
    render(); input.focus();
  }
  function close() { panel.classList.add('hidden'); $('chat-launch').classList.remove('hidden'); }

  $('chat-launch').addEventListener('click', open);
  $('chat-close').addEventListener('click', close);
  $('chat-clear').addEventListener('click', () => { history = []; save(); render(); });
  log.addEventListener('click', (e) => { const q = e.target.closest('[data-q]'); if (q) ask(q.dataset.q); });
  $('chat-form').addEventListener('submit', (e) => { e.preventDefault(); const t = input.value; input.value = ''; input.style.height = ''; ask(t); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('chat-form').requestSubmit(); }
    if (e.key === 'Escape') close();
  });
  input.addEventListener('input', () => { input.style.height = ''; input.style.height = `${input.scrollHeight}px`; });
  window.addEventListener('hashchange', () => { if (!panel.classList.contains('hidden') && !history.length) render(); else $('chat-ctx').textContent = currentSymbol() ? `· viewing ${currentSymbol()}` : ''; });

  // Without an API key on the server, dim the launcher and explain inside the panel.
  fetch('/api/chat/status').then((r) => r.json()).then((s) => {
    if (!s.enabled) {
      $('chat-launch').title = s.reason || 'Assistant disabled';
      $('chat-launch').classList.add('opacity-50');
      history.length || history.push({ role: 'assistant', error: true, content: s.reason || 'Assistant disabled' });
    }
  }).catch(() => {});
})();
