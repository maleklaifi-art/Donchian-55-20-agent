# Verification — NR7/Supertrend v2 (2026-10-02)

Run from the repository root:

```bash
python -m pip install -r requirements.txt pytest==8.3.5
python -m pytest -q
```

Local result: **68 passed**. Exchange calls in the tests are simulated; no trading credentials or real orders were used.

Covered behavior:

- Supplied Pine's strict R:R lower bound, no upper cap, fixed daily R2/box-low prices.
- Hand-calculated custom Supertrend flips, carried trend and equality boundaries.
- UTC closing-boundary Sunday/Monday exclusions and strict decimal 2.56% stop boundaries.
- Separate App/Pine zero-volume bypass, safe confidence arithmetic, JSON save/reload and revision-4 migration.
- Fresh closed candles, stale/missing data rejection, original status categories and symbol eligibility checks.
- Multiple positions, $20 order cap, available capital, per-trade risk, duplicate protection, existing kill switch/daily loss controls.
- TP touch followed by reversal, stop first, native fill/cancel race, partial fills, lost sell response followed by state reload, cancel failure, cursor continuation and no fabricated fills.
- Recovery of a closed state record into an existing OPEN dashboard row; win/loss counts use gross P&L sign.
- Local HTTP health/auth/CSRF/settings integration. Zero values round-trip through `/api/config`; old upper-RR UI is absent; new ST controls connect to runtime configuration.

Also passed Python compilation, JavaScript syntax validation with `node --check`, dashboard DOM-ID checks, and `git diff --check`.

Not verified here: an authenticated Testnet execution, the specific historical DUSK trade, a Railway deployment, TradingView compilation, or bar-by-bar TradingView parity. No new backtest return is claimed. Production data and Testnet fills differ; polling is not instantaneous. Read the exact rules and limitations in `docs/NR7_AGENT_STRATEGY_HANDOFF.md`.
