# Verification record

Date: 2026-08-25 (America/Toronto)

## Automated checks

Command:

```bash
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Result: 5/5 tests passed.

Covered invariants:

- exactly 100 unique `(family, variant)` specifications;
- Gamma JSON-string outcome/token parsing and fee fallback;
- executable, fee-aware YES+NO complete-set edge;
- end-to-end 100-portfolio simulation with nonnegative cash;
- no live-order submission method on the public client.

## Offline endurance smoke

- 20 cycles × 30 deterministic markets × 100 agents.
- 11,970 simulated ledger trades.
- 100 agent accounts remained solvent; minimum cash was $8,883.83 and minimum liquidation equity was $9,273.47 from $10,000 starting capital.
- The synthetic leaderboard is a software test, not an investment result.

## Live public-data compatibility smoke

The CLI was run with no market cap against Polymarket's public Gamma and CLOB APIs. No credentials were supplied and the code has no live-order endpoint.

- 418 unique active/open/order-accepting binary markets discovered.
- 836 unique outcome tokens identified.
- 354 markets had usable two-sided books for both outcomes at that instant.
- Full 100-agent evaluation completed in 2.32 seconds.
- 410 conservative taker fills and 248 resting maker orders were recorded in the paper ledger.
- A following live-data cycle completed in 2.81 seconds and conservatively filled 5 prior maker orders.
- Markets without two usable books were observed but not assigned fictional fills.

Counts are point-in-time and will change as Polymarket creates, pauses, and resolves markets.
