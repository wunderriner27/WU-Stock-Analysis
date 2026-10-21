/* AI trader panel (Trade tab).
 * Claude proposes orders via POST /api/ai-trader/propose; nothing is placed until the user
 * confirms an individual proposal, and the server re-checks the limits before submitting it.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const fmt = (v, d = 2) => (isNum(v) ? v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');

  const ai = { status: null, plan: null, busy: false, done: {}, auto: null, autoBusy: false };

  /** POST/GET/DELETE JSON. Never retried: a retried execute could place an order twice. */
  async function call(path, method = 'GET', body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 180000); // Claude can take a while on a full portfolio
    try {
      const res = await fetch(path, {
        method, signal: ctrl.signal,
        headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
        throw new Error(data.message || detail || `HTTP ${res.status}`);
      }
      return data;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('Request timed out');
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  function confirmDialog(title, html, tone, okText = 'Place order') {
    const dlg = $('confirm');
    $('c-title').textContent = title;
    $('c-body').innerHTML = html;
    const btn = $('c-ok');
    btn.textContent = okText;
    btn.className = `px-4 py-1.5 text-sm rounded font-semibold text-ink-950 ${tone === 'buy' ? 'bg-up hover:bg-green-400' : 'bg-down hover:bg-red-400'}`;
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.returnValue = '';
      dlg.showModal();
      dlg.querySelector('button[value="cancel"]').focus();
    });
  }

  function renderStatus() {
    const s = ai.status;
    if (!s) return;
    const L = s.limits;
    $('ai-limits').innerHTML = `Limits: $${fmt(L.max_order_usd, 0)} per order · position ≤ ${L.max_position_pct}% of equity · `
      + `${s.orders_today}/${L.max_orders_per_day} AI orders today · shorts ${L.allow_shorts ? 'on' : 'off'}`
      + (s.enabled && s.api_key_set === false ? ' · <span class="text-amber-300">ANTHROPIC_API_KEY is not set in .env</span>' : '');
    const live = s.mode === 'LIVE';
    $('ai-mode').innerHTML = !s.enabled ? '' : live
      ? `<span class="text-down font-semibold">LIVE${s.can_execute ? '' : ' · proposals only'}</span>`
      : '<span class="text-up">paper</span>';
  }

  async function loadStatus() {
    try { ai.status = await call('/api/ai-trader/status'); renderStatus(); } catch { /* panel still usable */ }
  }

  function orderRow(o) {
    const blocked = o.violations.length > 0;
    const done = ai.done[o.id];
    const sideCls = o.side === 'buy' ? 'text-up' : 'text-down';
    const px = o.type === 'limit' ? `limit $${fmt(o.limit_price)}` : 'market';
    let action;
    if (done) action = `<span class="text-[11px] ${done.ok ? 'text-up' : 'text-down'}">${esc(done.msg)}</span>`;
    else if (blocked) action = '<span class="text-[11px] text-slate-500">Blocked</span>';
    else action = `<div class="flex gap-1 justify-end">
        <button data-exec="${esc(o.id)}" class="text-[11px] px-3 py-1 rounded font-semibold ${o.side === 'buy' ? 'bg-up text-ink-950' : 'bg-down text-white'}">Review &amp; place</button>
        <button data-dismiss="${esc(o.id)}" class="text-[11px] px-2 py-1 rounded bg-ink-700 hover:bg-ink-600">Skip</button></div>`;
    return `<div class="border border-ink-700 rounded p-3 ${blocked || done ? 'opacity-80' : ''}">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="num text-sm"><span class="${sideCls} font-semibold uppercase">${esc(o.side)}</span>
          ${esc(o.qty)} <a href="#/ticker/${encodeURIComponent(o.symbol)}" class="text-slate-100 font-semibold hover:text-accent">${esc(o.symbol)}</a>
          <span class="text-slate-500">· ${esc(px)} · ${esc(o.time_in_force.toUpperCase())} · ≈ $${fmt(o.est_value)}</span>
          <span class="chip chip-neutral ml-1">${esc(o.confidence)} confidence</span></div>
        ${action}
      </div>
      <p class="text-xs text-slate-300 mt-2">${esc(o.rationale)}</p>
      ${blocked ? `<p class="text-[11px] text-amber-300/90 mt-1">⚠ ${o.violations.map(esc).join(' · ')}</p>` : ''}
    </div>`;
  }

  function renderPlan() {
    const p = ai.plan;
    if (!p) { $('ai-result').innerHTML = ''; return; }
    const orders = p.orders.length ? p.orders.map(orderRow).join('')
      : '<div class="text-xs text-slate-500">No orders proposed.</div>';
    const risks = p.risks.length
      ? `<div class="text-[11px] text-slate-400"><span class="uppercase tracking-widest text-slate-500">Risks</span><ul class="list-disc ml-5 mt-1 space-y-0.5">${p.risks.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>` : '';
    $('ai-result').innerHTML = `<p class="text-sm text-slate-200 whitespace-pre-line">${esc(p.summary)}</p>${orders}${risks}
      <p class="text-[10px] text-slate-600">Proposals expire after ${Math.round(p.expires_in / 60)} minutes. Each order needs your confirmation. Not investment advice.</p>`;
  }

  async function onAsk(e) {
    e.preventDefault();
    if (ai.busy) return;
    const instruction = $('ai-instruction').value.trim();
    if (!instruction) return;
    const symbol = $('ai-symbol').value.trim().toUpperCase() || null;
    ai.busy = true; ai.done = {};
    $('ai-submit').disabled = true; $('ai-submit').textContent = 'Thinking…';
    $('ai-result').innerHTML = '<div class="text-xs text-slate-500">Claude is reading the quotes, indicators and your account…</div>';
    try {
      ai.plan = await call('/api/ai-trader/propose', 'POST', { instruction, symbol });
      renderPlan();
    } catch (err) {
      ai.plan = null;
      $('ai-result').innerHTML = `<div class="text-xs text-down">${esc(err.message)}</div>`;
    } finally {
      ai.busy = false;
      $('ai-submit').disabled = false; $('ai-submit').textContent = 'Ask Claude';
    }
  }

  async function onResultClick(e) {
    const btn = e.target.closest('button');
    if (!btn || !ai.plan) return;
    if (btn.dataset.dismiss) {
      call(`/api/ai-trader/proposals/${encodeURIComponent(btn.dataset.dismiss)}`, 'DELETE').catch(() => {});
      ai.done[btn.dataset.dismiss] = { ok: false, msg: 'Skipped' };
      renderPlan();
      return;
    }
    const o = ai.plan.orders.find((x) => x.id === btn.dataset.exec);
    if (!o) return;
    const live = ai.plan.mode === 'LIVE';
    const ok = await confirmDialog(`${live ? 'LIVE ' : ''}AI order`, `
      ${live ? '<div class="text-down font-semibold">This is a LIVE account. Real money.</div>' : '<div class="text-up">Paper account</div>'}
      <table class="tbl text-xs"><tbody>
        <tr><td class="text-slate-500">Symbol</td><td class="r font-semibold">${esc(o.symbol)}</td></tr>
        <tr><td class="text-slate-500">Side</td><td class="r ${o.side === 'buy' ? 'text-up' : 'text-down'} font-semibold">${esc(o.side.toUpperCase())}</td></tr>
        <tr><td class="text-slate-500">Quantity</td><td class="r">${esc(o.qty)}</td></tr>
        <tr><td class="text-slate-500">Type</td><td class="r">${o.type === 'limit' ? `Limit $${fmt(o.limit_price)}` : 'Market'} · ${esc(o.time_in_force.toUpperCase())}</td></tr>
        <tr><td class="text-slate-500">Est. value</td><td class="r">$${fmt(o.est_value)}</td></tr>
      </tbody></table>
      <p class="text-xs text-slate-400">${esc(o.rationale)}</p>`, o.side);
    if (!ok) return;
    btn.disabled = true;
    try {
      const r = await call(`/api/ai-trader/proposals/${encodeURIComponent(o.id)}/execute`, 'POST');
      ai.done[o.id] = { ok: true, msg: `Submitted (${r.order.status})` };
      window.dispatchEvent(new CustomEvent('ai-trader:order', { detail: r.order }));
      loadStatus();
    } catch (err) {
      ai.done[o.id] = { ok: false, msg: err.message };
    }
    renderPlan();
  }


  // ------------------------------------------------------------------ auto mode
  const when = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '—');

  function renderAuto() {
    const a = ai.auto;
    if (!a) return;
    const on = a.enabled;
    $('ai-auto-badge').innerHTML = on ? '<span class="text-up font-semibold">ON</span>' : '<span class="text-slate-500">off</span>';
    const btn = $('ai-auto-toggle');
    btn.textContent = on ? 'Turn off' : 'Turn on auto mode';
    btn.className = `font-semibold text-sm px-5 py-2 rounded disabled:opacity-40 ${on ? 'bg-down text-white' : 'bg-accent text-ink-950'}`;
    btn.disabled = ai.autoBusy || (!on && !a.allowed);
    $('ai-auto-run').classList.toggle('hidden', !on);
    $('ai-auto-run').disabled = ai.autoBusy || a.running;
    for (const id of ['ai-auto-symbols', 'ai-auto-interval', 'ai-auto-instruction']) $(id).disabled = on;
    if (on && document.activeElement?.closest('#ai-auto-form') == null) {
      $('ai-auto-symbols').value = a.symbols.join(', ');
      $('ai-auto-interval').value = String(a.interval_min);
      $('ai-auto-instruction').value = a.instruction;
    }
    let st;
    if (!a.allowed) st = '<span class="text-amber-300">Not allowed: set AI_TRADER_AUTO=yes in .env and restart.</span>';
    else if (a.running) st = 'Running now…';
    else if (on) st = `Next run ${when(a.next_run)}${a.market_open ? '' : ' (market closed, waits for the open)'} · last ${when(a.last_run)}`;
    else st = a.stopped_reason ? `Stopped: ${esc(a.stopped_reason)}` : 'Off';
    $('ai-auto-status').innerHTML = st;

    $('ai-auto-log').innerHTML = a.activity.map((e) => `
      <div class="border border-ink-700 rounded p-2 text-[11px]">
        <div class="text-slate-500 num">${esc(when(e.at))}${e.error ? ` · <span class="text-down">${esc(e.error)}</span>` : ''}</div>
        ${e.placed.map((p) => `<div><span class="text-up font-semibold">Placed</span> <span class="num">${esc(p.order)}</span> <span class="text-slate-500">(${esc(p.status)})</span> · <span class="text-slate-300">${esc(p.rationale)}</span></div>`).join('')}
        ${e.blocked.map((b) => `<div><span class="text-amber-300">Blocked</span> <span class="num">${esc(b.order)}</span> · <span class="text-slate-400">${esc(b.reason)}</span></div>`).join('')}
        ${e.plans.map((p) => `<div class="text-slate-400">${p.symbol ? `<span class="num text-slate-300">${esc(p.symbol)}</span>: ` : ''}${esc(p.summary)}</div>`).join('')}
      </div>`).join('') || '<div class="text-[11px] text-slate-600">No runs yet.</div>';
  }

  async function loadAuto() {
    try { ai.auto = await call('/api/ai-trader/auto'); renderAuto(); } catch { /* keep last state */ }
  }

  async function onAutoToggle(e) {
    e.preventDefault();
    if (ai.autoBusy || !ai.auto) return;
    const turnOn = !ai.auto.enabled;
    const body = {
      enabled: turnOn,
      instruction: $('ai-auto-instruction').value.trim(),
      symbols: $('ai-auto-symbols').value.split(/[\s,]+/).filter(Boolean),
      interval_min: Number($('ai-auto-interval').value),
    };
    if (turnOn) {
      if (!body.instruction) { $('ai-auto-status').innerHTML = '<span class="text-down">Describe the strategy first.</span>'; return; }
      const L = ai.status?.limits || {};
      const ok = await confirmDialog('Turn on auto mode', `
        <p>Claude will trade <b>${body.symbols.length ? esc(body.symbols.join(', ')) : 'your portfolio'}</b> ${esc($('ai-auto-interval').selectedOptions[0].text.toLowerCase())} during market hours, <b>without asking you first</b>. The first run starts right away if the market is open.</p>
        <p class="text-xs text-slate-400">Paper account only. Each order still has to pass the limits: $${fmt(L.max_order_usd, 0)} per order, ≤ ${esc(L.max_position_pct)}% of equity per position, ${esc(L.max_orders_per_day)} AI orders a day. It turns itself off after 3 failed runs or a server restart. Each run is a Claude API call per symbol.</p>`, 'buy', 'Turn on');
      if (!ok) return;
    }
    ai.autoBusy = true; renderAuto();
    try {
      ai.auto = await call('/api/ai-trader/auto', 'POST', body);
      if (turnOn) setTimeout(loadAuto, 3000); // pick up the first run's state
    } catch (err) { ai.auto.stopped_reason = err.message; }
    ai.autoBusy = false; renderAuto();
  }

  async function onRunNow() {
    if (ai.autoBusy) return;
    ai.autoBusy = true; ai.auto.running = true; renderAuto();
    try {
      ai.auto = await call('/api/ai-trader/auto/run', 'POST');
      if (ai.auto.activity[0]?.placed.length) window.dispatchEvent(new CustomEvent('ai-trader:order'));
      loadStatus();
    } catch (err) {
      ai.auto.running = false; $('ai-auto-status').innerHTML = `<span class="text-down">${esc(err.message)}</span>`;
    }
    ai.autoBusy = false; renderAuto();
  }

  function syncSymbol() {
    // Default the focus symbol to the one in the URL (#/trade/AAPL) when the box is empty.
    const m = location.hash.match(/^#\/trade\/([^/?#]+)/);
    if (m && !$('ai-symbol').value) $('ai-symbol').value = decodeURIComponent(m[1]).toUpperCase();
    if (location.hash.startsWith('#/trade')) { loadStatus(); loadAuto(); }
  }

  function init() {
    if (!$('ai-trader')) return;
    $('ai-form').addEventListener('submit', onAsk);
    $('ai-result').addEventListener('click', onResultClick);
    $('ai-auto-form').addEventListener('submit', onAutoToggle);
    $('ai-auto-run').addEventListener('click', onRunNow);
    window.addEventListener('hashchange', syncSymbol);
    syncSymbol();
    loadStatus();
    loadAuto();
    let lastPlaced = 0;
    setInterval(async () => {
      if (document.hidden || !location.hash.startsWith('#/trade') || ai.autoBusy) return;
      await loadAuto();
      const placed = (ai.auto?.activity || []).reduce((n, e) => n + e.placed.length, 0);
      if (placed !== lastPlaced) { lastPlaced = placed; window.dispatchEvent(new CustomEvent('ai-trader:order')); loadStatus(); }
    }, 15000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
