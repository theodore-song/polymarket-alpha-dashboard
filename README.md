# PolyAlpha Dashboard

Public analytics for the PolyAlpha 100-agent Polymarket paper-trading tournament.

- Live dashboard: https://polymarket-alpha-dashboard-five.vercel.app
- GitHub repository: https://github.com/theodore-song/polymarket-alpha-dashboard
- Current runtime: v2.2 evaluates all 100 agents across the full tradable Polymarket universe every five minutes, including 90 active-book agents and 10 quarantined crowd-bias shadow agents.
- Immutable archive: v1 forced-activation ledger with all 1,461 trades and 609 positions preserved for audit.

The dashboard exposes:

- all 100 virtual portfolios and strategy parameters;
- the complete paper-trade ledger;
- every open position and resting maker order;
- per-agent cash, equity, returns, drawdown, exposure, and activity;
- market questions and links;
- CSV exports and methodology documentation.
- epoch switching, active/shadow attribution, decision-time edge auditing, and promotion progress.
- live cycle health, cache-free polling, alpha/retirement attribution, and a downloadable SQLite audit ledger;
- conservative multi-source public-news confirmation and forward, after-cost strategy adaptation.

V2.2 disables forced activation and inactivity heartbeat trades. Temporal signals
need valid time-spaced history, all entries must clear a round-trip cost hurdle,
and a strategy's allocation is reduced or paused when its forward executable
returns provide statistically negative evidence. News is fetched on each runner
cycle. The workflow also accepts an authenticated `news_alert` repository dispatch
for an immediate cycle from an outside monitor; without that optional dispatcher,
the current GitHub-hosted runner is five-minute reactive, not sub-minute.

## Local development

```bash
npm install
npm run dev
```

## Runtime architecture

The scheduled GitHub workflow restores the durable SQLite ledger from the isolated
`runtime-state` branch, executes one public-data paper cycle, verifies it, and
atomically replaces the live JSON datasets. Vercel serves those files through
cache-free read-only routes; five-minute updates do not trigger deployments.

The checked-in `public/data/snapshot.json` remains the verified fallback while
the immutable v1 archive is retained byte-for-byte.

## Export a local ledger

```bash
python3 scripts/export_snapshot.py \
  --db /path/to/polyalpha.sqlite3 \
  --manifest /path/to/agents.json \
  --output dashboard.json \
  --trades-output trades.json \
  --equity-output equity.json \
  --health-output health.json
```

## Verification

```bash
npm run build
npm run build:vercel
npm run verify:data
npm run test:engine
```

Paper-trading research only. Simulated performance is not evidence of future returns.
