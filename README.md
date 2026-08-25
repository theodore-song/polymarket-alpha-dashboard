# PolyAlpha Dashboard

Public analytics for the PolyAlpha 100-agent Polymarket paper-trading tournament.

- Live dashboard: https://polymarket-alpha-dashboard-five.vercel.app
- GitHub repository: https://github.com/theodore-song/polymarket-alpha-dashboard
- Current snapshot: v2 with 100/100 agents trading and positioned, including 90 active-book agents and 10 crowd-bias shadow agents.
- Immutable archive: v1 forced-activation ledger with all 1,461 trades and 609 positions preserved for audit.

The dashboard exposes:

- all 100 virtual portfolios and strategy parameters;
- the complete paper-trade ledger;
- every open position and resting maker order;
- per-agent cash, equity, returns, drawdown, exposure, and activity;
- market questions and links;
- CSV exports and methodology documentation.
- epoch switching, active/shadow attribution, decision-time edge auditing, and promotion progress.

## Local development

```bash
npm install
npm run dev
```

## Refresh the ledger snapshot

```bash
python3 scripts/export_snapshot.py \
  --db /path/to/polyalpha.sqlite3 \
  --manifest /path/to/agents.json \
  --output public/data/snapshot.json
```

## Verification

```bash
npm run build
npm run build:vercel
npm run verify:data
```

Paper-trading research only. Simulated performance is not evidence of future returns.
