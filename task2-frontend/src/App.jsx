import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const PAGE_SIZES = [25, 50, 100, 200];
const SORT_FIELDS = { id: 'ID', name: 'Name', company: 'Company', country: 'Country', plan: 'Plan', status: 'Status', signup_date: 'Signup date', current_month_minutes: 'Minutes', current_month_sms: 'SMS', cost_cents: 'Spend', last_active: 'Last active' };
const DEFAULTS = { page: 1, page_size: 25, search: '', status: '', country: '', plan: '', date_from: '', date_to: '', sort_by: 'id', sort_dir: 'asc' };
const STEPS = ['Contact', 'Plan & billing', 'Usage caps', 'Webhook'];
const METRICS = {
  minutes: { label: 'Minutes', field: 'minutes', unit: 'min', color: 'blue' },
  sms: { label: 'SMS', field: 'sms', unit: 'messages', color: 'blue' },
  spend: { label: 'Spend', field: 'cost_cents', unit: 'cents', color: 'blue' },
};
const money = (cents = 0) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(cents || 0) / 100);
const compactNumber = (value) => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value || 0);

function metricValue(metric, value) {
  return metric === 'spend' ? money(value) : `${Number(value || 0).toLocaleString()} ${metric === 'sms' ? 'SMS' : 'min'}`;
}

function summarizePeriods(series, limit = 14) {
  if (series.length <= limit) return series.map((point) => ({ ...point, key: point.date, period: point.date }));
  const result = [];
  const size = Math.ceil(series.length / limit);
  for (let start = 0; start < series.length; start += size) {
    const points = series.slice(start, start + size);
    const first = points[0];
    const last = points.at(-1);
    result.push({
      date: first.date,
      key: first.date,
      period: `${first.date} – ${last.date}`,
      minutes: points.reduce((sum, point) => sum + Number(point.minutes || 0), 0),
      sms: points.reduce((sum, point) => sum + Number(point.sms || 0), 0),
      cost_cents: points.reduce((sum, point) => sum + Number(point.cost_cents || 0), 0),
      signups: points.reduce((sum, point) => sum + Number(point.signups || 0), 0),
    });
  }
  return result;
}

