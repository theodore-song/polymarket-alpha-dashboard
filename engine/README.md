# PolyAlpha Lab

PolyAlpha Lab is a working, research-only system in which **100 distinct agents paper trade every currently tradable binary Polymarket market**. It discovers the entire active market set on every cycle, batches public CLOB order books, computes cost-aware signals, applies portfolio risk gates, and records 100 separate virtual portfolios in SQLite.

It cannot place a live order: the public client exposes only market discovery, market lookup, and order-book reads. No wallet, key, signer, or order-submission endpoint exists in this project.

## What is included

- Exactly 100 reproducible agents: ten alpha families × ten parameter variants.
- Five-minute restart-safe cycles with the last 128 observations persisted per market.
- No forced activation or inactivity heartbeat. An agent stays flat when no executable after-cost edge exists.
- Edge-only alpha allocations require executable edge and the full strategy threshold.
- Ninety active-book agents and ten crowd-bias shadow agents, all tracked separately in the same audit ledger.
- Full active-event traversal via Gamma pagination, flattened to every nested market; new markets join automatically.
- Batch CLOB book ingestion for both YES and NO tokens.
- Market, event-relative, order-flow, volume, volatility, expiry, and complete-set features.
- Fee-aware taker execution and conservative snapshot-based maker fills.
- Fractional-Kelly sizing plus spread, liquidity, event, market, position-count, and drawdown limits.
- Separate cash, positions, orders, trades, equity, high-water marks, and leaderboards for every agent.
- Official-resolution settlement of virtual positions.
- A deterministic offline exchange for immediate testing.
- Zero third-party runtime dependencies; Python 3.11+ and SQLite are enough.

“Every market” means every active, open, order-accepting market with a valid two-token CLOB representation. Closed, malformed, paused, and non-order-book records are deliberately not traded. Polymarket multi-outcome events are normally represented as collections of binary markets, so their constituent markets are covered.

## Quick start

```bash
cd outputs/polymarket-alpha-lab
python3 -m venv .venv
.venv/bin/pip install -e .

# Inspect all 100 agents.
.venv/bin/polyalpha agents

# Verify the whole system without network access.
.venv/bin/polyalpha demo --cycles 20 --markets 30 --db data/demo.sqlite3

# One public-data cycle. The limit is useful for a first smoke test.
.venv/bin/polyalpha once --max-markets 100 --db data/paper.sqlite3

# Omit --max-markets to cover the complete current market set every minute.
.venv/bin/polyalpha run --interval 60 --db data/paper.sqlite3

# Render the current tournament leaderboard.
.venv/bin/polyalpha report --db data/paper.sqlite3 --top 100 --output performance.md
```

No Polymarket credentials are needed because this is paper trading. Stop the continuous runner with `Ctrl-C`; the SQLite ledger is durable.

## Agent map

| IDs | Family | Edge hypothesis |
|---|---|---|
| A001–A010 | Momentum | Multi-horizon continuation confirmed by depth imbalance |
| A011–A020 | Mean reversion | Fade large moves relative to a rolling fair-value anchor |
| A021–A030 | Order flow | Use top-five-level book imbalance before midpoint response |
| A031–A040 | Volatility breakout | Require moves to clear recent noise |
| A041–A050 | Crowd bias | Apply conservative favorite/longshot calibration shrinkage |
| A051–A060 | Attention | Confirm price moves with 24-hour volume acceleration |
| A061–A070 | Time catalyst | Shift trend/reversion weights as resolution approaches |
| A071–A080 | Relative value | Normalize only explicitly negative-risk, mutually-exclusive events |
| A081–A090 | Complement arbitrage | Buy YES+NO only below $1 after both taker fees |
| A091–A100 | Liquidity maker | Place passive fair-value quotes and require a conservative fill event |

The complete machine-readable definitions—including thresholds, horizons, risk fractions, liquidity minimums, and execution modes—are in [`agents.json`](agents.json).

## Highest-return research protocol

The strategy is not to pick one clever rule in advance. It is to run a diversified, falsifiable tournament and allocate attention only after sufficient unseen data:

1. **Measure executable edge.** Compare a probability estimate to the actual ask/bid and current fee curve, never merely to a displayed midpoint.
2. **Exploit orthogonal mechanisms.** Microstructure, behavioral calibration, cross-outcome constraints, catalyst timing, and passive liquidity have different failure modes.
3. **Learn from every decision.** Executed entries and each agent's best rejected idea are scored at the declared horizon, so bad hypotheses lose allocation even when an over-strict filter prevented a fill.
4. **Use balanced alpha risk.** Probation sizing is capped at 0.5%, with 0.15× Kelly, 2% per-market and 8% per-event limits.
5. **Use liquidation accounting.** Open positions are marked at executable bids, not optimistic midpoints. Passive orders fill only after a later snapshot crosses or moves through the quote. No speculative maker rebates are credited.
6. **Promote out of sample.** Let paper data accumulate through complete market resolutions, then rank agents by return, drawdown, calibration, turnover, and stability across categories/time windows—not in-sample return alone.
7. **Retire decay.** Drawdown throttling begins at 6%; an agent at 12% drawdown stops adding risk. Strategy capital should be reduced when rolling edge or calibration deteriorates.

See [`STRATEGY.md`](STRATEGY.md) for the detailed rationale, validation standard, and known limits.

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests assert the 100-agent invariant, Gamma parsing, fee-aware complete-set logic, nonnegative virtual cash, end-to-end simulation, and the absence of a live-order method.

## Data and execution assumptions

- Open events and their complete nested market lists come from Polymarket's [Gamma keyset events API](https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination), then active/unarchived markets are retained. This avoids both the historical market catalog and finite offset windows.
- Both outcome books use the public batch [`POST /books`](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body) endpoint.
- Taker cost follows Polymarket's category-dependent formula `shares × rate × p × (1-p)` from its [fee documentation](https://docs.polymarket.com/trading/fees).
- The simulator intentionally does not credit maker rewards or rebates. Their realized value depends on competition and qualifying fills; Polymarket documents those separately under [liquidity rewards](https://docs.polymarket.com/market-makers/liquidity-rewards) and [maker rebates](https://docs.polymarket.com/market-makers/maker-rebates).
- API pacing should remain inside Polymarket's published [rate limits](https://docs.polymarket.com/api-reference/rate-limits); books are batched rather than fetched token by token.

## Important limitations

- There is no guarantee of profit or “world-class” return. More exposure increases both upside and drawdown; a credible system cannot honestly promise profit before long, resolved, out-of-sample evidence exists.
- The initial edge models use exchange-native data only. News, polls, weather, sports, macro releases, and primary-source resolution data would materially improve fundamental forecasting, but each requires licensed/reliable feeds, point-in-time storage, and latency controls.
- Public snapshots cannot reveal exact queue position. Passive fills are therefore modeled conservatively but remain estimates.
- Liquidity, fee schedules, API fields, and market rules can change. Keep the public-data smoke test and tests in deployment monitoring.
- This is research software, not financial advice. Prediction-market access may be restricted by jurisdiction; check the applicable rules before any future live-trading work.
