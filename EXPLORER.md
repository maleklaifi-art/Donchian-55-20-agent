> **Archived:** This job is no longer started or exposed by the app. Existing stored records are retained. Only demo trading and decision/outcome tracking run.

# Multi-timeframe strategy explorer

The existing agent hosts an additional paper-research worker and a subsection of
**Pattern learning · beyond Donchian**. It starts with backtests, advances eligible
candidates to newly arriving data, and records negative results. It receives no
execution client, position-state path, settings writer or adoption callback.
Current Donchian orders, open positions and the original daily model are separate.

## Scientific method and scope

The complete catalogue, order, hypotheses and acceptance policy are registered
before results. There are 363 candidates on **1H, 4H and 1D**: 360 rule candidates
(10 families × 2 variants × 6 filters × 3 frames) and three trained trees.

Discovery roots compare a predeclared family with the daily Donchian 55/20,
ADX(14)>25 reference. These are explicitly not one-factor changes to Donchian.
Every controlled improvement names its parent and changes one parameter or adds
one condition. Both parent and child are replayed on **identical calendar dates,
coins, capital, costs and sizing assumptions**. Parent results are recomputed for
that matched window, not compared with an older trial's different dates. A
controlled candidate must pass both the parent and Donchian comparison gates.

| Family | Initial rule | One-parameter variant |
|---|---|---|
| Breakout | Close above prior 20-bar high; exit below prior 10-bar low | Entry lookback 20 → 55; exit stays 10 |
| EMA trend | EMA20 crosses above EMA50; exit while fast EMA is below or equal to slow | Fast EMA20 → EMA10; slow stays 50 |
| Momentum | 20-bar return >0; exit when <=0 | Shared entry/exit lookback 20 → 50 |
| RSI reversion | RSI14 <30; exit >50 | Entry threshold 30 → 25; exit stays 50 |
| Bollinger reversion | Close below 20-bar mean minus 2 population standard deviations; exit above mean | Band width 2 → 2.5 |
| Trend pullback | Close >EMA50 and RSI14 <45; exit below EMA50 or RSI14 >65 | Entry RSI 45 → 35 |
| Volatility breakout | Add ATR20 / mean TR100 <0.75 to 20/10 breakout | Compression threshold 0.75 → 0.9 |
| Breakout + momentum | Add positive 20-bar return to 20/10 breakout | Entry channel 20 → 55; momentum and exit unchanged |
| Bollinger + RSI | Add RSI14 <30 to Bollinger rule | Band width 2 → 2.5; RSI unchanged |
| EMA + momentum | Add positive 20-bar return to EMA20/50 rule | Fast EMA20 → EMA10; momentum unchanged |

Filters are none, ADX14 >25, volume >1.5 times its prior 20-bar average,
completed higher-timeframe close >EMA50, ADX plus volume, and higher trend plus
volume. Combined filters add only the remaining condition to their named parent.
They apply to entries only. Parameters count bars of the strategy's own timeframe.
Higher context is 4H for 1H, daily for 4H, and Monday-based weekly for daily.
Only fully completed higher candles are visible at a signal's timestamp.

One parameter can legitimately affect both entry and exit (the EMA fast period or
momentum lookback). That is one declared factor, not two separately tuned factors.
Selected combinations test interactions incrementally. One-factor experiments do
not establish causation, exhaust all interactions, or guarantee no overfitting.
This catalogue is a finite initial search, not every known trading strategy.

## Data and models

The fixed universe is BTC, ETH, BNB, SOL, XRP, ADA, AVAX, DOT, LINK, LTC, ATOM, BCH,
TRX, NEAR, UNI, XLM, ALGO, VET and FIL against USDT. Binance public hourly candles
are saved page by page in `DATA_DIR/explorer.sqlite3`, starting three years before
experiment registration. No API key is needed. Downloads resume after restart,
back off on failures, stop on rate limiting or low storage, and yield between
bounded batches. Completed hours are appended without silently overwriting them.
No missing price is interpolated. A complete, contiguous common history for all
19 coins is required; dashboard readiness and per-coin errors explain any wait.
First startup requires about 500 public API pages; duration depends on connectivity.

A regression tree is fitted on each of the three frames. It uses 11 price, volume,
volatility and Bitcoin features with 1/7/28-bar returns, depth at most 3, minimum
200 examples and 30 distinct sampled dates per leaf. Input and target definitions
are fixed. Targets cover next-bar open to the open seven bars later after costs.
All training labels must be fully observable strictly before a validation fold.
To bound memory, one deterministic, rotating intraday hour is sampled per UTC
date and coin; all coins share dates. Each fold needs a prior year of mature
samples. A final model is fitted after historical diagnostics and frozen before
its forward test. Its JSON tree, readable conditions and training cutoff persist.