function UsagePrism({ data, metric, selectedDate, onSelect }) {
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 650px)').matches);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 650px)');
    const update = () => setCompact(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const { label, field } = METRICS[metric];
  const width = compact ? 430 : 1000;
  const viewHeight = compact ? 318 : 292;
  const plotLeft = compact ? 38 : 58;
  const plotRight = compact ? 406 : 945;
  const baseY = compact ? 254 : 232;
  const plotHeight = compact ? 175 : 160;
  const depth = compact ? 9 : 12;
  const depthY = compact ? 7 : 8;
  const axisX = compact ? 31 : 42;
  const plotWidth = plotRight - plotLeft;
  const slot = data.length ? plotWidth / data.length : plotWidth;
  const barWidth = Math.max(8, slot * 0.58);
  const maxValue = Math.max(1, ...data.map((point) => Number(point[field] || 0)));
  const tickValues = [0, 0.25, 0.5, 0.75, 1];
  const visibleTicks = new Set([0, Math.round((data.length - 1) / 3), Math.round((data.length - 1) * 2 / 3), data.length - 1]);

  return <svg className="usage-prism" viewBox={`0 0 ${width} ${viewHeight}`} role="group" aria-label={`${label} usage by period. Use Tab to focus a period and arrow keys to move between periods.`}>
    <defs>
      <linearGradient id="prism-front" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--prism-front-top)" stopOpacity=".92" /><stop offset="100%" stopColor="var(--prism-front-bottom)" stopOpacity=".76" /></linearGradient>
      <linearGradient id="prism-top" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stopColor="var(--prism-top-start)" stopOpacity=".95" /><stop offset="100%" stopColor="var(--prism-top-end)" stopOpacity=".82" /></linearGradient>
      <linearGradient id="prism-side" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stopColor="var(--prism-side-start)" stopOpacity=".72" /><stop offset="100%" stopColor="var(--prism-side-end)" stopOpacity=".56" /></linearGradient>
      <linearGradient id="prism-floor" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stopColor="#9dc5f8" stopOpacity=".1" /><stop offset="100%" stopColor="#70a9ed" stopOpacity=".015" /></linearGradient>
      <filter id="prism-glow" x="-60%" y="-30%" width="220%" height="170%"><feGaussianBlur stdDeviation="8" /></filter>
    </defs>
    {tickValues.map((fraction) => {
      const y = baseY - plotHeight * fraction;
      const value = maxValue * fraction;
      const tickLabel = metric === 'spend' ? (value < 100 ? money(value) : `$${compactNumber(value / 100)}`) : compactNumber(value);
      return <g key={fraction} className="chart-gridline"><text x={axisX} y={y + 4} textAnchor="end">{tickLabel}</text><path d={`M${plotLeft} ${y} H${plotRight}`} /><path d={`M${plotRight} ${y} l${depth} ${-depthY}`} className="chart-gridline-depth" /></g>;
    })}
    <path className="prism-floor" d={`M${plotLeft} ${baseY} H${plotRight + depth} L${plotRight + depth} ${baseY - depthY} H${plotLeft + depth} Z`} />
    {data.map((point, index) => {
      const value = Number(point[field] || 0);
      const height = Math.max(value ? 5 : 0, (value / maxValue) * plotHeight);
      const x = plotLeft + index * slot + (slot - barWidth) / 2;
      const y = baseY - height;
      const selected = point.key === selectedDate;
      const labelX = x + barWidth / 2 + depth / 2;
      const periodLabel = point.period.length > 12 ? `${point.period.slice(5, 10)}…` : point.period.slice(5);
      return <g key={point.key} className={`prism-bar ${selected ? 'is-selected' : ''}`} role="button" aria-label={`${point.period}, ${metricValue(metric, value)}. Select period.`} aria-pressed={selected} tabIndex={0}
        onMouseEnter={() => onSelect(point.key)} onFocus={() => onSelect(point.key)} onClick={() => onSelect(point.key)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(point.key); }
          if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
            event.preventDefault();
            const next = Math.max(0, Math.min(data.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
            document.getElementById(`prism-${data[next]?.key}`)?.focus();
          }
        }} id={`prism-${point.key}`}>
        <rect className="prism-hit-area" x={plotLeft + index * slot} y={baseY - plotHeight - depthY} width={slot} height={plotHeight + depthY + 34} />
        {selected && <path className="prism-halo" filter="url(#prism-glow)" d={`M${x - 2} ${baseY} v${-height} h${barWidth + depth} v${height} Z`} />}
        <path className="prism-shadow" d={`M${x} ${baseY + 4} h${barWidth + depth} l${depth / 2} ${depthY / 2} h${-barWidth - depth} Z`} />
        <path className="prism-side" d={`M${x + barWidth} ${y} l${depth} ${-depthY} v${height} l${-depth} ${depthY} Z`} />
        <path className="prism-front" d={`M${x} ${y} h${barWidth} v${height} h${-barWidth} Z`} />
        <path className="prism-top" d={`M${x} ${y} l${depth} ${-depthY} h${barWidth} l${-depth} ${depthY} Z`} />
        <path className="prism-glint" d={`M${x + 2} ${y + 4} v${Math.max(0, height - 8)}`} />
        {visibleTicks.has(index) && <text className="prism-period" x={labelX} y={baseY + 29} textAnchor="middle">{periodLabel}</text>}
        <title>{`${point.period}: ${metricValue(metric, value)}`}</title>
      </g>;
    })}
    <path className="prism-baseline" d={`M${plotLeft} ${baseY} H${plotRight + depth}`} />
  </svg>;
}

