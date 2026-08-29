'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';

type Agent = {
  id: string; name: string; family: string; variant: number; strategy: string;
  threshold: number; horizon: number; risk_fraction: number; max_spread: number;
  min_liquidity: number; execution: string; params: Record<string, number>;
  cash: number; equity: number; return_pct: number; drawdown_pct: number;
  trades: number; positions: number; pending_orders: number; cost_exposure: number;
  allocation_status?: 'active' | 'shadow'; allocation_tier?: 'probation' | 'validated'; strategy_version?: string;
  fees_paid?: number; turnover?: number; liquidation_value?: number; realized_pnl?: number; unrealized_pnl?: number;
  alpha_trades?: number; heartbeat_trades?: number; activation_trades?: number; retirement_trades?: number;
  adaptation?: { samples: number; mean_return: number; lower_bound: number | null; upper_bound: number | null; allocation_multiplier: number; win_rate?: number; research_samples?: number; executed_samples?: number; state: 'warming' | 'research' | 'rejected' | 'reduced' | 'probation' | 'validated' | 'paused' };
  promotion?: { eligible: boolean; resolved_positions: number; days_observed: number; categories: number; edge_lcb: number | null; brier_improvement: number | null; max_event_profit_share: number | null; checks: Record<string, boolean> };
};
type Trade = { id: number; timestamp: number; agent_id: string; market_id: string; token_id: string; outcome: string; side: string; shares: number; price: number; fee: number; execution: string; reason: string; net_edge?: number | null; spread?: number | null; decision_class?: string | null; strategy_version?: string | null };
type Position = { agent_id: string; market_id: string; event_id: string; token_id: string; outcome: string; shares: number; avg_price: number };
type Order = { id: number; agent_id: string; market_id: string; event_id: string; token_id: string; outcome: string; side: string; shares: number; limit_price: number; created_at: number; reason: string };
type EquityPoint = { timestamp: number; agent_id: string; cash: number; equity: number };
type Market = { id: string; question: string; slug: string; category: string; event: string; event_id?: string; active: boolean; closed: boolean; end_date: string | null; liquidity: number; volume_24h: number };
type BookSummary = { agents: number; aggregate_equity: number; aggregate_starting_cash: number; return_pct: number; trades: number; alpha_trades?: number; heartbeat_trades?: number; activation_trades?: number; retirement_trades?: number; fees: number; turnover: number; realized_pnl: number; unrealized_pnl: number };
type Cycle = { cycle_id: string; started_at: number; finished_at: number | null; status: string; agents_evaluated: number; markets_discovered: number; markets_with_books: number; alpha_fills: number; heartbeat_fills: number; maker_fills: number; retirement_fills?: number; news_items?: number; news_confirmed_markets?: number; news_signal_overlays?: number; history_points?: number; history_ready_markets?: number; signals_generated?: number; signals_approved?: number; executable_signals?: number; counterfactuals_recorded?: number; adaptation_resolved?: number; risk_rejections?: number; risk_rejection_reasons?: Record<string, number>; strategies_paused?: number; error?: string | null };
type NewsItem = { id: string; title: string; link: string; source: string; published_at: number };
type Health = { meta: { generated_at: string; snapshot_version: number; cycle_id: string }; status: 'healthy' | 'stale' | 'degraded'; last_success_at: number | null; last_attempt_at: number | null; next_expected_at: number | null; age_seconds: number | null; cycle: Cycle | null; last_attempt: Cycle | null; error?: string };
type Snapshot = {
  meta: { generated_at: string; disclaimer: string; mode: string; starting_cash_per_agent: number; epoch?: string; epoch_label?: string; strategy_version?: string; snapshot_version?: number; cycle_id?: string };
  summary: { agents: number; trades: number; positions: number; pending_orders: number; markets_traded: number; aggregate_equity: number; aggregate_starting_cash: number; agents_with_trades: number; agents_with_positions?: number; agents_evaluated?: number; latest_cycle?: Cycle | null; decision_classes?: Record<string, number>; active_book?: BookSummary; shadow_book?: BookSummary; combined?: BookSummary; adaptation?: Record<string, number>; news?: { items: number; sources: number; latest_at: number | null } };
  agents: Agent[]; trades: Trade[]; positions: Position[]; orders: Order[]; equity: EquityPoint[]; markets: Record<string, Market>; news?: NewsItem[];
};
type Epoch = { id: string; label: string; file: string; current?: boolean; immutable?: boolean };
type View = 'overview' | 'portfolios' | 'trades' | 'positions' | 'methodology';

const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
const money0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const number = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const FAMILY_COLORS = ['#b8f34a', '#70d6a5', '#f5c95b', '#82a9ff', '#ee8d6a', '#c9a3ff', '#63cdd8', '#ffadcb', '#adb978', '#a3a7a0'];