Explorer trees are frozen candidate experiments, not continuously refitted during
a forward comparison. The existing separate daily model still retrains every 28
days under its original protocol; see `PATTERN_LEARNING.md`. Unseen strategy code,
new parameters, additional frames or an exhausted search cannot be introduced by
silently resetting the experiment. They require a new version and explicit
validation budget. No LLM generates or executes arbitrary strategy code.

## Backtest → compare → forward → retain or reject

At most **12 candidate backtests per rolling seven days** run in a predetermined
order. Failures and interrupted attempts consume their slots and remain in the
registry. The worker performs at most one costly test or forward update per tick.
Historical diagnostics use three successive 90-day folds across all coins.
Trees fit only on preceding mature labels. Rule variants are already fixed;
these historical periods are known diagnostics, not untouched holdouts.

Historical gates require positive net return exceeding daily Donchian, no greater
maximum drawdown, at least 20 closed trades spanning five coins, positive return
in two of three folds, and positive return with fees and slippage doubled.
Controlled changes additionally need better net return and no greater drawdown
than their unchanged parent. A pass queues the candidate FIFO; it is not an edge
claim or a switch to demo execution.

At most **three forward experiments** run concurrently. Their recipes and control
are hashed and frozen; each starts on the next full UTC day after activation,
with equal flat paper accounts. Public data continues arriving hourly. Descriptive
forward metrics update once per completed day. Acceptance is assessed only at
predeclared **90/180/360-day** checkpoints. Insufficient trade count or breadth
allows an extension to the next declared checkpoint; other failures reject it.
An adequate final pass is labelled `PROMISING_PAPER_ONLY`, never auto-adopted.

At a checkpoint, paired daily return differences are resampled in circular seven-
day blocks. A lower bound must exceed zero against Donchian and, for controlled
changes, against the parent. The nominal alpha is 0.05/(363 × 3 looks × 2 controls),
including unused catalogue slots. 500,000 seeded samples provide at least ten
nominal tail observations. Return, drawdown, trade count and breadth gates also
apply. This bootstrap is a conservative operational screen with approximation,
serial-dependence and model limitations; its alpha is **not a guaranteed realized
false-discovery rate**. Neither this nor one-factor testing eliminates overfitting.
The fixed 19 surviving coins do not establish whole-market generalization.

## Paper accounting and reproducibility

Long-only, one position per coin, no leverage or stops. Each candidate has its own
cash account; three forward slots are not three shared positions. Settings capture
capital, risk percentage, per-side fee/slippage and minimum notional once. Sizing
is risk% of equity divided by 2 × ATR20 in that strategy's timeframe, limited by
cash. This is sizing, not a guaranteed loss cap. Exits are processed first, then
entries in the fixed coin order. Decisions use completed candles, filling at the
following bar's open. Trees hold seven bars. No signal before a comparison start
is filled; all comparisons start flat. Equity includes marked open positions.

Daily returns are paired on identical UTC dates. Maximum drawdown is measured at
each strategy bar close (hourly/four-hourly for intraday candidates, daily for
Donchian), which exposes additional intraday loss for candidates. No lot-size,
order-book, outages or market-impact simulation is claimed. Forward results are
**prospectively registered rule replays with hypothetical fills**, including
catch-up after downtime, not timestamped demo orders or proof of executable fills.

Trials retain the candidate, hypothesis, exact parent/change, registration time,
history boundary, historical data hash, frozen settings/catalogue hashes,
chronological metrics, double-cost result, gates, model where applicable,
checkpoints, rejection reasons and timeline. The authenticated
`/api/explorer/trial?id=...` endpoint returns detailed evidence. The dashboard uses
a compact trial registry and fetches selected evidence separately. The existing
CSRF-protected settings endpoint exposes `explorer_enabled`; pausing preserves
history and frozen recipes, and resuming catches up their paper replays.

The explorer never writes original state, trade history, strategy version or
settings, and it has no execution adoption path. The earlier Donchian research
worker's existing adoption policy remains independent and unchanged.

## Verification

`python -m pytest -q` and `node tests/check_dashboard_render.cjs` cover existing
execution/state preservation plus catalogue ancestry, one-factor signal changes,
strict and resumable candle handling, completed higher-timeframe context, future-
prefix invariance, next-open paper fills, cost deductions, purged actual tree
fits on all three frames, matched parent comparisons, fixed checkpoints, frozen
recipes, budget/restart handling, authentication, settings isolation and dashboard
escaping. Synthetic fixtures test mechanics; they are not profitable market
backtests and do not establish an edge. Production history downloads and actual
candidate outcomes are reported by the dashboard after deployment.