function readQuery() {
  const params = new URLSearchParams(location.search);
  const next = { ...DEFAULTS };
  for (const key of Object.keys(next)) if (params.has(key)) next[key] = key === 'page' || key === 'page_size' ? Number(params.get(key)) : params.get(key);
  next.page = Number.isFinite(next.page) && next.page > 0 ? next.page : 1;
  next.page_size = PAGE_SIZES.includes(next.page_size) ? next.page_size : 25;
  next.sort_by = SORT_FIELDS[next.sort_by] ? next.sort_by : 'id';
  next.sort_dir = next.sort_dir === 'desc' ? 'desc' : 'asc';
  return next;
}

function readTheme() {
  try {
    const stored = localStorage.getItem('callzo-theme');
    return stored === 'light' || stored === 'dark' ? stored : 'dark';
  } catch { return 'dark'; }
}

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let payload = null;
    try { payload = await response.json(); detail = payload.detail?.errors ? Object.values(payload.detail.errors).join(' ') : payload.detail || detail; } catch { /* response had no JSON body */ }
    throw Object.assign(new Error(String(detail)), { status: response.status, payload });
  }
  return response.json();
}

function GlassButton({ children, className = '', ...props }) {
  return <button className={`button-glass ${className}`} {...props}>{children}</button>;
}

