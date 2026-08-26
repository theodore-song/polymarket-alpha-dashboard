# Strategy and implementation report

## The honest objective

The highest-return *credible* process is to maximize repeatable net edge per unit of drawdown, not to maximize backtested headline return. A prediction-market strategy can look extraordinary by overfitting a few resolutions, ignoring spreads, assuming midpoint fills, or concentrating in correlated contracts. PolyAlpha is built to make those shortcuts difficult.

No strategy can guarantee alpha, and these agents have not yet accumulated live forward results. Their status is **research candidates under paper evaluation**, not proven world-class performers.

## Why 100 agents

The 100 agents are not cosmetic copies. They form a controlled grid across ten distinct mechanisms. Within each mechanism, ten variants trade off speed, shrinkage, order-flow weight, volatility floor, signal threshold, liquidity gate, spread tolerance, horizon, and risk. This produces enough diversity to test parameter stability without an unbounded optimizer that can data-mine noise.

The most promising mechanisms are, in descending order of structural confidence:

1. **Executable constraint violations.** A YES+NO pair pays exactly $1 at resolution. A complete set priced below $1 after taker fees has mechanically defined gross edge. It is rare and must be checked against both asks, available size, and settlement risk.
2. **Negative-risk event relative value.** Explicitly mutually-exclusive event outcomes should form a coherent probability set. The agents normalize only markets flagged as negative-risk; they do not assume every group of related questions is mutually exclusive.
3. **Microstructure.** Depth imbalance and microprice can anticipate very short-horizon moves. This edge is fragile, so orders must be cost- and spread-gated.
4. **Passive liquidity.** Resting near fair value can monetize spread and avoid taker fees. Queue uncertainty and adverse selection are the main risks. The simulator requires the next snapshot to trade/cross through the quote and credits no reward or rebate.
5. **Information-flow regimes.** Momentum, attention, volatility breakouts, and expiry-aware catalysts try to distinguish new information from temporary noise.
6. **Behavioral calibration.** Conservative shrinkage targets favorite/longshot overconfidence, but its expected edge is small and should only be retained if resolved forward data confirms category-specific calibration improvement.

## Signal construction

For every market on every cycle, the system builds a fair-probability estimate using:

- executable YES and NO bid/ask prices;
- top-five-level bid/ask depth imbalance;
- 1-, 3-, and 10-cycle returns;
- rolling mean, z-score, and volatility;
- change in reported 24-hour volume;
- time remaining to the scheduled end;
- explicitly mutually-exclusive event probability sums;
- YES+NO complete-set cost.

For a taker YES order with estimated probability `q`, ask `p`, shares `C`, and fee rate `r`, the per-share net edge is modeled as:

```text
edge_yes = q - p - r × p × (1 - p)
```

NO uses `(1 - q)` and the NO ask. A trade must clear both this estimated cost and the agent's safety threshold. Complete-set agents subtract the fee on both legs before calling a sub-$1 pair an opportunity.

## Risk and survival

Raw edge is converted to a fractional-Kelly allocation:

```text
raw_kelly = edge / max(0.01, p × (1 - p))
allocation = min(agent_limit, 2%, 0.15 × raw_kelly)
```

That allocation is further reduced linearly with drawdown. The hard controls are:

- 2% maximum equity per market;
- 8% maximum equity per event;
- 30 active markets per agent;
- agent-specific spread and liquidity gates;
- minimum economical order size;
- linear drawdown throttle from 6% to a 12% kill switch;
- no borrowing, leverage, or negative cash;
- liquidation-value marking at bids;
- official 0/1 resolution settlement.

Validated alpha signals target 0.50%–1.25% of equity and use a 0.15× Kelly multiplier; probation agents are capped at 0.50%. After 24 hours without an executed BUY or SELL, an agent may place one 0.05% heartbeat only while it retains at least 90% cash, drawdown below 6%, gross exposure below 10%, heartbeat exposure below 0.5%, and a liquid contract with no more than 3% relative spread. Heartbeats are explicitly labeled and excluded from claims of edge. Alpha entries are ranked, limited to three per cycle, and risk-adding turnover is capped at 10% of equity per cycle.

## How agents should be promoted

Do not select a winner from the offline demo; its purpose is software verification. Run the full active universe continuously and wait for enough complete resolutions. A production research committee should require all of the following before promoting an agent:

- positive net return after simulated fees/spread;
- adequate resolved trade count and category breadth;
- positive performance in multiple non-overlapping time blocks;
- acceptable maximum drawdown and tail loss;
- probability calibration improvement (Brier/log score) versus market midpoint;
- robustness across neighboring parameter variants;
- low dependence on one event, category, or extreme winner;
- realistic turnover and capacity;
- a held-out shadow period after the strategy is frozen.

The leaderboard is an observation tool, not an automatic capital allocator. Automatic promotion before sufficient resolutions would turn noise into feedback and amplify overfitting.

## What has been done in the code

- Generated and asserted exactly 100 unique, inspectable specifications.
- Traversed all active Gamma events with pagination and flattened every nested market rather than reading a fixed first page.
- Fetched both token books in batches to control latency and rate use.
- Unified YES with the complement of NO midpoint to reduce one-book staleness.
- Made all taker decisions fee-aware using the current category curve/fallback metadata.
- Restricted event normalization to Polymarket negative-risk event groups.
- Added conservative maker-order aging and fill-through rules.
- Kept a separate virtual balance sheet for each agent in a durable SQLite ledger.
- Added fractional-Kelly and concentration limits, drawdown throttling, and a kill switch.
- Replaced recurring forced activity with a reserve-gated 24-hour heartbeat while preserving strict separation between activity and alpha.
- Added edge ranking, entry/exit hysteresis, a re-entry cooldown, turnover controls, and decision-time audit fields.
- Quarantined crowd-bias agents in a separately reported shadow book until they satisfy the forward promotion gate.
- Marked at executable liquidation bids and settled from official outcomes.
- Added a deterministic exchange and automated tests so execution changes are reproducible.
- Removed any possible path to live orders; the Polymarket client is read-only.

## What would materially improve expected returns next

The largest likely improvement is not another technical indicator. It is a point-in-time fundamental data layer: polling averages and ballot access for politics; injuries, lineups, and independent sportsbook lines for sports; scheduled macro releases and nowcasts for economics; station-level forecasts for weather; and primary resolution sources for every question. Those feeds must be timestamped as observed, protected from look-ahead, normalized to the exact resolution wording, and evaluated against exchange price after latency and costs.

After that data exists, add walk-forward calibration by category, event-level covariance estimates, capacity/queue models from WebSocket data, and a frozen champion/challenger promotion service. Until then, the current system is a strong market-native paper-trading foundation—not a claim of proven alpha.
