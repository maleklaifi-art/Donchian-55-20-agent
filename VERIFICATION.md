# Verification — sizing, exclusions and history preservation (2026-10-07)

Run from the repository root:

```bash
python -m pip install -r requirements.txt pytest==8.3.5
python -m pytest -q
node tests/check_dashboard_render.cjs
```

Local result: **94 passed**, plus the offline dashboard rendering check. Exchange calls in the tests are simulated; no trading credentials or real orders were used.

Covered behavior:

- Supplied Pine's strict R:R lower bound, no upper cap, fixed daily R2/box-low prices.
- Hand-calculated custom Supertrend flips, carried trend and equality boundaries.
- UTC closing-boundary Sunday/Monday exclusions and strict decimal 2.56% stop boundaries.
- Separate App/Pine zero-volume bypass, safe confidence arithmetic, JSON save/reload and revision-4 migration.
- Fresh closed candles, stale/missing data rejection, original status categories and symbol eligibility checks.
- Risk-based sizing, absolute $100 order cap even with a larger configured cap, $2 planned risk cap, available capital, multiple positions, duplicate protection, and existing kill switch/daily loss controls.
- All 13 user-specified pairs excluded before top-N ranking and blocked at new-order submission; rejected excluded signals leave existing state unchanged.
- Signal R:R matches Passed while preserving fill entry/risk; legacy records without a signal use a clearly labelled fill-R:R fallback.
- Closed database and signal rows survive restart plus stale open/realized backfill without modification. Existing recovery of a missed close remains covered.
- TP touch followed by reversal, stop first, native fill/cancel race, partial fills, lost sell response followed by state reload, cancel failure, cursor continuation and no fabricated fills.
- Recovery of a closed state record into an existing OPEN dashboard row; win/loss counts use gross P&L sign.
- Local HTTP health/auth/CSRF/settings integration. Zero values round-trip through `/api/config`; old upper-RR UI is absent; new ST controls connect to runtime configuration.

Also passed Python compilation, JavaScript syntax validation with `node --check`, dashboard DOM-ID checks, and `git diff --check`.

Not verified here: an authenticated Testnet execution, the specific historical DUSK trade, a Railway deployment, TradingView compilation, or bar-by-bar TradingView parity. No new backtest return is claimed. Production data and Testnet fills differ; polling is not instantaneous. Read the exact rules and limitations in `docs/NR7_AGENT_STRATEGY_HANDOFF.md`.

Capital/accounting extension: settings API saves and reloads editable capital, rejects zero/negative/non-finite values, preserves strategy settings and trade rows, and updates balance/equity immediately. Tests cover positive/negative P&L funding subsequent orders, reduced capital retaining current positions, database-ledger sizing, restart without double-counting, and blocking Railway entries without a data mount. Dashboard rendering checks verify the capital input, balance, equity and before-fee labelling.

Live volume provisioning and deployment remain pending owner approval; local restart tests are not a claim that Railway currently has persistent storage.
