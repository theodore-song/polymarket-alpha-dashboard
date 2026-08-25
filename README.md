# PolyAlpha Dashboard

Public analytics for the PolyAlpha 100-agent Polymarket paper-trading tournament.

- Live dashboard: https://polymarket-alpha-dashboard-five.vercel.app
- GitHub repository: https://github.com/theodore-song/polymarket-alpha-dashboard
- Published snapshot: all 100 agents active, 1,461 trades, 609 open positions, 333 resting orders, and 127 engaged markets.

The dashboard exposes:

- all 100 virtual portfolios and strategy parameters;
- the complete paper-trade ledger;
- every open position and resting maker order;
- per-agent cash, equity, returns, drawdown, exposure, and activity;
- market questions and links;
- CSV exports and methodology documentation.

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
```

Paper-trading research only. Simulated performance is not evidence of future returns.
