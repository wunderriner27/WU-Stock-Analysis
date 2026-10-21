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

  const ai = { status: null, plan: null, busy: false, done: {} };

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

  function confirmDialog(title, html, tone) {
    const dlg = $('confirm');
    $('c-title').textContent = title;
    $('c-body').innerHTML = html;
    const btn = $('c-ok');
    btn.textContent = 'Place order';
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

  function syncSymbol() {
    // Default the focus symbol to the one in the URL (#/trade/AAPL) when the box is empty.
    const m = location.hash.match(/^#\/trade\/([^/?#]+)/);
    if (m && !$('ai-symbol').value) $('ai-symbol').value = decodeURIComponent(m[1]).toUpperCase();
    if (location.hash.startsWith('#/trade')) loadStatus();
  }

  function init() {
    if (!$('ai-trader')) return;
    $('ai-form').addEventListener('submit', onAsk);
    $('ai-result').addEventListener('click', onResultClick);
    window.addEventListener('hashchange', syncSymbol);
    syncSymbol();
    loadStatus();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