function pct(value: number) { return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`; }
function familyLabel(value: string) { return value.replaceAll('_', ' '); }
function allocationStatus(agent: Agent) { return agent.allocation_status ?? 'active'; }
function marketLabel(snapshot: Snapshot, marketId: string) { return snapshot.markets[marketId]?.question ?? `Market ${marketId}`; }
function marketUrl(snapshot: Snapshot, marketId: string) { const slug = snapshot.markets[marketId]?.slug; return slug ? `https://polymarket.com/event/${slug}` : null; }
function formatTime(seconds: number) { return new Date(seconds * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
function formatAge(seconds: number | null | undefined) { if (seconds == null) return 'waiting for first live cycle'; if (seconds < 60) return `${Math.round(seconds)}s ago`; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`; return `${Math.floor(seconds / 3600)}h ago`; }

function downloadCsv(filename: string, rows: object[]) {
  if (!rows.length) return;
  const records = rows as Record<string, unknown>[];
  const headers = Object.keys(records[0]);
  const cell = (value: unknown) => `"${String(value ?? '').replaceAll('"', '""')}"`;
  const csv = [headers.map(cell).join(','), ...records.map((row) => headers.map((header) => cell(row[header])).join(','))].join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click(); URL.revokeObjectURL(url);
}

function ReturnValue({ value }: { value: number }) {
  return <span className={value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral'}>{pct(value)}</span>;
}

function AgentDrawer({ snapshot, agent, close, showTrades }: { snapshot: Snapshot; agent: Agent; close: () => void; showTrades: (id: string) => void }) {
  const positions = snapshot.positions.filter((row) => row.agent_id === agent.id);
  const trades = snapshot.trades.filter((row) => row.agent_id === agent.id);
  const orders = snapshot.orders.filter((row) => row.agent_id === agent.id);
  const equity = snapshot.equity.filter((row) => row.agent_id === agent.id);
  const minEquity = Math.min(...equity.map((row) => row.equity), agent.equity);
  const maxEquity = Math.max(...equity.map((row) => row.equity), agent.equity);
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={close}>
      <aside className="agent-drawer" role="dialog" aria-modal="true" aria-label={`${agent.name} portfolio`} onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div><span className="eyebrow">{agent.id} / Agent portfolio</span><h2>{agent.name}</h2><div className="badge-row"><span className="family-chip dark-chip">{familyLabel(agent.family)}</span><span className={`status-chip ${allocationStatus(agent)}`}>{allocationStatus(agent)}</span><span className="tier-chip">{agent.allocation_tier ?? 'legacy'}</span></div></div>
          <button className="icon-button" onClick={close} aria-label="Close portfolio details">×</button>
        </div>
        <div className="drawer-stats">
          <div><span>Equity</span><strong>{money.format(agent.equity)}</strong></div>
          <div><span>Return</span><strong><ReturnValue value={agent.return_pct} /></strong></div>
          <div><span>Drawdown</span><strong>{agent.drawdown_pct.toFixed(2)}%</strong></div>
          <div><span>Cash</span><strong>{money.format(agent.cash)}</strong></div>
        </div>
        <section className="drawer-section">
          <span className="eyebrow">Strategy mandate</span><p className="strategy-copy">{agent.strategy}</p>
          <div className="parameter-grid">
            <div><span>Execution</span><strong>{agent.execution}</strong></div><div><span>Signal threshold</span><strong>{(agent.threshold * 100).toFixed(2)}%</strong></div>
            <div><span>Risk / signal</span><strong>{(agent.risk_fraction * 100).toFixed(2)}%</strong></div><div><span>Max spread</span><strong>{(agent.max_spread * 100).toFixed(2)}%</strong></div>
            <div><span>Min liquidity</span><strong>{money0.format(agent.min_liquidity)}</strong></div><div><span>Horizon</span><strong>{agent.horizon} cycles</strong></div>
          </div>
        </section>
        {agent.promotion && <section className="drawer-section">
          <div className="drawer-section-heading"><span className="eyebrow">Promotion gate</span><span>{agent.promotion.eligible ? 'Eligible' : 'Collecting evidence'}</span></div>
          <div className="promotion-grid"><div><span>Resolved</span><strong>{agent.promotion.resolved_positions}/100</strong></div><div><span>Observed</span><strong>{agent.promotion.days_observed.toFixed(1)}/28d</strong></div><div><span>Categories</span><strong>{agent.promotion.categories}/3</strong></div><div><span>Checks passed</span><strong>{Object.values(agent.promotion.checks).filter(Boolean).length}/{Object.keys(agent.promotion.checks).length}</strong></div></div>
        </section>}
        {agent.adaptation && <section className="drawer-section">
          <div className="drawer-section-heading"><span className="eyebrow">Forward adaptation</span><span className={`adaptation-state ${agent.adaptation.state}`}>{agent.adaptation.state}</span></div>
          <div className="promotion-grid"><div><span>Scored entries</span><strong>{agent.adaptation.samples}</strong></div><div><span>Mean net return</span><strong>{pct(agent.adaptation.mean_return * 100)}</strong></div><div><span>Evidence bound</span><strong>{agent.adaptation.lower_bound == null ? 'Warming' : pct(agent.adaptation.lower_bound * 100)}</strong></div><div><span>Size multiplier</span><strong>{agent.adaptation.allocation_multiplier.toFixed(2)}×</strong></div></div>
        </section>}
        <section className="drawer-section">
          <div className="drawer-section-heading"><span className="eyebrow">Equity observations</span><span>{equity.length} marks</span></div>
          <div className="equity-strip" aria-label={`Equity range ${money.format(minEquity)} to ${money.format(maxEquity)}`}>
            {equity.map((point, index) => <span key={`${point.timestamp}-${index}`} style={{ height: `${22 + 68 * ((point.equity - minEquity) / Math.max(1, maxEquity - minEquity))}%` }} title={money.format(point.equity)} />)}
          </div>
          <div className="range-label"><span>{money.format(minEquity)}</span><span>{money.format(maxEquity)}</span></div>
        </section>
        <section className="drawer-section">
          <div className="drawer-section-heading"><span className="eyebrow">Open positions</span><span>{positions.length}</span></div>
          {positions.length ? <div className="compact-list">{positions.slice(0, 10).map((position) => <div key={`${position.market_id}-${position.token_id}`}><div><strong>{position.outcome} · {number.format(position.shares)} shares</strong><span>{marketLabel(snapshot, position.market_id)}</span></div><strong>{money.format(position.shares * position.avg_price)}</strong></div>)}</div> : <p className="empty-copy">No open positions in this snapshot.</p>}
        </section>
        <section className="drawer-section">
          <div className="drawer-section-heading"><span className="eyebrow">Recent trades</span><span>{trades.length}</span></div>
          {trades.length ? <div className="compact-list">{trades.slice(0, 8).map((trade) => <div key={trade.id}><div><strong>{trade.side} {trade.outcome} · {number.format(trade.shares)}</strong><span>{marketLabel(snapshot, trade.market_id)}</span></div><strong>{trade.price.toFixed(3)}</strong></div>)}</div> : <p className="empty-copy">No trade has been recorded for this agent in the current snapshot.</p>}
          {trades.length > 0 && <button className="text-button" onClick={() => showTrades(agent.id)}>Inspect all {trades.length} trades →</button>}
          {orders.length > 0 && <p className="order-note">{orders.length} resting paper maker order{orders.length === 1 ? '' : 's'} are also open.</p>}
        </section>
      </aside>
    </div>
  );
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [epochs, setEpochs] = useState<Epoch[]>([{ id: 'v2-edge-only', label: 'V2.5 · Validation-first alpha', file: '/data/snapshot.json', current: true }]);
  const [epochId, setEpochId] = useState('v2-edge-only');
  const [loadError, setLoadError] = useState(false);
  const [view, setView] = useState<View>('overview');
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [portfolioSearch, setPortfolioSearch] = useState('');
  const [portfolioFamily, setPortfolioFamily] = useState('all');
  const [portfolioSort, setPortfolioSort] = useState('equity');
  const [tradeSearch, setTradeSearch] = useState('');
  const [tradeAgent, setTradeAgent] = useState('all');
  const [tradeSide, setTradeSide] = useState('all');
  const [tradePage, setTradePage] = useState(0);
  const [positionSearch, setPositionSearch] = useState('');
  const [positionMode, setPositionMode] = useState<'positions' | 'orders'>('positions');
  const [health, setHealth] = useState<Health | null>(null);
  const [fullTradesCycle, setFullTradesCycle] = useState<string | null>(null);
  const [fullEquityCycle, setFullEquityCycle] = useState<string | null>(null);
  const [runtimeFallback, setRuntimeFallback] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => { fetch('/data/epochs/index.json').then((response) => response.json() as Promise<Epoch[]>).then((data) => { setEpochs(data); const current = data.find((epoch) => epoch.current); if (current) setEpochId(current.id); }).catch(() => undefined); }, []);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const epoch = epochs.find((item) => item.id === epochId);
      const live = epochId === 'v2-edge-only';
      try {
        const response = await fetch(live ? `/api/runtime/dashboard?t=${Date.now()}` : (epoch?.file ?? '/data/snapshot.json'), { cache: 'no-store' });
        if (!response.ok) throw new Error('runtime unavailable');
        const data = await response.json() as Snapshot;
        if (!cancelled) {
          setSnapshot(data); setLoadError(false); setRuntimeFallback(false);
          setFullTradesCycle(null); setFullEquityCycle(null);
        }
      } catch {
        if (!live) { if (!cancelled) setLoadError(true); return; }
        try {
          const fallback = await fetch('/data/snapshot.json', { cache: 'no-store' });
          if (!fallback.ok) throw new Error();
          const data = await fallback.json() as Snapshot;
          if (!cancelled) { setSnapshot(data); setLoadError(false); setRuntimeFallback(true); }
        } catch { if (!cancelled) setLoadError(true); }
      }
    };
    void load();
    const timer = epochId === 'v2-edge-only' ? window.setInterval(load, 60_000) : undefined;
    return () => { cancelled = true; if (timer) window.clearInterval(timer); };
  }, [epochId, epochs, refreshTick]);
  useEffect(() => {
    if (epochId !== 'v2-edge-only') return;
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch(`/api/runtime/health?t=${Date.now()}`, { cache: 'no-store' });
        if (!response.ok) throw new Error();
        const data = await response.json() as Health;
        if (!cancelled) setHealth(data);
      } catch { if (!cancelled) setHealth(null); }
    };
    void load(); const timer = window.setInterval(load, 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [epochId, refreshTick]);
  useEffect(() => {
    if (epochId !== 'v2-edge-only' || !snapshot || (view !== 'trades' && !selectedAgent)) return;
    const cycle = snapshot.meta.cycle_id ?? 'bootstrap';
    if (fullTradesCycle === cycle) return;
    fetch(`/api/runtime/trades?t=${Date.now()}`, { cache: 'no-store' }).then((response) => { if (!response.ok) throw new Error(); return response.json() as Promise<{ meta: Snapshot['meta']; trades: Trade[] }>; }).then((data) => { setSnapshot((current) => current && (!data.meta.cycle_id || current.meta.cycle_id === data.meta.cycle_id) ? { ...current, trades: data.trades } : current); setFullTradesCycle(data.meta.cycle_id ?? cycle); }).catch(() => undefined);
  }, [epochId, snapshot, view, selectedAgent, fullTradesCycle]);
  useEffect(() => {
    if (epochId !== 'v2-edge-only' || !snapshot || !selectedAgent) return;
    const cycle = snapshot.meta.cycle_id ?? 'bootstrap';
    if (fullEquityCycle === cycle) return;
    fetch(`/api/runtime/equity?t=${Date.now()}`, { cache: 'no-store' }).then((response) => { if (!response.ok) throw new Error(); return response.json() as Promise<{ meta: Snapshot['meta']; equity: EquityPoint[] }>; }).then((data) => { setSnapshot((current) => current && (!data.meta.cycle_id || current.meta.cycle_id === data.meta.cycle_id) ? { ...current, equity: data.equity } : current); setFullEquityCycle(data.meta.cycle_id ?? cycle); }).catch(() => undefined);
  }, [epochId, snapshot, selectedAgent, fullEquityCycle]);
  useEffect(() => { document.body.style.overflow = selectedAgent ? 'hidden' : ''; return () => { document.body.style.overflow = ''; }; }, [selectedAgent]);

  const families = useMemo(() => [...new Set(snapshot?.agents.map((agent) => agent.family) ?? [])], [snapshot]);
  const portfolios = useMemo(() => {
    if (!snapshot) return [];
    const query = portfolioSearch.toLowerCase();
    const rows = snapshot.agents.filter((agent) => (portfolioFamily === 'all' || agent.family === portfolioFamily) && (!query || `${agent.id} ${agent.name} ${agent.family}`.toLowerCase().includes(query)));
    return rows.sort((a, b) => portfolioSort === 'return' ? b.return_pct - a.return_pct : portfolioSort === 'trades' ? b.trades - a.trades : portfolioSort === 'drawdown' ? a.drawdown_pct - b.drawdown_pct : b.equity - a.equity);
  }, [snapshot, portfolioSearch, portfolioFamily, portfolioSort]);
  const filteredTrades = useMemo(() => {
    if (!snapshot) return [];
    const query = tradeSearch.toLowerCase();
    return snapshot.trades.filter((trade) => (tradeAgent === 'all' || trade.agent_id === tradeAgent) && (tradeSide === 'all' || trade.side === tradeSide) && (!query || `${trade.agent_id} ${marketLabel(snapshot, trade.market_id)} ${trade.outcome} ${trade.reason} ${trade.execution}`.toLowerCase().includes(query)));
  }, [snapshot, tradeSearch, tradeAgent, tradeSide]);
  const filteredPositions = useMemo(() => {
    if (!snapshot) return [];
    const query = positionSearch.toLowerCase(); const rows = positionMode === 'positions' ? snapshot.positions : snapshot.orders;
    return rows.filter((row) => !query || `${row.agent_id} ${marketLabel(snapshot, row.market_id)} ${row.outcome}`.toLowerCase().includes(query));
  }, [snapshot, positionSearch, positionMode]);

  if (loadError) return <main className="loading-screen">The ledger snapshot could not be loaded.</main>;
  if (!snapshot) return <main className="loading-screen">Loading PolyAlpha ledger…</main>;

  const leaders = [...snapshot.agents].sort((a, b) => b.equity - a.equity);
  const v25Fills = snapshot.trades.filter((trade) => trade.strategy_version === 'v2.5-validated-alpha');
  const validatedAgents = snapshot.agents.filter((agent) => agent.adaptation?.state === 'validated');
  const researchAgents = snapshot.agents.filter((agent) => agent.adaptation?.state === 'research');
  const positionedAgents = snapshot.agents.filter((agent) => agent.positions > 0);
  const positionedLeaders = [...positionedAgents].sort((a, b) => b.positions - a.positions || b.trades - a.trades || b.equity - a.equity);
  const fallbackBook: BookSummary = { agents: snapshot.summary.agents, aggregate_equity: snapshot.summary.aggregate_equity, aggregate_starting_cash: snapshot.summary.aggregate_starting_cash, return_pct: 100 * (snapshot.summary.aggregate_equity / snapshot.summary.aggregate_starting_cash - 1), trades: snapshot.summary.trades, fees: snapshot.trades.reduce((sum, trade) => sum + trade.fee, 0), turnover: snapshot.trades.filter((trade) => trade.side === 'BUY' || trade.side === 'SELL').reduce((sum, trade) => sum + trade.shares * trade.price, 0), realized_pnl: 0, unrealized_pnl: snapshot.summary.aggregate_equity - snapshot.summary.aggregate_starting_cash };
  const headlineBook = snapshot.summary.active_book ?? fallbackBook;
  const aggregateReturn = headlineBook.return_pct;
  const isV2 = epochId === 'v2-edge-only';
  const liveCycle = health?.cycle ?? snapshot.summary.latest_cycle ?? null;
  const topRiskReason = Object.entries(liveCycle?.risk_rejection_reasons ?? {}).sort((a, b) => b[1] - a[1])[0];
  const dataAge = health?.age_seconds ?? (runtimeFallback ? null : 0);
  const runtimeStatus = runtimeFallback ? 'stale' : health?.status ?? ((dataAge ?? Infinity) > 900 ? 'stale' : 'healthy');
  const economicStatus = runtimeStatus !== 'healthy'
    ? runtimeStatus.toUpperCase()
    : (liveCycle?.alpha_fills ?? 0) + (liveCycle?.maker_fills ?? 0) > 0
      ? 'TRADING'
      : (liveCycle?.executable_signals ?? 0) > 0
        ? 'POSITIONING'
        : (liveCycle?.counterfactuals_recorded ?? 0) + (liveCycle?.adaptation_resolved ?? 0) > 0
          ? 'LEARNING'
          : 'WATCHING';
  const familyMetrics = families.map((family, index) => {
    const agents = snapshot.agents.filter((agent) => agent.family === family); const equity = agents.reduce((sum, agent) => sum + agent.equity, 0); const trades = agents.reduce((sum, agent) => sum + agent.trades, 0);
    return { family, color: FAMILY_COLORS[index % FAMILY_COLORS.length], return_pct: 100 * (equity / (agents.length * snapshot.meta.starting_cash_per_agent) - 1), trades, active: agents.filter((agent) => agent.positions > 0).length };
  });
  const pageSize = 50; const maxTradePage = Math.max(0, Math.ceil(filteredTrades.length / pageSize) - 1); const tradeRows = filteredTrades.slice(tradePage * pageSize, (tradePage + 1) * pageSize);
  const navigate = (next: View) => { setView(next); window.scrollTo({ top: 0, behavior: 'smooth' }); };
  const showAgentTrades = (id: string) => { setSelectedAgent(null); setTradeAgent(id); setTradePage(0); navigate('trades'); };

  return (
    <main>
      <header className="site-header">
        <button className="brand" onClick={() => navigate('overview')} aria-label="PolyAlpha overview"><span className="brand-mark">Pα</span><span>POLYALPHA</span></button>
        <div className="header-meta"><span className="release-chip">Dashboard v2.5</span><span className={`status-dot ${runtimeStatus}`} /> {isV2 ? `${runtimeStatus === 'healthy' ? 'Live' : 'Stale'} paper runner` : 'Archived paper ledger'}</div>
      </header>
      <nav className="site-nav" aria-label="Dashboard sections">
        {(['overview', 'portfolios', 'trades', 'positions', 'methodology'] as View[]).map((item) => <button key={item} className={view === item ? 'active' : ''} onClick={() => navigate(item)}>{item === 'positions' ? 'Positions & orders' : item}</button>)}
        <label className="epoch-select"><span>Epoch</span><select value={epochId} onChange={(event) => setEpochId(event.target.value)}>{epochs.map((epoch) => <option key={epoch.id} value={epoch.id}>{epoch.label}{epoch.immutable ? ' · archived' : ''}</option>)}</select></label>
        <span className="snapshot-pill">Updated · {formatAge(dataAge)}</span>
      </nav>

      {view === 'overview' && <>
        <section className="hero" id="top">
          <div className="eyebrow">LIVE DEPLOYMENT · V2.5 VALIDATION-FIRST ALPHA</div>
          <h1>Every agent.<br />Every position.<br /><em>Nothing hidden.</em></h1>
          <p className="hero-copy">The objective is positive, repeatable return after spread and fees. Historical losses remain visible, but unvalidated hypotheses now receive zero new capital; only clean executable forward tests can unlock cautious paper positions.</p>
          <div className="hero-total"><span>{snapshot.summary.active_book ? 'Active alpha-book equity' : 'Aggregate paper equity'}</span><strong>{money0.format(headlineBook.aggregate_equity)}</strong><small><ReturnValue value={aggregateReturn} /> from {money0.format(headlineBook.aggregate_starting_cash)} starting paper capital</small></div>
        </section>
        {isV2 && <section className="release-proof" aria-label="Visible v2.5 deployment proof">
          <div className="release-proof-title"><span>Capital protection status</span><strong>{validatedAgents.length ? 'VALIDATED CAPITAL ONLY' : 'NO NEW RISK'}</strong><small>Profit is not guaranteed. Capital unlocks only after statistically positive, after-cost forward evidence.</small></div>
          <div><span>Validated agents</span><strong>{validatedAgents.length}</strong><small>30 clean samples + positive 95% lower bound required</small></div>
          <div><span>Researching</span><strong>{researchAgents.length}</strong><small>Still learning with zero capital allocation</small></div>
          <div><span>V2.5 capital fills</span><strong>{v25Fills.length}</strong><small>New positions after the validation gate</small></div>
          <div><span>Historical loss retained</span><strong>{money.format(headlineBook.realized_pnl)}</strong><small>Never reset or hidden</small></div>
        </section>}
        {isV2 && <section className={`runtime-banner ${runtimeStatus}`} aria-label="Autonomous runner status">
          <div className="runtime-primary"><span className="eyebrow">Self-chained five-minute runner</span><strong>{economicStatus}</strong><small>Last success {formatAge(dataAge)} · cycle {snapshot.meta.cycle_id ?? 'awaiting live state'}</small></div>
          <div><span>Agents evaluated</span><strong>{liveCycle?.agents_evaluated ?? snapshot.summary.agents_evaluated ?? 0}/100</strong></div>
          <div><span>Market books</span><strong>{liveCycle?.markets_with_books ?? '—'}</strong><small>{liveCycle?.history_ready_markets ?? '—'} history-ready · {liveCycle?.markets_discovered ?? '—'} discovered</small></div>
          <div><span>Executable signals</span><strong>{liveCycle?.executable_signals ?? '—'}</strong><small>{liveCycle?.signals_generated ?? 0} raw · {liveCycle?.risk_rejections ?? 0} rejected{topRiskReason ? ` · top: ${topRiskReason[0]}` : ''}</small></div>
          <div><span>Latest cycle</span><strong>{liveCycle?.alpha_fills ?? 0} alpha</strong><small>{liveCycle?.maker_fills ?? 0} maker · {liveCycle?.counterfactuals_recorded ?? 0} learning samples</small></div>
          <div className="runtime-actions"><button onClick={() => setRefreshTick((value) => value + 1)}>Refresh now</button><Link href="/api/runtime/ledger">Download SQLite</Link></div>
          {runtimeStatus !== 'healthy' && <p className="runtime-warning">The last successful cycle is more than 15 minutes old or the live runtime is unavailable. The last verified ledger remains visible and no partial cycle has replaced it.</p>}
        </section>}
        <section className="stat-grid" aria-label="Snapshot statistics">
          {[['Agent portfolios', snapshot.summary.agents], ['Agents positioned', snapshot.summary.agents_with_positions ?? snapshot.summary.agents_with_trades], ['Recorded trades', snapshot.summary.trades], ['Open positions', snapshot.summary.positions]].map(([label, value]) => <article className="stat-card" key={label}><span>{label}</span><strong>{Number(value).toLocaleString()}</strong></article>)}
        </section>
        {snapshot.summary.active_book && <section className="attribution-grid" aria-label="Active book attribution">
          <article><span>Fees paid</span><strong>{money.format(headlineBook.fees)}</strong><small>Explicit transaction drag</small></article>
          <article><span>Turnover</span><strong>{money0.format(headlineBook.turnover)}</strong><small>Executed buys + sells</small></article>
          <article><span>Realized P&amp;L</span><strong><ReturnValue value={100 * headlineBook.realized_pnl / headlineBook.aggregate_starting_cash} /></strong><small>{money.format(headlineBook.realized_pnl)}</small></article>
          <article><span>Unrealized P&amp;L</span><strong><ReturnValue value={100 * headlineBook.unrealized_pnl / headlineBook.aggregate_starting_cash} /></strong><small>{money.format(headlineBook.unrealized_pnl)}</small></article>
        </section>}
        {isV2 && <section className="intelligence-grid" aria-label="News and strategy adaptation">
          <article className="panel intelligence-panel"><div className="section-heading"><div><span className="eyebrow">External intelligence</span><h2>News confirmation feed</h2></div><p className="section-note">News never creates an unbounded trade. Two independent sources plus market-price or book confirmation are required.</p></div>
            <p className="learning-note">{snapshot.summary.news?.sources ?? 0} publishers · {liveCycle?.news_confirmed_markets ?? 0} confirmed markets · {liveCycle?.news_signal_overlays ?? 0} signal overlays this cycle</p>
            <div className="news-list">{snapshot.news?.length ? snapshot.news.slice(0, 6).map((item) => <a href={item.link} target="_blank" rel="noreferrer" key={item.id}><div><strong>{item.title}</strong><span>{item.source} · {formatTime(item.published_at)}</span></div><span>↗</span></a>) : <p className="empty-copy">The runner has not stored a qualifying public-feed item yet.</p>}</div>
          </article>
          <article className="panel intelligence-panel"><div className="section-heading"><div><span className="eyebrow">Online controls</span><h2>Only validated alpha gets capital</h2></div><p className="section-note">Only tight-spread, liquid, executable signals are tested at the declared horizon. Rejected and extreme ideas cannot authorize capital.</p></div>
            <p className="learning-note">{snapshot.summary.adaptation?.evaluations ?? 0} resolved · {snapshot.summary.adaptation?.pending ?? 0} pending · {liveCycle?.counterfactuals_recorded ?? 0} added this cycle</p>
            <div className="adaptation-grid">{['research', 'validated', 'rejected', 'paused'].map((state) => <div key={state}><span className={`adaptation-state ${state}`}>{state}</span><strong>{snapshot.summary.adaptation?.[state] ?? snapshot.agents.filter((agent) => agent.adaptation?.state === state).length}</strong></div>)}</div>
          </article>
        </section>}
        <section className="panel">
          <div className="section-heading"><div><span className="eyebrow">Strategy field</span><h2>Ten hypotheses under adaptive control</h2></div><p className="section-note">{isV2 ? 'All 100 agents are evaluated every cycle. Heartbeat and activation trades are disabled, crowd bias is quarantined, temporal signals wait for valid time-spaced history, and no after-cost edge means no trade.' : 'Archived forced-activation results are preserved exactly as produced for auditability.'}</p></div>
          <div className="family-grid">{familyMetrics.map((item) => <article className="family-card" key={item.family}><span className="family-index" style={{ background: item.color }} /><div><h3>{familyLabel(item.family)}</h3><span>{item.active}/10 active agents</span></div><strong><ReturnValue value={item.return_pct} /></strong><small>{item.trades.toLocaleString()} trades</small></article>)}</div>
        </section>
        <section className="panel">
          <div className="section-heading"><div><span className="eyebrow">Live portfolio activity</span><h2>{positionedAgents.length ? 'Agents with capital at work' : 'Current liquidation value'}</h2></div><button className="outline-button" onClick={() => navigate('portfolios')}>Analyze all 100 →</button></div>
          <PortfolioTable agents={(positionedAgents.length ? positionedLeaders : leaders).slice(0, 12)} select={setSelectedAgent} />
        </section>
      </>}

      {view === 'portfolios' && <section className="page-section">
        <div className="page-title"><span className="eyebrow">100 complete virtual balance sheets</span><h1>Portfolio analyzer</h1><p>Compare equity, drawdown, activity, cash, and exposure. Open any row for its strategy parameters, positions, and complete ledger.</p></div>
        <div className="toolbar"><label className="search-field"><span>Search</span><input value={portfolioSearch} onChange={(event) => setPortfolioSearch(event.target.value)} placeholder="Agent ID, name, or family" /></label><label><span>Family</span><select value={portfolioFamily} onChange={(event) => setPortfolioFamily(event.target.value)}><option value="all">All families</option>{families.map((family) => <option value={family} key={family}>{familyLabel(family)}</option>)}</select></label><label><span>Sort</span><select value={portfolioSort} onChange={(event) => setPortfolioSort(event.target.value)}><option value="equity">Highest equity</option><option value="return">Highest return</option><option value="trades">Most trades</option><option value="drawdown">Lowest drawdown</option></select></label><span className="result-count">{portfolios.length} portfolios</span></div>
        <PortfolioTable agents={portfolios} select={setSelectedAgent} expanded />
      </section>}

      {view === 'trades' && <section className="page-section">
        <div className="page-title"><span className="eyebrow">Complete execution ledger</span><h1>Trade explorer</h1><p>All paper fills, including price, size, fees, execution style, signal rationale, agent, and underlying market. {isV2 && fullTradesCycle !== snapshot.meta.cycle_id ? 'Loading the complete live ledger…' : ''}</p></div>
        <div className="toolbar"><label className="search-field"><span>Search trades</span><input value={tradeSearch} onChange={(event) => { setTradeSearch(event.target.value); setTradePage(0); }} placeholder="Market, rationale, outcome…" /></label><label><span>Agent</span><select value={tradeAgent} onChange={(event) => { setTradeAgent(event.target.value); setTradePage(0); }}><option value="all">All 100 agents</option>{snapshot.agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.id} · {agent.name}</option>)}</select></label><label><span>Side</span><select value={tradeSide} onChange={(event) => { setTradeSide(event.target.value); setTradePage(0); }}><option value="all">All sides</option>{[...new Set(snapshot.trades.map((trade) => trade.side))].map((side) => <option key={side}>{side}</option>)}</select></label><button className="outline-button export-button" onClick={() => downloadCsv('polyalpha-trades.csv', filteredTrades)}>Export CSV</button></div>
        <div className="result-bar"><span>{filteredTrades.length.toLocaleString()} matching trades</span><span>Page {Math.min(tradePage + 1, maxTradePage + 1)} of {maxTradePage + 1}</span></div>
        <div className="table-shell"><table><thead><tr><th>Time</th><th>Agent</th><th>Market</th><th>Action</th><th>Shares</th><th>Price</th><th>Fee</th><th>Net edge</th><th>Decision</th><th>Signal rationale</th></tr></thead><tbody>{tradeRows.map((trade) => <tr key={trade.id}><td>{formatTime(trade.timestamp)}</td><td><button className="table-link" onClick={() => setSelectedAgent(snapshot.agents.find((agent) => agent.id === trade.agent_id) ?? null)}>{trade.agent_id}</button></td><td className="market-cell">{marketUrl(snapshot, trade.market_id) ? <a href={marketUrl(snapshot, trade.market_id)!} target="_blank" rel="noreferrer">{marketLabel(snapshot, trade.market_id)} ↗</a> : marketLabel(snapshot, trade.market_id)}</td><td><span className={`side-chip ${trade.side.toLowerCase()}`}>{trade.side}</span> {trade.outcome}</td><td>{number.format(trade.shares)}</td><td>{trade.price.toFixed(4)}</td><td>{money.format(trade.fee)}</td><td>{trade.net_edge == null ? '—' : pct(trade.net_edge * 100)}</td><td>{trade.decision_class ?? trade.execution}</td><td className="reason-cell">{trade.reason}</td></tr>)}</tbody></table></div>
        <div className="pagination"><button disabled={tradePage === 0} onClick={() => setTradePage((page) => Math.max(0, page - 1))}>← Previous</button><button disabled={tradePage >= maxTradePage} onClick={() => setTradePage((page) => Math.min(maxTradePage, page + 1))}>Next →</button></div>
      </section>}

      {view === 'positions' && <section className="page-section">
        <div className="page-title"><span className="eyebrow">Entire open book</span><h1>Positions & resting orders</h1><p>Inspect current holdings at cost and every unfilled maker quote retained by the conservative paper execution model.</p></div>
        <div className="segmented"><button className={positionMode === 'positions' ? 'active' : ''} onClick={() => setPositionMode('positions')}>Open positions · {snapshot.positions.length}</button><button className={positionMode === 'orders' ? 'active' : ''} onClick={() => setPositionMode('orders')}>Resting orders · {snapshot.orders.length}</button></div>
        <div className="toolbar position-toolbar"><label className="search-field"><span>Search</span><input value={positionSearch} onChange={(event) => setPositionSearch(event.target.value)} placeholder="Agent, market, or outcome" /></label><button className="outline-button export-button" onClick={() => downloadCsv(`polyalpha-${positionMode}.csv`, filteredPositions as unknown as Record<string, unknown>[])}>Export CSV</button><span className="result-count">{filteredPositions.length} records</span></div>
        <div className="table-shell"><table><thead><tr><th>Agent</th><th>Market</th><th>Outcome</th>{positionMode === 'positions' ? <><th>Shares</th><th>Average price</th><th>Cost exposure</th></> : <><th>Side</th><th>Shares</th><th>Limit</th><th>Signal rationale</th></>}</tr></thead><tbody>{filteredPositions.map((row, index) => { const order = row as Order; const position = row as Position; return <tr key={`${row.agent_id}-${row.market_id}-${row.token_id}-${index}`}><td><button className="table-link" onClick={() => setSelectedAgent(snapshot.agents.find((agent) => agent.id === row.agent_id) ?? null)}>{row.agent_id}</button></td><td className="market-cell">{marketLabel(snapshot, row.market_id)}</td><td>{row.outcome}</td>{positionMode === 'positions' ? <><td>{number.format(position.shares)}</td><td>{position.avg_price.toFixed(4)}</td><td>{money.format(position.shares * position.avg_price)}</td></> : <><td><span className={`side-chip ${order.side.toLowerCase()}`}>{order.side}</span></td><td>{number.format(order.shares)}</td><td>{order.limit_price.toFixed(4)}</td><td className="reason-cell">{order.reason}</td></>}</tr>; })}</tbody></table></div>
      </section>}

      {view === 'methodology' && <section className="page-section methodology-page">
        <div className="page-title"><span className="eyebrow">How to read the experiment</span><h1>Methodology, costs & risk</h1><p>The dashboard exposes a research tournament—not a claim of proven returns. Here is exactly how the paper results were produced.</p></div>
        {isV2 ? <div className="method-grid"><article><span>01</span><h2>Time-valid features</h2><p>All 100 agents evaluate every tradable market each runner cycle. The engine backfills Polymarket’s timestamped five-minute price history, so a delayed host cannot freeze indicators or masquerade as momentum.</p></article><article><span>02</span><h2>One executable edge</h2><p>Each signal is reduced to expected executable exit value minus displayed entry price, one entry fee, one exit fee, and the modeled spread. Displayed depth caps size instead of silently eliminating every otherwise valid trade.</p></article><article><span>03</span><h2>Confirmed outside information</h2><p>Hundreds of recent articles from independent publishers are fetched every cycle. A market adjustment requires text relevance, two sources, directional agreement, and confirming price or order-book movement; the adjustment remains tightly capped.</p></article><article><span>04</span><h2>Validation-first allocation</h2><p>Only executable signals with 10¢–90¢ prices, at most 1% relative spread, sufficient depth, and positive after-cost edge enter shadow testing. Thirty current-version samples, at least 55% wins, and a positive 95% lower confidence bound are required before a 0.10% paper allocation; ten losing live samples revoke it.</p></article></div> : <div className="warning-card"><div className="warning-mark">V1</div><div><h2>Forced-activation archive</h2><p>This immutable epoch required larger discovery positions without relative-spread filtering. Its cost and crowd-bias losses remain visible for comparison with v2.</p></div></div>}
        <div className="method-section"><span className="eyebrow">The ten hypotheses</span><div className="hypothesis-list">{families.map((family, index) => { const agent = snapshot.agents.find((row) => row.family === family)!; return <div key={family}><span>{String(index + 1).padStart(2, '0')}</span><div><h3>{familyLabel(family)}</h3><p>{agent.strategy}</p></div><strong>10 variants</strong></div>; })}</div></div>
        <div className="warning-card"><div className="warning-mark">!</div><div><h2>Paper results are not investable evidence.</h2><p>This snapshot is short, unresolved, and deliberately transparent about inactive agents and early losses. Robust strategy selection requires substantially more forward data, complete market resolutions, probability-calibration scoring, and held-out evaluation.</p></div></div>
      </section>}

      <footer><div className="brand"><span className="brand-mark">Pα</span><span>POLYALPHA</span></div><p>{snapshot.meta.disclaimer}</p><span>Data snapshot {new Date(snapshot.meta.generated_at).toLocaleString()}</span></footer>
      {selectedAgent && <AgentDrawer snapshot={snapshot} agent={selectedAgent} close={() => setSelectedAgent(null)} showTrades={showAgentTrades} />}
    </main>
  );
}