export default function App() {
  const [theme, setTheme] = useState(readTheme);
  const [filters, setFilters] = useState(readQuery);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [tableStatus, setTableStatus] = useState('loading');
  const [tableError, setTableError] = useState('');
  const [selected, setSelected] = useState(() => new Set());
  const [chart, setChart] = useState({ series: [], total_accounts: 0 });
  const [chartStatus, setChartStatus] = useState('loading');
  const [chartError, setChartError] = useState('');
  const [chartMetric, setChartMetric] = useState('minutes');
  const [selectedChartDate, setSelectedChartDate] = useState('');
  const [drawerAccount, setDrawerAccount] = useState(null);
  const [step, setStep] = useState(1);
  const [draft, setDraft] = useState({ 1: { name: '', email: '' }, 2: { plan: 'pro', billing_country: 'US' }, 3: { minutes_cap: '', sms_cap: '' }, 4: { webhook_url: '' } });
  const [saved, setSaved] = useState({});
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [formMessage, setFormMessage] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [now, setNow] = useState(Date.now());
  const drawerRef = useRef(null);
  const previousFocus = useRef(null);
  const accountRequest = useRef(0);
  const settingsRequest = useRef(0);
  const chartRequest = useRef(0);
  const saveQueue = useRef(Promise.resolve());
  const lastSaveFailure = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('callzo-theme', theme); } catch { /* appearance still applies for this session */ }
  }, [theme]);

  useEffect(() => {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => { if (value !== '' && value != null) params.set(key, String(value)); });
    history.replaceState(null, '', `${location.pathname}?${params}`);
  }, [filters]);

  const loadAccounts = useCallback(async ({ background = false } = {}) => {
    const requestId = ++accountRequest.current;
    if (!background) setTableStatus((current) => current === 'ready' ? current : 'loading');
    try {
      const params = new URLSearchParams();
      Object.entries(filters).forEach(([key, value]) => { if (value !== '' && value != null) params.set(key, String(value)); });
      const result = await request(`/api/accounts?${params}`);
      if (requestId !== accountRequest.current) return;
      setRows(Array.isArray(result.data) ? result.data : []);
      setTotal(Number(result.total) || 0);
      setTableError(''); setTableStatus('ready'); setLastUpdated(Date.now());
    } catch (error) {
      if (requestId !== accountRequest.current) return;
      setTableError(error.message);
      setTableStatus((current) => current === 'ready' ? 'ready' : 'error');
    }
  }, [filters]);

  useEffect(() => { loadAccounts(); }, [loadAccounts]);
  useEffect(() => {
    const timer = setInterval(() => loadAccounts({ background: true }), 15000);
    return () => clearInterval(timer);
  }, [loadAccounts]);

  const loadChart = useCallback(async () => {
    const requestId = ++chartRequest.current;
    if (!chart.series.length) setChartStatus('loading');
    try {
      const params = new URLSearchParams();
      if (filters.date_from) params.set('date_from', filters.date_from);
      if (filters.date_to) params.set('date_to', filters.date_to);
      const result = await request(`/api/accounts/summary?${params}`);
      if (requestId !== chartRequest.current) return;
      setChart({ series: result.series || [], total_accounts: result.total_accounts || 0 }); setChartError(''); setChartStatus('ready');
    } catch (error) {
      if (requestId !== chartRequest.current) return;
      setChartError(error.message); setChartStatus(chart.series.length ? 'stale' : 'error');
    }
  }, [filters.date_from, filters.date_to, chart.series.length]);
  useEffect(() => { loadChart(); }, [loadChart]);

  const setFilter = (key, value) => setFilters((old) => ({ ...old, [key]: value, page: key === 'page' ? value : 1 }));
  const updateDraft = (target, value) => {
    setDraft((old) => ({ ...old, [step]: { ...old[step], [target]: value } }));
    setErrors((old) => ({ ...old, [step]: { ...old[step], [target]: undefined, _general: undefined } }));
    setFormMessage(null);
  };

  const closeDrawer = useCallback(() => {
    setDrawerAccount(null); setFormMessage(null);
    requestAnimationFrame(() => previousFocus.current?.focus());
  }, []);

  useEffect(() => {
    if (!drawerAccount) return undefined;
    previousFocus.current = document.activeElement;
    requestAnimationFrame(() => drawerRef.current?.querySelector('input,select,button')?.focus());
    const onKey = (event) => {
      if (event.key === 'Escape') closeDrawer();
      if (event.key !== 'Tab') return;
      const items = [...drawerRef.current.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href]')];
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drawerAccount, closeDrawer]);

  const openSettings = async (account) => {
    setDrawerAccount(account); setStep(1); setErrors({}); setFormMessage(null); setSaving(false);
    const requestId = ++settingsRequest.current;
    try {
      const settings = await request(`/api/accounts/${account.id}/settings`);
      if (requestId !== settingsRequest.current) return;
      const next = {
        1: { name: settings['1']?.name ?? account.name, email: settings['1']?.email ?? account.email },
        2: { plan: settings['2']?.plan ?? account.plan, billing_country: settings['2']?.billing_country ?? account.country ?? 'US' },
        3: { minutes_cap: settings['3']?.minutes_cap ?? '', sms_cap: settings['3']?.sms_cap ?? '' },
        4: { webhook_url: settings['4']?.webhook_url ?? '' },
      };
      setSaved(settings || {}); setDraft(next);
    } catch (error) { setSaved({}); setFormMessage({ type: 'error', text: `Could not load settings: ${error.message}` }); }
  };

  const saveStep = async (number = step, values = draft[number]) => {
    if (!drawerAccount) return false;
    const task = async () => {
      setSaving(true);
      try {
        const result = await request(`/api/accounts/${drawerAccount.id}/settings/step/${number}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ data: values }) });
        setSaved((old) => ({ ...old, [String(number)]: result.data }));
        setErrors((old) => ({ ...old, [number]: {} }));
        lastSaveFailure.current = null;
        setFormMessage({ type: 'success', text: `${STEPS[number - 1]} saved` });
        return true;
      } catch (error) {
        lastSaveFailure.current = error;
        const stepErrors = error.status === 422 ? error.payload?.detail?.errors : null;
        const targetStep = error.status === 422 ? Number(error.payload?.detail?.step) || number : number;
        setErrors((old) => ({ ...old, [targetStep]: stepErrors || { _general: error.message } }));
        setFormMessage({ type: 'error', text: error.message });
        return false;
      } finally { setSaving(false); }
    };
    const operation = saveQueue.current.then(task, task);
    saveQueue.current = operation.then(() => undefined, () => undefined);
    return operation;
  };

  const moveStep = async (next) => {
    if (next === step) return;
    await saveStep(step);
    // Keep navigation available after an upstream validation rejection. The
    // failed step remains marked and is surfaced again during final submit.
    setStep(next);
  };

  const submitAll = async (event) => {
    event.preventDefault(); if (!drawerAccount) return;
    setSubmitting(true); setFormMessage(null);
    try {
      for (const number of [1, 2, 3, 4]) {
        const current = draft[number];
        if (JSON.stringify(saved[String(number)] || null) !== JSON.stringify(current)) {
          const ok = await saveStep(number, current);
          if (!ok) {
            // An existing Step 2 snapshot remains valid when the API begins rejecting it after Step 4.
            if (number === 2 && saved['2'] && lastSaveFailure.current?.status === 422) continue;
            setStep(number); setSubmitting(false); return;
          }
        }
      }
      await request(`/api/accounts/${drawerAccount.id}/settings/submit`, { method: 'POST' });
      setFormMessage({ type: 'success', text: 'Settings submitted successfully.' });
      loadAccounts({ background: true });
    } catch (error) { setFormMessage({ type: 'error', text: error.message }); }
    finally { setSubmitting(false); }
  };

  const pageCount = Math.max(1, Math.ceil(total / filters.page_size));
  const checkedOnPage = rows.length > 0 && rows.every((row) => selected.has(row.id));
  const chartData = useMemo(() => summarizePeriods(chart.series), [chart.series]);
  const selectedChartPoint = chartData.find((point) => point.key === selectedChartDate) || chartData.at(-1);
  const effectiveChartDate = selectedChartPoint?.key || '';
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);

  return <div className="app-shell" data-theme={theme}>
    <div className="ambient ambient-one" /><div className="ambient ambient-two" />
    <header className="topbar glass-surface">
      <a className="brand" href="#top" aria-label="Callzo console home"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 48 48"><defs><linearGradient id="callzo-c" x1="8" y1="7" x2="40" y2="42" gradientUnits="userSpaceOnUse"><stop stopColor="#f7fbff"/><stop offset=".48" stopColor="#a9ceff"/><stop offset="1" stopColor="#75e2d0"/></linearGradient></defs><path className="brand-c" d="M34.7 14.1A15.6 15.6 0 1 0 35 33.8" fill="none" stroke="url(#callzo-c)" strokeWidth="7.2" strokeLinecap="round"/><path className="brand-glint" d="M32.5 11.5 37 8.8" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round"/><circle cx="36.4" cy="36.2" r="2.2" fill="#82e8d4"/></svg></span><span><strong>Callzo</strong><small>Usage console</small></span></a>
      <div className="header-tools"><GlassButton className="theme-toggle" onClick={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} appearance`} title="Change appearance"><span aria-hidden="true">{theme === 'dark' ? '☼' : '◐'}</span><span className="theme-label">{theme === 'dark' ? 'Light' : 'Dark'}</span></GlassButton><div className="header-meta"><span className="live-dot" /> Live account data <span className="meta-divider" /> Updated {lastUpdated ? `${Math.max(0, Math.floor((now - lastUpdated) / 1000))}s ago` : '—'}</div></div>
    </header>

    <main id="top" className="content-wrap">
      <section className="intro"><div><p className="eyebrow">OPERATIONS / USAGE</p><h1>Accounts at a glance.</h1><p className="lede">Monitor usage, find the accounts that need attention, and manage their settings.</p></div><div className="intro-stat"><span>ACCOUNTS</span><strong>{total.toLocaleString()}</strong><small>in this view</small></div></section>

      <section className="glass-surface summary-card" aria-labelledby="summary-title">
        <div className="section-heading"><div><p className="eyebrow">LIVE USAGE PRISM</p><h2 id="summary-title">Usage, in perspective.</h2><p className="muted">{chart.total_accounts.toLocaleString()} accounts · Daily totals in a dimensional timeline</p></div><div className="range-controls"><label>From<input type="date" value={filters.date_from} onChange={(e) => setFilter('date_from', e.target.value)} /></label><label>To<input type="date" value={filters.date_to} onChange={(e) => setFilter('date_to', e.target.value)} /></label></div></div>
        <div className="chart-toolbar"><div className="chart-hint"><span className="prism-key" /> Select a prism to inspect its period</div><div className="metric-switch" role="group" aria-label="Choose a usage measure">{Object.entries(METRICS).map(([key, item]) => <button key={key} type="button" className={chartMetric === key ? 'active' : ''} aria-pressed={chartMetric === key} onClick={() => setChartMetric(key)}>{item.label}</button>)}</div></div>
        <div className="chart-frame">
          {chartStatus === 'loading' && <div className="state-inline"><span className="spinner" /> Loading summary…</div>}
          {chartStatus === 'error' && <div className="chart-fallback"><strong>Summary isn’t available right now.</strong><span>{chartError}</span><GlassButton onClick={loadChart}>Try again</GlassButton></div>}
          {(chartStatus === 'ready' || chartStatus === 'stale') && (chartData.length ? <><UsagePrism data={chartData} metric={chartMetric} selectedDate={effectiveChartDate} onSelect={setSelectedChartDate} /><div className="chart-readout"><span className="readout-period">{selectedChartPoint?.period || '—'}</span><strong>{selectedChartPoint ? metricValue(chartMetric, selectedChartPoint[METRICS[chartMetric].field]) : '—'}</strong><span className="readout-context">{METRICS[chartMetric].label} across {chart.total_accounts.toLocaleString()} accounts</span></div></> : <div className="state-inline">No summary data for this date range.</div>)}
          {chartStatus === 'stale' && <div className="stale-note" role="status">Showing the last saved summary. Refresh failed: {chartError} <button onClick={loadChart}>Retry</button></div>}
        </div>
      </section>

      <section className="glass-surface accounts-card" aria-labelledby="accounts-title">
        <div className="accounts-heading"><div><p className="eyebrow">DIRECTORY</p><h2 id="accounts-title">Account usage</h2><p className="muted">Search, filter and sort the full account list.</p></div><div className="selection-pill">{selected.size} selected</div></div>
        <div className="filter-grid">
          <label className="search-control">Search accounts<input type="search" placeholder="Name, email, or company" value={filters.search} onChange={(e) => setFilter('search', e.target.value)} /></label>
          <label>Status<select value={filters.status} onChange={(e) => setFilter('status', e.target.value)}><option value="">All statuses</option><option value="active">Active</option><option value="trial">Trial</option><option value="suspended">Suspended</option></select></label>
          <label>Plan<select value={filters.plan} onChange={(e) => setFilter('plan', e.target.value)}><option value="">All plans</option><option value="basic">Basic</option><option value="pro">Pro</option><option value="enterprise">Enterprise</option></select></label>
          <label>Country<select value={filters.country} onChange={(e) => setFilter('country', e.target.value)}><option value="">All countries</option>{['US','IN','GB','CA','AU','DE','SG','JP'].map((country) => <option key={country}>{country}</option>)}</select></label>
          <label>Sort by<select value={filters.sort_by} onChange={(e) => setFilter('sort_by', e.target.value)}>{Object.entries(SORT_FIELDS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>Direction<select value={filters.sort_dir} onChange={(e) => setFilter('sort_dir', e.target.value)}><option value="asc">Ascending</option><option value="desc">Descending</option></select></label>
        </div>
        {tableError && rows.length > 0 && <div className="inline-error" role="status">Couldn’t refresh the table: {tableError} <button onClick={() => loadAccounts()}>Retry</button></div>}
        {tableStatus === 'loading' && <div className="table-state"><span className="spinner" /> Loading accounts…</div>}
        {tableStatus === 'error' && <div className="table-state error-state"><strong>Accounts couldn’t be loaded.</strong><span>{tableError}</span><GlassButton onClick={() => loadAccounts()}>Try again</GlassButton></div>}
        {tableStatus === 'ready' && rows.length === 0 && <div className="table-state"><strong>No accounts match these filters.</strong><span>Try changing your search or clearing a filter.</span><GlassButton onClick={() => setFilters({ ...DEFAULTS, page_size: filters.page_size })}>Clear filters</GlassButton></div>}
        {rows.length > 0 && <><div className="table-scroll"><table><thead><tr><th><input type="checkbox" aria-label="Select all accounts on this page" checked={checkedOnPage} onChange={() => setSelected((old) => { const next = new Set(old); if (checkedOnPage) rows.forEach((row) => next.delete(row.id)); else rows.forEach((row) => next.add(row.id)); return next; })} /></th><th>ID</th><th>Account</th><th>Company</th><th>Plan</th><th>Status</th><th className="num">Minutes</th><th className="num">SMS</th><th className="num">Spend</th><th></th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td><input type="checkbox" aria-label={`Select ${row.name}`} checked={selected.has(row.id)} onChange={() => setSelected((old) => { const next = new Set(old); next.has(row.id) ? next.delete(row.id) : next.add(row.id); return next; })} /></td><td className="mono">#{row.id}</td><td><strong>{row.name}</strong><small>{row.email}</small></td><td>{row.company}</td><td><span className={`plan-tag plan-${row.plan}`}>{row.plan}</span></td><td><span className={`status-tag status-${row.status}`}><i />{row.status}</span></td><td className="num mono">{Number(row.current_month_minutes || 0).toLocaleString()}</td><td className="num mono">{Number(row.current_month_sms || 0).toLocaleString()}</td><td className="num mono">{money(row.cost_cents)}</td><td><GlassButton className="settings-button" onClick={() => openSettings(row)}>Manage</GlassButton></td></tr>)}</tbody></table></div>
          <div className="pagination"><span>Showing {((filters.page - 1) * filters.page_size + 1).toLocaleString()}–{Math.min(filters.page * filters.page_size, total).toLocaleString()} of {total.toLocaleString()}</span><div><label>Rows<select value={filters.page_size} onChange={(e) => setFilters((old) => ({ ...old, page_size: Number(e.target.value), page: 1 }))}>{PAGE_SIZES.map((size) => <option key={size}>{size}</option>)}</select></label><GlassButton disabled={filters.page <= 1} onClick={() => setFilters((old) => ({ ...old, page: old.page - 1 }))}>Previous</GlassButton><span>Page {filters.page} of {pageCount}</span><GlassButton disabled={filters.page >= pageCount} onClick={() => setFilters((old) => ({ ...old, page: old.page + 1 }))}>Next</GlassButton></div></div></>}
      </section>
    </main>

    {drawerAccount && <div className="drawer-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDrawer(); }}><aside ref={drawerRef} className="settings-drawer glass-surface" role="dialog" aria-modal="true" aria-labelledby="settings-title"><div className="drawer-top"><div><p className="eyebrow">ACCOUNT SETTINGS</p><h2 id="settings-title">{drawerAccount.name}</h2><p className="muted">Changes save as you move between steps.</p></div><GlassButton aria-label="Close settings" onClick={closeDrawer}>✕</GlassButton></div><div className="stepper" role="tablist" aria-label="Settings steps">{STEPS.map((label, index) => { const number = index + 1; return <button key={label} role="tab" aria-selected={step === number} className={`${step === number ? 'active' : ''} ${errors[number] && Object.values(errors[number]).some(Boolean) ? 'has-error' : ''}`} onClick={() => moveStep(number)}><span>{saved[String(number)] ? '✓' : number}</span><small>{label}</small></button>; })}</div>
      <form onSubmit={submitAll} noValidate><div className="step-content" key={step}>
        {step === 1 && <><label>Full name<input autoComplete="name" required value={draft[1].name} onChange={(e) => updateDraft('name', e.target.value)} onBlur={() => saveStep(1)} aria-invalid={Boolean(errors[1]?.name)} />{errors[1]?.name && <small className="field-error">{errors[1].name}</small>}</label><label>Email address<input autoComplete="email" type="email" required value={draft[1].email} onChange={(e) => updateDraft('email', e.target.value)} onBlur={() => saveStep(1)} aria-invalid={Boolean(errors[1]?.email)} />{errors[1]?.email && <small className="field-error">{errors[1].email}</small>}</label></>}
        {step === 2 && <><label>Plan<select value={draft[2].plan} onChange={(e) => updateDraft('plan', e.target.value)}><option value="basic">Basic</option><option value="pro">Pro</option><option value="enterprise">Enterprise</option></select></label><label>Billing country<select value={draft[2].billing_country} onChange={(e) => updateDraft('billing_country', e.target.value)}><option value="US">United States</option><option value="IN">India</option><option value="GB">United Kingdom</option><option value="CA">Canada</option><option value="AU">Australia</option></select>{errors[2]?.billing_country && <small className="field-error">{errors[2].billing_country}</small>}</label>{errors[2]?.billing_country && <p className="help-note">The settings service is rejecting this field after a webhook draft exists. Your selected value and other steps are preserved. Continue to the next step and submit using the previously saved billing value.</p>}</>}
        {step === 3 && <><label>Monthly minutes cap <span className="optional">Optional</span><input type="number" min="0" value={draft[3].minutes_cap} onChange={(e) => updateDraft('minutes_cap', e.target.value === '' ? '' : Number(e.target.value))} onBlur={() => saveStep(3)} /></label><label>Monthly SMS cap <span className="optional">Optional</span><input type="number" min="0" value={draft[3].sms_cap} onChange={(e) => updateDraft('sms_cap', e.target.value === '' ? '' : Number(e.target.value))} onBlur={() => saveStep(3)} /></label></>}
        {step === 4 && <><label>Webhook URL <span className="optional">Optional</span><input type="url" placeholder="https://example.com/webhooks" value={draft[4].webhook_url} onChange={(e) => updateDraft('webhook_url', e.target.value)} onBlur={() => saveStep(4)} aria-invalid={Boolean(errors[4]?.webhook_url)} />{errors[4]?.webhook_url && <small className="field-error">{errors[4].webhook_url}</small>}</label><p className="help-note">We’ll send usage events to this address when it’s configured.</p></>}
      </div>
      {formMessage && <div className={`form-message ${formMessage.type}`} role="status">{saving ? 'Saving… ' : ''}{formMessage.text}</div>}
      {errors[step]?._general && <div className="form-message error" role="alert">{errors[step]._general}</div>}
      <div className="drawer-actions">{step > 1 && <GlassButton type="button" onClick={() => setStep((old) => old - 1)}>Back</GlassButton>}{step < 4 ? <GlassButton type="button" className="primary-button" disabled={saving} onClick={() => moveStep(step + 1)}>Save & continue</GlassButton> : <button className="primary-button" type="submit" disabled={submitting || saving}>{submitting ? 'Submitting…' : 'Save and submit'}</button>}</div></form>
      <p className="autosave-note">{saving ? 'Saving your draft…' : 'Drafts save automatically when you leave a field or step.'}</p></aside></div>}
    <footer className="page-footer">Callzo usage console <span>·</span> Refreshes every 15 seconds</footer>
  </div>;
}
