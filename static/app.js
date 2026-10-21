/* Market Terminal — vanilla JS front-end.
 * Hash router:  #/            -> Home
 *               #/ticker/AAPL -> Ticker analysis
 */
(() => {
  'use strict';

  // ------------------------------------------------------------------ utils
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const fmt = (v, d = 2) => (isNum(v) ? v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');
  const fmtPct = (v, d = 2) => (isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(d)}%` : '—');
  const fmtChg = (v, d = 2) => (isNum(v) ? `${v > 0 ? '+' : ''}${fmt(v, d)}` : '—');
  const dir = (v) => (!isNum(v) || v === 0 ? 'flat' : v > 0 ? 'up' : 'down');
  const fmtBig = (v) => {
    if (!isNum(v)) return '—';
    const a = Math.abs(v);
    for (const [n, s] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) if (a >= n) return `${(v / n).toFixed(2)}${s}`;
    return fmt(v, 0);
  };
  const priceDigits = (p) => (isNum(p) && Math.abs(p) < 1 ? 4 : 2);
  const ago = (iso) => {
    if (!iso) return '';
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!Number.isFinite(s)) return '';
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  };
  const chip = (label) => `<span class="chip chip-${esc(label)}">${esc(label)}</span>`;
  const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : '#');

  class ApiError extends Error {
    constructor(status, body) {
      const detail = Array.isArray(body?.detail) ? body.detail.map((d) => d.msg).join('; ') : body?.detail;
      super(body?.message || detail || `HTTP ${status}`);
      this.status = status; this.code = body?.error; this.retryAfter = body?.retry_after;
    }
  }

  /** fetch JSON with timeout + retry on 429/5xx/network errors.
   *  Non-GET requests are never retried: a retried order could be placed twice. */
  async function api(path, { retries = 2, timeout = 25000, method = 'GET', body: payload } = {}) {
    if (method !== 'GET') retries = 0;
    for (let attempt = 0; ; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeout);
      try {
        const res = await fetch(path, {
          method, signal: ctrl.signal,
          headers: { Accept: 'application/json', ...(payload ? { 'Content-Type': 'application/json' } : {}) },
          body: payload ? JSON.stringify(payload) : undefined,
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          const err = new ApiError(res.status, body);
          const transient = res.status === 429 || res.status >= 500;
          if (!transient || attempt >= retries) throw err;
          const wait = (Number(res.headers.get('Retry-After')) || 0) * 1000 || 800 * 2 ** attempt;
          await new Promise((r) => setTimeout(r, Math.min(wait, 8000)));
          continue;
        }
        return body;
      } catch (e) {
        if (e instanceof ApiError) throw e;
        if (attempt >= retries) throw new ApiError(0, { message: e.name === 'AbortError' ? 'Request timed out' : 'Network error — is the server running?' });
        await new Promise((r) => setTimeout(r, 800 * 2 ** attempt));
      } finally {
        clearTimeout(timer);
      }
    }
  }

  let toastTimer;
  function toast(msg, kind = 'error') {
    const t = $('toast');
    t.innerHTML = kind === 'ok'
      ? `<span class="text-up font-semibold mr-1">✓</span>${esc(msg)}`
      : `<span class="text-down font-semibold mr-1">!</span>${esc(msg)}`;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 5000);
  }

  const warningsHtml = (ws) => (ws?.length
    ? `<div class="panel px-3 py-2 text-[11px] text-amber-300/90 border-amber-500/30">⚠ ${ws.map(esc).join(' · ')}</div>` : '');

  // ------------------------------------------------------------------ clock
  function tickClock() {
    const now = new Date();
    const et = now.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false });
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short', hour: 'numeric', minute: 'numeric', hour12: false })
      .formatToParts(now).map((p) => [p.type, p.value]));
    const mins = Number(parts.hour) * 60 + Number(parts.minute);
    const weekday = !['Sat', 'Sun'].includes(parts.weekday);
    const state = !weekday ? 'CLOSED' : mins >= 570 && mins < 960 ? 'OPEN' : mins >= 240 && mins < 570 ? 'PRE' : mins >= 960 && mins < 1200 ? 'POST' : 'CLOSED';
    const color = state === 'OPEN' ? 'text-up' : state === 'CLOSED' ? 'text-down' : 'text-accent';
    $('market-clock').innerHTML = `${et} ET <span class="${color}">● ${state}</span>`;
  }

  // ------------------------------------------------------------------ ticker tape
  const TAPE_SYMS = ['SPY', 'QQQ', 'DIA', 'IWM', '^VIX'];
  const TAPE_LABEL = { '^VIX': 'VIX' };
  function renderTape(quotes) {
    if (!quotes?.length) return;
    const item = (q) => `<a href="#/ticker/${encodeURIComponent(q.symbol)}" class="flex items-center gap-2 px-5 border-r border-ink-800 h-7 hover:bg-ink-800">
        <span class="text-slate-300 font-semibold">${esc(TAPE_LABEL[q.symbol] || q.symbol)}</span>
        <span>${fmt(q.price)}</span>
        <span class="${dir(q.change)}">${fmtChg(q.change)} (${fmtPct(q.change_pct)})</span></a>`;
    // repeat enough to fill wide screens, then duplicate for seamless loop
    const one = Array(4).fill(quotes.map(item).join('')).join('');
    $('tape').innerHTML = one + one;
  }
  async function refreshTape() {
    try {
      const d = await api(`/api/quotes?symbols=${encodeURIComponent(TAPE_SYMS.join(','))}`, { retries: 1 });
      renderTape(TAPE_SYMS.map((s) => d.quotes[s]).filter(Boolean));
    } catch (e) { /* tape is non-critical */ }
  }

  // ------------------------------------------------------------------ watchlist
  // Kept in this browser's localStorage; quotes come from the batch /api/quotes route.
  const WL_KEY = 'mkt.watchlist';
  const WL_DEFAULT = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'SPY'];
  const wl = {
    syms: (() => {
      try {
        const v = JSON.parse(localStorage.getItem(WL_KEY));
        return Array.isArray(v) ? v.filter((s) => typeof s === 'string').slice(0, 50) : WL_DEFAULT.slice();
      } catch { return WL_DEFAULT.slice(); }
    })(),
    quotes: {},
    prev: {},
  };
  const wlSave = () => { try { localStorage.setItem(WL_KEY, JSON.stringify(wl.syms)); } catch { /* private mode */ } };
  const wlHas = (s) => wl.syms.includes(s);

  function wlToggle(sym, on = !wlHas(sym)) {
    if (on && !wlHas(sym)) {
      if (wl.syms.length >= 50) return toast('Watchlist is full (50 symbols)');
      wl.syms.push(sym);
    } else if (!on) {
      wl.syms = wl.syms.filter((s) => s !== sym);
    }
    wlSave();
    renderWatchlist();
    renderWatchButton();
    if (on) refreshWatchlist();
  }

  function renderWatchlist() {
    const row = (s) => {
      const q = wl.quotes[s];
      const dg = priceDigits(q?.price);
      const flash = q && isNum(wl.prev[s]) && q.price !== wl.prev[s] ? (q.price > wl.prev[s] ? 'flash-up' : 'flash-down') : '';
      return `<tr data-sym="${esc(s)}" class="cursor-pointer">
        <td><a href="#/ticker/${encodeURIComponent(s)}" class="text-link font-semibold num">${esc(s)}</a>${q?.name ? `<div class="text-[10px] text-slate-500 truncate max-w-[140px]">${esc(q.name)}</div>` : ''}</td>
        <td class="r num ${flash}">${q ? fmt(q.price, dg) : '<span class="text-slate-600">…</span>'}</td>
        <td class="r num ${dir(q?.change)}">${fmtChg(q?.change, dg)}</td>
        <td class="r num ${dir(q?.change_pct)}">${fmtPct(q?.change_pct)}</td>
        <td class="r num text-slate-400">${fmtBig(q?.volume)}</td>
        <td class="r"><button data-wl-remove="${esc(s)}" title="Remove ${esc(s)}" class="text-slate-600 hover:text-down px-1">×</button></td></tr>`;
    };
    $('watchlist').innerHTML = wl.syms.length
      ? wl.syms.map(row).join('')
      : '<tr><td colspan="6" class="text-slate-500 text-center py-4">Empty. Add a symbol above, or press ☆ on any ticker page.</td></tr>';

    const current = state.data?.symbol;
    $('wl-strip').innerHTML = wl.syms.map((s) => {
      const q = wl.quotes[s];
      const on = s === current;
      return `<a href="#/ticker/${encodeURIComponent(s)}" class="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded border ${on ? 'border-accent/60 bg-ink-800' : 'border-ink-700 bg-ink-900 hover:bg-ink-800'}">
        <span class="font-semibold ${on ? 'text-white' : 'text-slate-300'}">${esc(s)}</span>
        <span class="${dir(q?.change_pct)}">${fmtPct(q?.change_pct)}</span></a>`;
    }).join('');
  }

  function renderWatchButton() {
    const s = state.data?.symbol;
    const b = $('h-watch');
    if (!s) return;
    const on = wlHas(s);
    b.textContent = on ? '★ Watching' : '☆ Watch';
    b.classList.toggle('text-accent', on); b.classList.toggle('border-accent/60', on);
  }

  async function refreshWatchlist() {
    if (!wl.syms.length) return renderWatchlist();
    try {
      const d = await api(`/api/quotes?symbols=${encodeURIComponent(wl.syms.join(','))}`, { retries: 1 });
      for (const s of wl.syms) wl.prev[s] = wl.quotes[s]?.price;
      Object.assign(wl.quotes, d.quotes);
    } catch (e) { /* keep last quotes; watchlist is non-critical */ }
    renderWatchlist();
  }

  function wireWatchlist() {
    $('wl-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const s = $('wl-input').value.trim().toUpperCase();
      if (!s) return;
      if (!/^[A-Z0-9.\-^=]{1,15}$/.test(s)) return toast(`“${s}” is not a valid ticker format`);
      $('wl-input').value = '';
      if (wlHas(s)) return toast(`${s} is already on the watchlist`, 'ok');
      wlToggle(s, true);
    });
    $('watchlist').addEventListener('click', (e) => {
      const rm = e.target.closest('[data-wl-remove]');
      if (rm) { e.preventDefault(); return wlToggle(rm.dataset.wlRemove, false); }
      const tr = e.target.closest('tr[data-sym]');
      if (tr && !e.target.closest('a')) location.hash = `#/ticker/${encodeURIComponent(tr.dataset.sym)}`;
    });
    $('h-watch').addEventListener('click', () => state.data && wlToggle(state.data.symbol));
    renderWatchlist();
  }

  // ------------------------------------------------------------------ HOME
  async function loadHome() {
    const skel = (n, h = 'h-5') => Array(n).fill(`<tr><td colspan="4"><div class="skeleton ${h}"></div></td></tr>`).join('');
    if (!$('news-list').children.length) {
      $('news-list').innerHTML = Array(6).fill('<div class="bg-ink-900 p-3"><div class="skeleton h-24"></div></div>').join('');
      ['gainers', 'losers', 'macro', 'global-indices', 'calendar'].forEach((id) => ($(id).innerHTML = skel(5)));
      $('index-cards').innerHTML = Array(5).fill('<div class="panel p-3"><div class="skeleton h-12"></div></div>').join('');
    }
    let d;
    try {
      d = await api('/api/news');
    } catch (e) {
      $('home-warnings').innerHTML = warningsHtml([`Home data failed to load: ${e.message}`]);
      return;
    }
    $('home-warnings').innerHTML = warningsHtml(d.warnings);
    renderTape(d.tape.map((q) => q));

    // index cards (top 5 global)
    $('index-cards').innerHTML = d.indices.slice(0, 5).map((q) => `
      <a href="#/ticker/${encodeURIComponent(q.symbol)}" class="panel p-3 hover:border-ink-500 block">
        <div class="flex justify-between text-[10px] uppercase tracking-wider text-slate-500"><span>${esc(q.label)}</span><span class="num">${esc(q.symbol)}</span></div>
        <div class="num text-lg font-semibold mt-1">${fmt(q.price)}</div>
        <div class="num text-xs ${dir(q.change)}">${fmtChg(q.change)} · ${fmtPct(q.change_pct)}</div>
      </a>`).join('');

    // news
    const s = d.news_sentiment || {};
    $('news-sentiment').innerHTML = `<span class="chip chip-bullish">${s.bullish || 0} bull</span><span class="chip chip-bearish">${s.bearish || 0} bear</span><span class="chip chip-neutral">${s.neutral || 0} neutral</span>`;
    $('news-list').innerHTML = d.news.length ? d.news.map((n) => `
      <a href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer" class="bg-ink-900 hover:bg-ink-850 p-3 flex gap-3 group">
        ${n.thumbnail ? `<img src="${esc(n.thumbnail)}" alt="" loading="lazy" class="w-16 h-16 object-cover rounded shrink-0 opacity-80 group-hover:opacity-100" onerror="this.remove()">` : ''}
        <div class="min-w-0">
          <div class="flex items-center gap-2 text-[10px] text-slate-500 mb-1">${chip(n.sentiment.label)}<span class="truncate">${esc(n.publisher || '')}</span><span class="ml-auto num shrink-0">${ago(n.published)}</span></div>
          <div class="text-[13px] leading-snug font-medium text-slate-100 group-hover:text-link">${esc(n.title)}</div>
          ${n.summary ? `<div class="text-[11px] text-slate-500 mt-1 line-clamp-2">${esc(n.summary)}</div>` : ''}
        </div>
      </a>`).join('') : '<div class="bg-ink-900 p-6 text-slate-500 text-sm col-span-2">No news available.</div>';

    // movers
    const moverRow = (r) => `<tr class="cursor-pointer" onclick="location.hash='#/ticker/${encodeURIComponent(r.symbol)}'">
        <td class="num font-semibold text-slate-200">${esc(r.symbol)}</td><td class="r num">${fmt(r.price)}</td>
        <td class="r num ${dir(r.change_pct)}">${fmtPct(r.change_pct)}</td></tr>`;
    $('gainers').innerHTML = d.movers.gainers.map(moverRow).join('') || '<tr><td colspan="3" class="text-slate-500">—</td></tr>';
    $('losers').innerHTML = d.movers.losers.map(moverRow).join('') || '<tr><td colspan="3" class="text-slate-500">—</td></tr>';

    $('macro').innerHTML = d.macro.map((q) => `<tr><td><span class="text-slate-200">${esc(q.label)}</span> <span class="text-slate-600 num text-[10px]">${esc(q.symbol)}</span></td>
      <td class="r num">${fmt(q.price, q.symbol.startsWith('^') && q.price < 20 ? 3 : 2)}</td><td class="r num ${dir(q.change)}">${fmtChg(q.change, 3)}</td><td class="r num ${dir(q.change_pct)}">${fmtPct(q.change_pct)}</td></tr>`).join('');

    $('global-indices').innerHTML = d.indices.map((q) => `<tr class="cursor-pointer" onclick="location.hash='#/ticker/${encodeURIComponent(q.symbol)}'">
      <td>${esc(q.label)}</td><td class="r num">${fmt(q.price)}</td><td class="r num ${dir(q.change_pct)}">${fmtPct(q.change_pct)}</td></tr>`).join('');

    const impact = { high: 'bg-down', medium: 'bg-accent', low: 'bg-slate-600' };
    $('calendar').innerHTML = d.calendar.map((e) => {
      const dt = new Date(`${e.date}T12:00:00`);
      const label = e.days_out === 0 ? '<span class="text-accent">Today</span>' : e.days_out === 1 ? 'Tomorrow' : dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
      return `<tr><td class="num whitespace-nowrap">${label}</td><td class="num text-slate-500">${esc(e.time)}</td>
        <td>${e.estimated ? '<span class="text-slate-600">~</span> ' : ''}${esc(e.event)}</td>
        <td class="r"><span class="inline-flex gap-0.5">${[1, 2, 3].map((i) => `<span class="w-1.5 h-3 rounded-sm ${i <= ({ high: 3, medium: 2, low: 1 }[e.impact]) ? impact[e.impact] : 'bg-ink-700'}"></span>`).join('')}</span></td></tr>`;
    }).join('') || '<tr><td colspan="4" class="text-slate-500">No scheduled events.</td></tr>';

    $('home-updated').textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()} · provider: ${d.provider}`;
  }

  // ------------------------------------------------------------------ CHARTS
  Chart.defaults.color = '#8b98a9';
  Chart.defaults.font.family = "'IBM Plex Mono', monospace";
  Chart.defaults.font.size = 10;
  Chart.defaults.borderColor = '#151b25';
  const hasFinancial = !!Chart.registry.controllers.get?.('candlestick') || !!(Chart.controllers && Chart.controllers.candlestick);

  const state = { data: null, type: hasFinancial ? 'candlestick' : 'line', range: 126, overlays: { sma20: true, sma50: true, sma200: false, bb: false }, charts: {} };

  function xBounds() {
    const ts = state.data.history.dates;
    const n = Math.min(state.range, ts.length);
    return { min: new Date(ts[ts.length - n]).getTime() - 864e5 * 0.6, max: new Date(ts[ts.length - 1]).getTime() + 864e5 * 0.6, start: ts.length - n };
  }

  const timeScale = (showLabels = true) => ({
    type: 'timeseries',
    time: { unit: 'month', tooltipFormat: 'yyyy-LL-dd' },
    ticks: { display: showLabels, maxRotation: 0, autoSkip: true, autoSkipPadding: 20, source: 'auto' },
    grid: { color: '#11161e' },
  });

  // Draws a crosshair line at the hovered x on every chart.
  const crosshair = {
    id: 'crosshair',
    afterDraw(chart) {
      const a = chart.tooltip?.getActiveElements?.();
      if (!a?.length) return;
      const x = a[0].element.x; const { top, bottom } = chart.chartArea; const ctx = chart.ctx;
      ctx.save(); ctx.strokeStyle = 'rgba(148,163,184,.35)'; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
    },
  };

  function buildCharts() {
    const d = state.data;
    const ts = d.history.dates.map((s) => new Date(`${s}T00:00:00`).getTime());
    const S = d.indicators.series;
    const pts = (arr) => arr.map((y, i) => ({ x: ts[i], y }));
    Object.values(state.charts).forEach((c) => c.destroy());
    state.charts = {};

    const priceDs = state.type === 'candlestick' && hasFinancial
      ? { type: 'candlestick', label: d.symbol, yAxisID: 'y',
          data: ts.map((x, i) => ({ x, o: d.history.open[i], h: d.history.high[i], l: d.history.low[i], c: d.history.close[i] })),
          color: { up: '#22c55e', down: '#ef4444', unchanged: '#94a3b8' },
          borderColor: { up: '#22c55e', down: '#ef4444', unchanged: '#94a3b8' } }
      : { type: 'line', label: d.symbol, yAxisID: 'y', data: pts(d.history.close), borderColor: '#e2e8f0', borderWidth: 1.5, pointRadius: 0, tension: 0.1,
          fill: { target: 'origin', above: 'rgba(226,232,240,.04)' } };

    const line = (key, label, color, extra = {}) => ({ type: 'line', label, yAxisID: 'y', data: pts(S[key]), borderColor: color, borderWidth: 1.2, pointRadius: 0, spanGaps: false, hidden: false, _ov: key, ...extra });
    const datasets = [priceDs];
    const ov = state.overlays;
    if (ov.sma20) datasets.push(line('sma20', 'SMA 20', '#38bdf8'));
    if (ov.sma50) datasets.push(line('sma50', 'SMA 50', '#f59e0b'));
    if (ov.sma200) datasets.push(line('sma200', 'SMA 200', '#e879f9'));
    if (ov.bb) {
      datasets.push(line('bb_upper', 'BB Upper', 'rgba(148,163,184,.55)', { borderDash: [4, 3] }));
      datasets.push(line('bb_lower', 'BB Lower', 'rgba(148,163,184,.55)', { borderDash: [4, 3], fill: { target: '-1', above: 'rgba(148,163,184,.05)', below: 'rgba(148,163,184,.05)' } }));
    }
    const vMax = Math.max(...d.history.volume.filter(isNum));
    datasets.push({ type: 'bar', label: 'Volume', yAxisID: 'vol', data: pts(d.history.volume), order: 10,
      backgroundColor: d.history.close.map((c, i) => (i && c >= d.history.close[i - 1] ? 'rgba(34,197,94,.22)' : 'rgba(239,68,68,.22)')) });

    const { min, max } = xBounds();
    const common = {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: undefined, normalized: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: '#0b0f15', borderColor: '#2a3442', borderWidth: 1, titleFont: { weight: '600' }, padding: 8 } },
    };

    state.charts.price = new Chart($('price-chart'), {
      data: { datasets },
      options: {
        ...common,
        scales: {
          x: { ...timeScale(true), min, max },
          y: { position: 'right', grid: { color: '#11161e' }, ticks: { callback: (v) => fmt(v, priceDigits(v)) } },
          vol: { position: 'left', display: false, min: 0, max: vMax * 4, grid: { display: false } },
        },
        plugins: {
          ...common.plugins,
          legend: { display: true, labels: { boxWidth: 10, boxHeight: 2, filter: (it) => it.text !== 'Volume' } },
          tooltip: { ...common.plugins.tooltip, callbacks: {
            label: (ctx) => {
              const r = ctx.raw;
              if (r && r.o !== undefined) return `O ${fmt(r.o)}  H ${fmt(r.h)}  L ${fmt(r.l)}  C ${fmt(r.c)}`;
              if (ctx.dataset.label === 'Volume') return `Vol ${fmtBig(r.y)}`;
              return `${ctx.dataset.label}: ${fmt(r.y, priceDigits(r.y))}`;
            } } },
        },
      },
      plugins: [crosshair],
    });

    // RSI with 30/70 bands
    const rsiBands = {
      id: 'rsiBands',
      beforeDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { y } } = chart;
        ctx.save();
        ctx.fillStyle = 'rgba(148,163,184,.06)';
        ctx.fillRect(a.left, y.getPixelForValue(70), a.right - a.left, y.getPixelForValue(30) - y.getPixelForValue(70));
        ctx.strokeStyle = 'rgba(148,163,184,.3)'; ctx.setLineDash([3, 3]);
        [30, 50, 70].forEach((v) => { const py = y.getPixelForValue(v); ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke(); });
        ctx.restore();
      },
    };
    state.charts.rsi = new Chart($('rsi-chart'), {
      type: 'line',
      data: { datasets: [{ label: 'RSI', data: pts(S.rsi14), borderColor: '#a78bfa', borderWidth: 1.3, pointRadius: 0,
        segment: { borderColor: (c) => (c.p1.parsed.y >= 70 ? '#ef4444' : c.p1.parsed.y <= 30 ? '#22c55e' : '#a78bfa') } }] },
      options: { ...common, scales: { x: { ...timeScale(true), min, max }, y: { position: 'right', min: 0, max: 100, ticks: { stepSize: 10, callback: (v) => ([30, 50, 70].includes(v) ? v : '') }, grid: { display: false } } } },
      plugins: [rsiBands, crosshair],
    });

    // MACD histogram + lines
    state.charts.macd = new Chart($('macd-chart'), {
      data: { datasets: [
        { type: 'bar', label: 'Histogram', data: pts(S.macd_hist), order: 3,
          backgroundColor: S.macd_hist.map((v, i) => {
            const prev = S.macd_hist[i - 1];
            if (!isNum(v)) return 'transparent';
            const growing = isNum(prev) && Math.abs(v) > Math.abs(prev);
            return v >= 0 ? (growing ? 'rgba(34,197,94,.85)' : 'rgba(34,197,94,.4)') : (growing ? 'rgba(239,68,68,.85)' : 'rgba(239,68,68,.4)');
          }) },
        { type: 'line', label: 'MACD', data: pts(S.macd), borderColor: '#38bdf8', borderWidth: 1.2, pointRadius: 0, order: 1 },
        { type: 'line', label: 'Signal', data: pts(S.macd_signal), borderColor: '#f59e0b', borderWidth: 1.2, pointRadius: 0, order: 2 },
      ] },
      options: { ...common, scales: { x: { ...timeScale(true), min, max }, y: { position: 'right', grid: { color: '#11161e' }, ticks: { maxTicksLimit: 5 } } },
        plugins: { ...common.plugins, tooltip: { ...common.plugins.tooltip, callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.raw.y, 3)}` } } } },
      plugins: [crosshair],
    });
    applyRange();
  }

  /** Zoom x to the selected range and re-fit y to visible data. */
  function applyRange() {
    if (!state.data) return;
    const { min, max, start } = xBounds();
    const d = state.data; const S = d.indicators.series;
    const vis = (arr) => arr.slice(start).filter(isNum);
    const priceVals = [...vis(d.history.low), ...vis(d.history.high)];
    if (state.overlays.sma20) priceVals.push(...vis(S.sma20));
    if (state.overlays.sma50) priceVals.push(...vis(S.sma50));
    if (state.overlays.sma200) priceVals.push(...vis(S.sma200));
    if (state.overlays.bb) priceVals.push(...vis(S.bb_upper), ...vis(S.bb_lower));
    const lo = Math.min(...priceVals), hi = Math.max(...priceVals), pad = (hi - lo) * 0.05;
    const unit = state.range <= 21 ? 'day' : state.range <= 126 ? 'week' : 'month';
    for (const c of Object.values(state.charts)) {
      c.options.scales.x.min = min; c.options.scales.x.max = max; c.options.scales.x.time.unit = unit;
    }
    state.charts.price.options.scales.y.min = lo - pad;
    state.charts.price.options.scales.y.max = hi + pad;
    const m = [...vis(S.macd), ...vis(S.macd_signal), ...vis(S.macd_hist)];
    const mAbs = Math.max(...m.map(Math.abs)) * 1.1 || 1;
    state.charts.macd.options.scales.y.min = -mAbs; state.charts.macd.options.scales.y.max = mAbs;
    Object.values(state.charts).forEach((c) => c.update('none'));
  }

  function wireChartControls() {
    if (!hasFinancial) $('chart-type').querySelector('[data-type="candlestick"]').remove();
    $('chart-type').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      state.type = b.dataset.type;
      [...$('chart-type').children].forEach((x) => x.classList.toggle('active', x === b));
      if (state.data) buildCharts();
    });
    $('chart-range').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      state.range = Number(b.dataset.range);
      [...$('chart-range').children].forEach((x) => x.classList.toggle('active', x === b));
      applyRange();
    });
    $('overlay-toggles').addEventListener('change', (e) => {
      const k = e.target.dataset.ov; if (!k) return;
      state.overlays[k] = e.target.checked;
      if (state.data) buildCharts();
    });
    if (!hasFinancial) state.type = 'line';
  }

  // ------------------------------------------------------------------ TICKER
  let tickerReq = 0;
  let lastPrice = null;

  function renderHero(d, { flash = false } = {}) {
    const q = d.quote; const f = d.fundamentals || {};
    const dg = priceDigits(q.price);
    $('h-symbol').textContent = d.symbol;
    $('h-name').textContent = q.name || '';
    $('h-exchange').textContent = q.exchange || d.provider;
    $('h-exchange').classList.toggle('hidden', !q.exchange && !d.provider);
    const pe = $('h-price');
    pe.textContent = fmt(q.price, dg);
    if (flash && isNum(lastPrice) && q.price !== lastPrice) {
      pe.classList.remove('flash-up', 'flash-down'); void pe.offsetWidth;
      pe.classList.add(q.price > lastPrice ? 'flash-up' : 'flash-down');
    }
    lastPrice = q.price;
    $('h-trade').href = `#/trade/${encodeURIComponent(d.symbol)}`;
    $('h-trade').classList.toggle('hidden', !trade.status?.enabled);
    const ch = $('h-change');
    ch.className = `text-lg num font-medium ${dir(q.change)}`;
    ch.textContent = `${q.change > 0 ? '▲' : q.change < 0 ? '▼' : ''} ${fmtChg(q.change, dg)} (${fmtPct(q.change_pct)})`;
    $('h-meta').textContent = [q.currency, q.market_state && `mkt: ${q.market_state}`, `prev close ${fmt(q.prev_close, dg)}`, `as of ${new Date(d.generated_at).toLocaleTimeString()}`].filter(Boolean).join(' · ');

    $('h-bid').textContent = fmt(q.bid, dg);
    $('h-ask').textContent = fmt(q.ask, dg);
    $('h-bidsz').textContent = isNum(q.bid_size) ? `× ${fmtBig(q.bid_size)}` : 'size —';
    $('h-asksz').textContent = isNum(q.ask_size) ? `× ${fmtBig(q.ask_size)}` : 'size —';
    $('h-spread').textContent = isNum(q.spread) ? fmt(q.spread, dg === 4 ? 4 : 3) : '—';
    $('h-spreadbps').textContent = isNum(q.spread_bps) ? `${q.spread_bps.toFixed(1)} bps` : (q.bid ? '' : 'quotes outside RTH');
    const tot = (q.bid_size || 0) + (q.ask_size || 0);
    $('h-imb-bid').style.width = tot ? `${((q.bid_size || 0) / tot) * 100}%` : '50%';

    const relVol = isNum(q.volume) && isNum(q.avg_volume) && q.avg_volume ? q.volume / q.avg_volume : null;
    const wkPos = isNum(f.week52_high) && isNum(f.week52_low) && f.week52_high > f.week52_low ? ((q.price - f.week52_low) / (f.week52_high - f.week52_low)) * 100 : null;
    const stat = (k, v, cls = '') => `<div class="text-slate-500">${k}</div><div class="text-right ${cls}">${v}</div>`;
    $('h-stats').innerHTML = [
      stat('Open', fmt(q.open, dg)),
      stat('Day Range', `${fmt(q.day_low, dg)}–${fmt(q.day_high, dg)}`),
      stat('Volume', fmtBig(q.volume)),
      stat('Avg Vol', fmtBig(q.avg_volume)),
      stat('Rel Vol', isNum(relVol) ? `${relVol.toFixed(2)}×` : '—', relVol > 1.5 ? 'text-accent' : ''),
      stat('Mkt Cap', fmtBig(f.market_cap)),
      stat('52W Pos', isNum(wkPos) ? `${wkPos.toFixed(0)}%` : '—'),
      stat('ATR 14', fmt(d.performance?.atr14, dg)),
    ].join('');
  }

  function renderTechnicals(d) {
    const sig = d.indicators.signals;
    $('tech-table').innerHTML = sig.map((s) => `<tr><td class="text-slate-300">${esc(s.indicator)}</td><td class="r num">${isNum(s.value) ? fmt(s.value, Math.abs(s.value) < 10 ? 3 : 2) : '—'}</td><td>${chip(s.signal)}</td><td class="text-slate-500 text-[11px]">${esc(s.note)}</td></tr>`).join('');
    const bull = sig.filter((s) => s.signal === 'bullish').length, bear = sig.filter((s) => s.signal === 'bearish').length;
    const score = bull - bear;
    $('tech-score').innerHTML = `<span class="num ${dir(score)}">${bull}↑ ${bear}↓ · ${score > 1 ? 'BULLISH BIAS' : score < -1 ? 'BEARISH BIAS' : 'MIXED'}</span>`;
    const L = d.indicators.latest;
    $('rsi-last').innerHTML = `<span class="${L.rsi14 >= 70 ? 'down' : L.rsi14 <= 30 ? 'up' : ''}">${fmt(L.rsi14, 1)}</span>`;
    $('macd-last').innerHTML = `<span class="text-sky-400">${fmt(L.macd, 3)}</span> / <span class="text-amber-400">${fmt(L.macd_signal, 3)}</span> / <span class="${dir(L.macd_hist)}">${fmt(L.macd_hist, 3)}</span>`;

    const p = d.performance || {};
    const cells = ['1W', '1M', '3M', '6M', 'YTD', '1Y'].map((k) => `<td class="r num ${dir(p[k])}">${fmtPct(p[k], 1)}</td>`).join('');
    $('perf-table').innerHTML = `
      <tr>${['1W', '1M', '3M', '6M', 'YTD', '1Y'].map((k) => `<th class="r">${k}</th>`).join('')}</tr><tr>${cells}</tr>
      <tr><td colspan="3" class="text-slate-500">Realized vol (20d / 1y)</td><td colspan="3" class="r num">${fmt(p.vol_20d, 1)}% / ${fmt(p.vol_1y, 1)}%</td></tr>
      <tr><td colspan="3" class="text-slate-500">Max drawdown (1y)</td><td colspan="3" class="r num down">${fmtPct(p.max_drawdown_1y, 1)}</td></tr>
      <tr><td colspan="3" class="text-slate-500">EMA 12 / EMA 26</td><td colspan="3" class="r num">${fmt(L.ema12)} / ${fmt(L.ema26)}</td></tr>
      <tr><td colspan="3" class="text-slate-500">Bollinger (U / M / L)</td><td colspan="3" class="r num">${fmt(L.bb_upper)} / ${fmt(L.bb_mid)} / ${fmt(L.bb_lower)}</td></tr>`;
  }

  function renderFundamentals(d) {
    const f = d.fundamentals;
    if (!f) {
      $('fund-table').innerHTML = '<tr><td class="text-slate-500">Fundamentals unavailable for this symbol.</td></tr>';
      $('analyst').innerHTML = ''; $('an-rating').innerHTML = ''; $('f-sector').textContent = '';
      return;
    }
    $('f-sector').textContent = [f.sector, f.industry].filter(Boolean).join(' · ');
    const row = (a, av, b, bv) => `<tr><td class="text-slate-500">${a}</td><td class="r num">${av}</td><td class="text-slate-500 pl-6">${b}</td><td class="r num">${bv}</td></tr>`;
    const x = (v) => (isNum(v) ? `${fmt(v)}×` : '—');
    const pctv = (v) => (isNum(v) ? `${fmt(v)}%` : '—');
    $('fund-table').innerHTML = [
      row('Market Cap', fmtBig(f.market_cap), 'Revenue TTM', fmtBig(f.revenue_ttm)),
      row('P/E (TTM)', x(f.pe_trailing), 'P/E (Fwd)', x(f.pe_forward)),
      row('EPS (TTM)', fmt(f.eps_trailing), 'EPS (Fwd)', fmt(f.eps_forward)),
      row('PEG', fmt(f.peg), 'Price/Book', x(f.price_to_book)),
      row('Price/Sales', x(f.price_to_sales), 'Beta', fmt(f.beta)),
      row('Div Yield', pctv(f.dividend_yield), 'Payout', pctv(f.payout_ratio)),
      row('52W High', fmt(f.week52_high), '52W Low', fmt(f.week52_low)),
      row('Short Ratio', isNum(f.short_ratio) ? `${fmt(f.short_ratio)}d` : '—', 'Short % Float', pctv(f.short_pct_float)),
      row('Profit Margin', pctv(f.profit_margin), 'ROE', pctv(f.roe)),
      row('Debt/Equity', fmt(f.debt_to_equity), 'Float', fmtBig(f.float_shares)),
    ].join('');

    const a = f.analyst || {};
    const ratingColor = /buy/.test(a.rating || '') ? 'chip-bullish' : /sell|under/.test(a.rating || '') ? 'chip-bearish' : 'chip-neutral';
    $('an-rating').innerHTML = a.rating ? `<span class="chip ${ratingColor}">${esc(a.rating.replace('_', ' '))}</span>` : '';
    let html = '';
    if (isNum(a.mean)) {
      const pos = ((a.mean - 1) / 4) * 100;
      html += `<div><div class="flex justify-between text-[10px] text-slate-500 mb-1"><span>Strong Buy</span><span class="num text-slate-300">${fmt(a.mean)} · ${a.count ?? '?'} analysts</span><span>Sell</span></div>
        <div class="relative h-1.5 rounded bg-gradient-to-r from-up via-slate-500 to-down"><div class="absolute -top-1 w-1 h-3.5 bg-white rounded" style="left:calc(${pos}% - 2px)"></div></div></div>`;
    }
    if (a.breakdown) {
      const b = a.breakdown; const tot = Object.values(b).reduce((s, v) => s + v, 0) || 1;
      const colors = { strongBuy: '#16a34a', buy: '#4ade80', hold: '#64748b', sell: '#f87171', strongSell: '#b91c1c' };
      html += `<div><div class="flex h-4 rounded overflow-hidden text-[9px] num font-semibold text-ink-950">${Object.entries(b).filter(([, v]) => v).map(([k, v]) => `<div style="width:${(v / tot) * 100}%;background:${colors[k]}" class="flex items-center justify-center" title="${k}: ${v}">${v}</div>`).join('')}</div>
        <div class="flex justify-between text-[9px] text-slate-500 mt-1"><span>S.Buy</span><span>Buy</span><span>Hold</span><span>Sell</span><span>S.Sell</span></div></div>`;
    }
    if (isNum(a.target_mean)) {
      const lo = a.target_low, hi = a.target_high, px = d.quote.price;
      const span = hi - lo || 1;
      const p = (v) => Math.min(100, Math.max(0, ((v - lo) / span) * 100));
      html += `<div><div class="flex justify-between text-[10px] text-slate-500"><span>Price Targets</span><span class="num ${dir(a.upside_pct)}">mean ${fmt(a.target_mean)} (${fmtPct(a.upside_pct, 1)})</span></div>
        <div class="relative h-6 mt-1"><div class="absolute top-2.5 left-0 right-0 h-1 bg-ink-700 rounded"></div>
          <div class="absolute top-1 w-0.5 h-4 bg-accent" style="left:${p(a.target_mean)}%" title="Mean target"></div>
          ${isNum(px) ? `<div class="absolute top-1.5 w-2.5 h-2.5 rounded-full bg-white border-2 border-ink-950 -ml-1" style="left:${p(px)}%" title="Current price"></div>` : ''}
        </div><div class="flex justify-between text-[10px] num text-slate-500"><span>low ${fmt(lo)}</span><span>high ${fmt(hi)}</span></div></div>`;
    }
    $('analyst').innerHTML = html || '<div class="text-slate-500 text-xs">No analyst coverage.</div>';
  }

  function renderTickerNews(d) {
    $('tn-count').textContent = `${d.news.length} items`;
    $('ticker-news').innerHTML = d.news.length ? d.news.map((n) => `<tr>
      <td><a class="text-slate-200 hover:text-link leading-snug" href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
        <div class="text-[10px] text-slate-600">${esc(n.publisher || '')}</div></td>
      <td>${chip(n.sentiment.label)}</td><td class="r num text-slate-500">${ago(n.published)}</td></tr>`).join('')
      : '<tr><td colspan="3" class="text-slate-500">No recent headlines.</td></tr>';
  }

  async function loadTicker(symbol, { silent = false } = {}) {
    const my = ++tickerReq;
    if (!silent) {
      $('ticker-empty').classList.add('hidden');
      $('ticker-error').classList.add('hidden');
      $('ticker-body').classList.remove('hidden');
      $('ticker-body').style.opacity = '0.45';
    }
    try {
      const d = await api(`/api/stock/${encodeURIComponent(symbol)}`, { retries: silent ? 0 : 2 });
      if (my !== tickerReq) return; // a newer search superseded this one
      $('ticker-body').style.opacity = '1';
      $('ticker-warnings').innerHTML = warningsHtml(d.warnings);
      const symbolChanged = state.data?.symbol !== d.symbol;
      state.data = d;
      renderHero(d, { flash: silent });
      renderWatchButton(); renderWatchlist();
      if (!silent || symbolChanged) buildCharts();
      renderTechnicals(d);
      renderFundamentals(d);
      renderTickerNews(d);
      $('ticker-updated').textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()} · provider: ${d.provider} · ${d.history.dates.length} sessions`;
      document.title = `${d.symbol} ${fmt(d.quote.price, priceDigits(d.quote.price))} · Market Terminal`;
    } catch (e) {
      if (my !== tickerReq) return;
      if (silent) return; // background refresh failure: keep last good data
      $('ticker-body').classList.add('hidden');
      $('ticker-body').style.opacity = '1';
      const box = $('ticker-error');
      const title = e.status === 404 ? `Symbol “${esc(symbol)}” not found` : e.status === 429 ? 'Rate limited by data provider' : e.status === 504 ? 'Data provider timed out' : 'Could not load data';
      box.innerHTML = `<div class="text-down font-semibold mb-1">${title}</div><div class="text-xs text-slate-400">${esc(e.message)}</div>
        <button id="retry-btn" class="mt-3 text-xs px-3 py-1 rounded bg-ink-700 hover:bg-ink-600">Retry</button>`;
      box.classList.remove('hidden');
      $('retry-btn').onclick = () => loadTicker(symbol);
    }
  }

  // ------------------------------------------------------------------ TRADE
  const trade = { status: null, account: null, positions: [], orders: [], side: 'buy', quote: null, filter: 'all', busy: false };

  /** Promise<boolean> confirm dialog; body is trusted HTML built by us. */
  function confirmDialog({ title, html, ok = 'Confirm', tone = 'accent' }) {
    const dlg = $('confirm');
    $('c-title').textContent = title;
    $('c-body').innerHTML = html;
    const btn = $('c-ok');
    btn.textContent = ok;
    btn.className = `px-4 py-1.5 text-sm rounded font-semibold text-ink-950 ${tone === 'up' ? 'bg-up hover:bg-green-400' : tone === 'down' ? 'bg-down hover:bg-red-400' : 'bg-accent hover:bg-amber-400'}`;
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.returnValue = '';
      dlg.showModal();
      dlg.querySelector('button[value="cancel"]').focus(); // default focus on the safe choice
    });
  }

  async function loadTradeStatus() {
    try { trade.status = await api('/api/trading/status', { retries: 1 }); } catch { trade.status = { enabled: false, reason: 'Could not reach the server.' }; }
    const badge = $('nav-mode');
    if (trade.status.enabled) {
      const live = trade.status.mode === 'LIVE';
      badge.textContent = live ? 'LIVE' : 'PAPER';
      badge.className = `text-[9px] font-semibold px-1 rounded ${live ? 'bg-down text-white' : 'bg-up/20 text-up'}`;
    } else badge.className = 'hidden';
  }

  function ticketValues() {
    const symbol = $('t-symbol').value.trim().toUpperCase();
    const qty = Number($('t-qty').value);
    const type = $('t-type').value;
    const limit = Number($('t-limit').value);
    const tif = $('t-tif').value;
    const q = trade.quote && trade.quote.symbol === symbol ? trade.quote : null;
    const ref = type === 'limit' ? limit : q ? (trade.side === 'buy' ? q.ask || q.price : q.bid || q.price) : null;
    const held = trade.positions.find((p) => p.symbol === symbol);
    const longQty = held && held.side === 'long' ? held.qty : 0;
    const shortQty = trade.side === 'sell' && qty > longQty ? qty - longQty : 0; // shares sold short
    return { symbol, qty, type, limit, tif, q, ref, held, shortQty, est: isNum(ref) && qty > 0 ? ref * qty : null };
  }

  /** Returns an error string, or '' when the ticket is valid. */
  function ticketError(v) {
    if (!v.symbol) return 'Enter a symbol';
    if (!/^[A-Z]{1,5}$/.test(v.symbol)) return 'Only US stock tickers can be traded (e.g. AAPL)';
    if (!(v.qty > 0)) return 'Quantity must be greater than 0';
    if (v.qty !== Math.floor(v.qty) && (v.type !== 'market' || v.tif !== 'day')) return 'Fractional shares need a Market order with Day time-in-force';
    if (v.type === 'limit' && !(v.limit > 0)) return 'Enter a limit price';
    if (v.shortQty > 0 && v.shortQty % 1) return 'Fractional shares cannot be sold short';
    if (trade.side === 'buy' && isNum(v.est) && trade.account && v.est > trade.account.buying_power) return 'Estimated cost exceeds buying power';
    return '';
  }

  function renderTicket() {
    const v = ticketValues();
    [...$('t-side').children].forEach((b) => {
      const on = b.dataset.side === trade.side;
      b.className = `py-2 rounded text-sm font-semibold ${on ? (b.dataset.side === 'buy' ? 'bg-up text-ink-950' : 'bg-down text-white') : 'text-slate-400 hover:text-slate-200'}`;
    });
    $('t-limit-wrap').classList.toggle('hidden', v.type !== 'limit');
    const q = v.q;
    $('t-quote').innerHTML = q
      ? `<div class="flex justify-between"><span class="text-slate-300">${esc(q.symbol)} <span class="text-slate-100 text-sm">${fmt(q.price)}</span> <span class="${dir(q.change_pct)}">${fmtPct(q.change_pct)}</span></span>
         <span><span class="text-up">${fmt(q.bid)}</span> × <span class="text-down">${fmt(q.ask)}</span></span></div>
         ${v.held ? `<div class="mt-1">You hold <span class="text-slate-200">${fmt(v.held.qty, v.held.qty % 1 ? 4 : 0)}</span> sh @ ${fmt(v.held.avg_entry)}</div>` : ''}`
      : v.symbol ? '<span class="text-slate-600">Loading quote…</span>' : '';
    const acct = trade.account;
    const after = acct && isNum(v.est) ? acct.buying_power + (trade.side === 'buy' ? -v.est : v.est) : null;
    $('t-summary').innerHTML = `
      <tr><td class="text-slate-500">${v.type === 'limit' ? 'Limit price' : trade.side === 'buy' ? 'Est. price (ask)' : 'Est. price (bid)'}</td><td class="r num">${fmt(v.ref)}</td></tr>
      <tr><td class="text-slate-500">Est. ${trade.side === 'buy' ? 'cost' : 'proceeds'}</td><td class="r num text-slate-100">${isNum(v.est) ? '$' + fmt(v.est) : '—'}</td></tr>
      <tr><td class="text-slate-500">Buying power after</td><td class="r num">${isNum(after) ? '$' + fmt(after) : '—'}</td></tr>`;
    const shortNote = v.shortQty > 0 && v.qty > 0
      ? `<tr><td colspan="2" class="text-accent">⚠ ${v.held ? `You hold ${fmt(v.held.side === 'long' ? v.held.qty : 0, 0)} sh, so ` : `You don't own ${esc(v.symbol)}, so `}this sells ${fmt(v.shortQty, v.shortQty % 1 ? 4 : 0)} sh <b>short</b>.</td></tr>` : '';
    $('t-summary').insertAdjacentHTML('beforeend', shortNote);
    const err = ticketError(v);
    const touched = $('t-symbol').value.trim() !== '';
    $('t-error').textContent = touched ? err : '';
    $('t-error').classList.toggle('hidden', !touched || !err);
    const btn = $('t-submit');
    btn.disabled = !!err || trade.busy;
    btn.className = `w-full py-2.5 rounded font-semibold text-sm disabled:opacity-40 disabled:cursor-not-allowed ${trade.side === 'buy' ? 'bg-up text-ink-950' : 'bg-down text-white'}`;
    btn.textContent = trade.busy ? 'Submitting…' : `Review ${trade.side === 'buy' ? 'buy' : 'sell'} order`;
  }

  let quoteTimer;
  function scheduleQuote() {
    clearTimeout(quoteTimer);
    const sym = $('t-symbol').value.trim().toUpperCase();
    if (!/^[A-Z]{1,5}$/.test(sym)) { trade.quote = null; renderTicket(); return; }
    quoteTimer = setTimeout(async () => {
      try {
        const d = await api(`/api/quotes?symbols=${encodeURIComponent(sym)}`, { retries: 0 });
        if ($('t-symbol').value.trim().toUpperCase() === sym) trade.quote = d.quotes[sym] || null;
      } catch { trade.quote = null; }
      renderTicket();
    }, 350);
    renderTicket();
  }

  const statusTone = (s) => (s === 'filled' ? 'text-up' : /cancel|expired|rejected/.test(s) ? 'text-slate-500' : 'text-accent');
  const OPEN_STATES = ['new', 'accepted', 'pending_new', 'partially_filled', 'held', 'accepted_for_bidding', 'pending_replace'];

  function renderAccount() {
    const a = trade.account;
    const live = a.mode === 'LIVE';
    $('mode-banner').innerHTML = live
      ? `<div class="panel px-3 py-2 text-xs border-down bg-down/10 text-red-300 font-semibold">● LIVE ACCOUNT, REAL MONEY. ${a.live_orders_allowed ? 'Orders are enabled.' : 'Orders are blocked until ALPACA_ALLOW_LIVE_TRADING=yes.'}</div>`
      : '<div class="panel px-3 py-2 text-xs border-up/30 text-green-300/90">● Paper trading account. Orders are simulated, no real money.</div>';
    $('ticket-mode').innerHTML = live ? '<span class="chip chip-bearish">LIVE</span>' : '<span class="chip chip-bullish">PAPER</span>';
    const card = (label, value, cls = '') => `<div class="panel p-3"><div class="text-[10px] uppercase tracking-wider text-slate-500">${label}</div><div class="num text-lg font-semibold mt-1 ${cls}">${value}</div></div>`;
    $('acct-cards').innerHTML = [
      card('Equity', '$' + fmt(a.equity)),
      card('Day P/L', `${fmtChg(a.day_pl)} <span class="text-xs">${fmtPct(a.day_pl_pct)}</span>`, dir(a.day_pl)),
      card('Cash', '$' + fmt(a.cash)),
      card('Buying power', '$' + fmt(a.buying_power)),
      card('Long value', '$' + fmt(a.long_market_value)),
      card('Status', `${esc(a.status)}${a.trading_blocked ? ' · <span class="text-down">BLOCKED</span>' : ''}`, a.status === 'ACTIVE' ? 'text-up' : 'text-accent'),
    ].join('');

    const ps = trade.positions;
    const totPl = ps.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
    $('pos-total').innerHTML = ps.length ? `${ps.length} open · <span class="${dir(totPl)}">${fmtChg(totPl)}</span>` : '';
    $('positions').innerHTML = ps.length ? ps.map((p) => `<tr>
        <td><a href="#/ticker/${encodeURIComponent(p.symbol)}" class="num font-semibold text-slate-200 hover:text-link">${esc(p.symbol)}</a> ${p.side === 'short' ? '<span class="chip chip-bearish">short</span>' : ''}</td>
        <td class="r num">${fmt(p.qty, p.qty % 1 ? 4 : 0)}</td><td class="r num">${fmt(p.avg_entry)}</td><td class="r num">${fmt(p.current_price)}</td>
        <td class="r num">${fmt(p.market_value)}</td><td class="r num ${dir(p.change_today_pct)}">${fmtPct(p.change_today_pct)}</td>
        <td class="r num ${dir(p.unrealized_pl)}">${fmtChg(p.unrealized_pl)} <span class="text-[10px]">${fmtPct(p.unrealized_plpc)}</span></td>
        <td class="r whitespace-nowrap"><button data-fill="${esc(p.symbol)}" class="text-[11px] px-2 py-0.5 rounded bg-ink-700 hover:bg-ink-600">Trade</button>
          <button data-close="${esc(p.symbol)}" class="text-[11px] px-2 py-0.5 rounded bg-down/20 text-red-300 hover:bg-down/30">Close</button></td></tr>`).join('')
      : '<tr><td colspan="8" class="text-slate-500 py-4 text-center">No open positions.</td></tr>';
  }

  function renderOrders() {
    const os = trade.orders.filter((o) => trade.filter === 'all' || (trade.filter === 'open') === OPEN_STATES.includes(o.status));
    $('orders').innerHTML = os.length ? os.map((o) => `<tr>
        <td class="num text-slate-500 whitespace-nowrap">${o.submitted_at ? new Date(o.submitted_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
        <td class="num font-semibold">${esc(o.symbol)}</td><td class="${o.side === 'buy' ? 'text-up' : 'text-down'} uppercase text-[11px] font-semibold">${esc(o.side)}</td>
        <td class="r num">${isNum(o.qty) ? fmt(o.qty, o.qty % 1 ? 4 : 0) : '$' + fmt(o.notional)}</td><td class="text-slate-400">${esc(o.type)} · ${esc(o.time_in_force)}</td>
        <td class="r num">${fmt(o.limit_price)}</td><td class="r num">${fmt(o.filled_qty, o.filled_qty % 1 ? 4 : 0)}</td><td class="r num">${fmt(o.filled_avg_price)}</td>
        <td class="${statusTone(o.status)} text-[11px]">${esc(o.status.replace(/_/g, ' '))}</td>
        <td class="r">${OPEN_STATES.includes(o.status) ? `<button data-cancel="${esc(o.id)}" class="text-[11px] px-2 py-0.5 rounded bg-ink-700 hover:bg-ink-600">Cancel</button>` : ''}</td></tr>`).join('')
      : `<tr><td colspan="10" class="text-slate-500 py-4 text-center">No ${trade.filter === 'all' ? '' : trade.filter + ' '}orders.</td></tr>`;
  }

  async function loadTrade({ silent = false } = {}) {
    if (!trade.status) await loadTradeStatus();
    if (!trade.status.enabled) {
      $('trade-body').classList.add('hidden');
      $('trade-disabled').classList.remove('hidden');
      $('trade-disabled').innerHTML = `<div class="text-slate-200 font-semibold mb-1">Trading is not enabled</div>${esc(trade.status.reason)}`;
      return;
    }
    $('trade-disabled').classList.add('hidden');
    $('trade-body').classList.remove('hidden');
    try {
      const [acct, ords] = await Promise.all([api('/api/account', { retries: silent ? 0 : 1 }), api('/api/orders?status=all&limit=100', { retries: silent ? 0 : 1 })]);
      trade.account = acct.account; trade.positions = acct.positions; trade.orders = ords.orders;
      $('trade-warnings').innerHTML = '';
      renderAccount(); renderOrders(); renderTicket();
      $('trade-updated').textContent = `Updated ${new Date(acct.generated_at).toLocaleTimeString()} · refreshes every 10s`;
    } catch (e) {
      if (!silent || !trade.account) $('trade-warnings').innerHTML = warningsHtml([`Could not load account: ${e.message}`]);
    }
  }

  async function submitTicket(e) {
    e.preventDefault();
    const v = ticketValues();
    const err = ticketError(v);
    if (err || trade.busy) return;
    const live = trade.account?.mode === 'LIVE';
    const sideTxt = trade.side === 'buy' ? 'Buy' : 'Sell';
    const ok = await confirmDialog({
      title: `${live ? '⚠ LIVE ' : ''}${sideTxt} ${v.symbol}`,
      tone: trade.side === 'buy' ? 'up' : 'down',
      ok: `${v.shortQty > 0 ? 'Sell short' : sideTxt} ${fmt(v.qty, v.qty % 1 ? 4 : 0)} ${v.symbol}`,
      html: `${live ? '<div class="text-red-300 font-semibold">This is a LIVE account. This order uses real money.</div>' : '<div class="text-green-300/90 text-xs">Paper account: simulated order.</div>'}
        <table class="tbl num"><tbody>
        <tr><td class="text-slate-500">Side</td><td class="r ${trade.side === 'buy' ? 'text-up' : 'text-down'} font-semibold">${sideTxt.toUpperCase()}</td></tr>
        <tr><td class="text-slate-500">Quantity</td><td class="r">${fmt(v.qty, v.qty % 1 ? 4 : 0)} shares</td></tr>
        <tr><td class="text-slate-500">Type</td><td class="r">${v.type === 'limit' ? `Limit @ $${fmt(v.limit)}` : 'Market'} · ${v.tif.toUpperCase()}</td></tr>
        <tr><td class="text-slate-500">Est. ${trade.side === 'buy' ? 'cost' : 'proceeds'}</td><td class="r">${isNum(v.est) ? '$' + fmt(v.est) : '—'}</td></tr>
        </tbody></table>
        ${v.shortQty > 0 ? `<div class="text-accent text-xs font-semibold">⚠ This is a SHORT SALE of ${fmt(v.shortQty, 0)} shares. Losses are unlimited if the price rises.</div>` : ''}
        ${v.type === 'market' ? '<div class="text-[11px] text-slate-500">Market orders fill at the best available price, which may differ from the estimate. Outside market hours they queue until the open.</div>' : ''}`,
    });
    if (!ok) return;
    trade.busy = true; renderTicket();
    try {
      const o = await api('/api/orders', { method: 'POST', body: { symbol: v.symbol, qty: v.qty, side: trade.side, type: v.type, time_in_force: v.tif, limit_price: v.type === 'limit' ? v.limit : null } });
      toast(`${sideTxt} ${o.qty ?? v.qty} ${o.symbol} submitted (${o.status.replace(/_/g, ' ')})`, 'ok');
    } catch (err2) {
      toast(`Order failed: ${err2.message}`);
    } finally {
      trade.busy = false;
      await loadTrade({ silent: true });
    }
  }

  async function onTradeTableClick(e) {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.dataset.fill) {
      $('t-symbol').value = btn.dataset.fill; scheduleQuote(); $('t-qty').focus();
    } else if (btn.dataset.cancel) {
      const o = trade.orders.find((x) => x.id === btn.dataset.cancel);
      if (!o || !(await confirmDialog({ title: 'Cancel order', ok: 'Cancel order', html: `Cancel ${esc(o.side)} ${fmt(o.qty, 0)} ${esc(o.symbol)} (${esc(o.type)})?` }))) return;
      try { await api(`/api/orders/${encodeURIComponent(o.id)}`, { method: 'DELETE' }); toast('Cancel requested', 'ok'); } catch (err) { toast(`Cancel failed: ${err.message}`); }
      loadTrade({ silent: true });
    } else if (btn.dataset.close) {
      const p = trade.positions.find((x) => x.symbol === btn.dataset.close);
      const live = trade.account?.mode === 'LIVE';
      if (!p || !(await confirmDialog({ title: `${live ? '⚠ LIVE ' : ''}Close ${p.symbol}`, tone: 'down', ok: 'Close position',
        html: `Sell all <b>${fmt(p.qty, p.qty % 1 ? 4 : 0)}</b> shares of <b>${esc(p.symbol)}</b> at market (≈ $${fmt(p.market_value)}).${live ? '<div class="text-red-300 mt-2 font-semibold">LIVE account: real money.</div>' : ''}` }))) return;
      try { await api(`/api/positions/${encodeURIComponent(p.symbol)}`, { method: 'DELETE' }); toast(`Close order sent for ${p.symbol}`, 'ok'); } catch (err) { toast(`Close failed: ${err.message}`); }
      loadTrade({ silent: true });
    }
  }

  function wireTrade() {
    $('t-side').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; trade.side = b.dataset.side; renderTicket(); });
    $('t-symbol').addEventListener('input', scheduleQuote);
    ['t-qty', 't-limit'].forEach((id) => $(id).addEventListener('input', renderTicket));
    ['t-type', 't-tif'].forEach((id) => $(id).addEventListener('change', () => {
      if (id === 't-type' && $('t-type').value === 'limit' && !$('t-limit').value && trade.quote) $('t-limit').value = (trade.quote.price || 0).toFixed(2);
      renderTicket();
    }));
    $('ticket').addEventListener('submit', submitTicket);
    $('positions').addEventListener('click', onTradeTableClick);
    $('orders').addEventListener('click', onTradeTableClick);
    $('order-filter').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      trade.filter = b.dataset.status;
      [...$('order-filter').children].forEach((x) => x.classList.toggle('active', x === b));
      renderOrders();
    });
  }

  // ------------------------------------------------------------------ router
  let refreshTimer;
  function route() {
    const h = location.hash || '#/';
    const m = h.match(/^#\/ticker(?:\/([^/?#]+))?/);
    const tm = h.match(/^#\/trade(?:\/([^/?#]+))?/);
    const view = m ? 'ticker' : tm ? 'trade' : 'home';
    $('view-home').classList.toggle('hidden', view !== 'home');
    $('view-ticker').classList.toggle('hidden', view !== 'ticker');
    $('view-trade').classList.toggle('hidden', view !== 'trade');
    document.querySelectorAll('.nav-link').forEach((a) => {
      const on = a.dataset.nav === view;
      a.classList.toggle('bg-ink-800', on); a.classList.toggle('text-white', on); a.classList.toggle('text-slate-400', !on);
    });
    clearInterval(refreshTimer);

    if (view === 'home') {
      document.title = 'Market Terminal';
      loadHome();
      refreshTimer = setInterval(loadHome, 120000);
    } else if (view === 'trade') {
      document.title = 'Trade · Market Terminal';
      if (tm[1]) {
        $('t-symbol').value = decodeURIComponent(tm[1]).toUpperCase();
        scheduleQuote();
      }
      loadTrade();
      refreshTimer = setInterval(() => !document.hidden && !trade.busy && loadTrade({ silent: true }), 10000);
    } else {
      const sym = m[1] ? decodeURIComponent(m[1]).toUpperCase() : null;
      $('ticker-input').value = sym || '';
      if (sym) {
        loadTicker(sym);
        refreshTimer = setInterval(() => !document.hidden && loadTicker(sym, { silent: true }), 60000);
      } else {
        state.data = null;
        $('ticker-body').classList.add('hidden'); $('ticker-error').classList.add('hidden'); $('ticker-empty').classList.remove('hidden');
        setTimeout(() => $('ticker-input').focus(), 0);
      }
    }
  }

  const go = (sym) => {
    const s = (sym || '').trim().toUpperCase();
    if (!s) return;
    if (!/^[A-Z0-9.\-^=]{1,15}$/.test(s)) return toast(`“${s}” is not a valid ticker format`);
    const target = `#/ticker/${encodeURIComponent(s)}`;
    if (location.hash === target) loadTicker(s); else location.hash = target;
  };

  // ------------------------------------------------------------------ init
  function init() {
    wireChartControls();
    wireTrade();
    wireWatchlist();
    loadTradeStatus();
    $('ticker-form').addEventListener('submit', (e) => { e.preventDefault(); go($('ticker-input').value); });
    $('quick-search').addEventListener('submit', (e) => { e.preventDefault(); go($('quick-input').value); $('quick-input').value = ''; $('quick-input').blur(); });

    // Auto-submit: fire after the user pauses typing, or immediately on datalist pick.
    let debounce;
    $('ticker-input').addEventListener('input', (e) => {
      clearTimeout(debounce);
      if (!$('auto-submit').checked) return;
      const v = e.target.value.trim();
      const pickedFromList = e.inputType === 'insertReplacementText' || e.inputType === undefined;
      if (v.length >= 1) debounce = setTimeout(() => go(v), pickedFromList ? 0 : 900);
    });

    // "/" focuses search, Esc goes home
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $('quick-input').focus(); }
    });

    window.addEventListener('hashchange', route);
    tickClock(); setInterval(tickClock, 1000);
    refreshTape(); setInterval(() => !document.hidden && refreshTape(), 30000);
    refreshWatchlist(); setInterval(() => !document.hidden && refreshWatchlist(), 30000);
    route();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
