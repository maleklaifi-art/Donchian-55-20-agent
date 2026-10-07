# Verification — Donchian 55/20 rewrite (2026-10-07)

Run from the repository root:

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
node tests/check_dashboard_render.cjs
```

Local result: **102 passed**, plus the offline dashboard rendering/settings/balance check. Exchange calls in the tests are simulated; no trading credentials or real orders were used.

Covered behavior:

- Donchian entry: just-closed daily close strictly above the previous 55-day high, signal day excluded, exact `00:00 UTC` daily boundary, no lookahead.
- Donchian exit: held position with daily close strictly below the previous 20-day low, channel-equal closes rejected, exits accepted without a grace window.
- Entry grace window (default 15 minutes) acceptance and rejection.
- Wilder ATR(20) hand-checked values (19/3 seed), ATR-driven sizing `qty = (equity × 1%) / (2 × ATR)` with notional caps ($100 max, available × 0.995, capital cap).
- Stale, short, gapped and open-candle data rejected before any strategy calculation; open daily candle ignored; consecutive-candle integrity.
- Signal identity `symbol:daily_close_ms`, exactly-once state, stale signals rejected before any state write or exchange call.
- Universe resolution through `exchangeInfo`: requested order preserved, non-TRADING/non-spot/wrong-quote pairs dropped, stablecoin/fiat exclusions, no ticker fallback.
- Scan snapshot split into Entry signals / Exit signals / No action with stage + reason, plus Data failures, and a flat universe emitting no exit signals.
- Multiple simultaneous positions, duplicate-symbol blocking, kill switch file and daily -3R limit blocking only new entries.
- Channel exit flattens exactly once and records `CHANNEL_EXIT` with entry and exit signal keys; realized P&L feeds balance.
- Legacy OCO positions still reconciled; new Donchian positions are left untouched by the legacy monitor.
- Closed database and signal rows survive restart plus stale open/realized backfill without modification.
- Local HTTP health/auth/CSRF/settings integration: old `nr7_session` cookie rejected, `donchian_session` accepted, CSRF required.
- Settings API: capital, risk %, entry/exit periods, ATR period/multiplier, history length, grace, scan workers round trip; invalid values rejected with 400; legacy NR7 knobs cannot be reinstated.
- Named universes round trip (create/activate/unknown-active rejected) and are applied to the engine's scan config.
- Capital change preserves strategy settings and Closed/open rows; 500 capital + 10 P&L = 510 balance; restart does not double-count; negative close replayed once; invalid capitals (0, negative, boolean, NaN, Infinity) rejected.
- Daily scheduler targets `00:00 UTC + 10s` settle delay.
- Railway entries blocked without a mounted `/data` volume; exit readiness independent of it.
- Dashboard DOM-ID invariant: every `$('id')` reference exists in the markup; NR7/Supertrend wording absent; open/closed/entry/exit/no-action rendering, settings inputs, universe controls, balance/equity and before-fee labelling.

Also passed Python compilation, JavaScript syntax validation with `node --check`, dashboard DOM-ID checks, and `git diff --check`.

Not verified here: an authenticated Testnet execution, a Railway deployment, or a forward backtest return. No profitability claim is made. Production data and Testnet fills differ; polling is not instantaneous.

Live volume provisioning and deployment remain pending owner approval; local restart tests are not a claim that Railway currently has persistent storage.