function PortfolioTable({ agents, select, expanded = false }: { agents: Agent[]; select: (agent: Agent) => void; expanded?: boolean }) {
  return <div className="table-shell"><table><thead><tr><th>Rank</th><th>Agent</th><th>Book</th><th>Adaptive state</th><th>Family</th><th>Equity</th><th>Return</th><th>Drawdown</th><th>Cash</th><th>Positions</th><th>Trades</th>{expanded && <th>Explore</th>}</tr></thead><tbody>{agents.map((agent, index) => <tr key={agent.id} className="clickable-row" onClick={() => select(agent)}><td className="rank">{String(index + 1).padStart(2, '0')}</td><td><strong>{agent.id}</strong><span className="agent-name">{agent.name}</span></td><td><span className={`status-chip ${allocationStatus(agent)}`}>{agent.allocation_status ?? 'legacy'}</span></td><td><span className={`adaptation-state ${agent.adaptation?.state ?? 'warming'}`}>{agent.adaptation?.state ?? 'warming'}</span></td><td><span className="family-chip">{familyLabel(agent.family)}</span></td><td>{money.format(agent.equity)}</td><td><ReturnValue value={agent.return_pct} /></td><td>{agent.drawdown_pct.toFixed(2)}%</td><td>{money.format(agent.cash)}</td><td>{agent.positions}</td><td>{agent.trades.toLocaleString()}</td>{expanded && <td><button className="table-link" onClick={(event) => { event.stopPropagation(); select(agent); }}>Open →</button></td>}</tr>)}</tbody></table></div>;
}
