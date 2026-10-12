> **Archived:** This job is no longer started or exposed by the app. Existing stored records are retained. Only demo trading and decision/outcome tracking run.

# Pattern learning — separate section, same agent

The original daily-model experiment documented here continues unchanged. The same
dashboard also hosts the multi-timeframe strategy explorer described in
[EXPLORER.md](EXPLORER.md), with separate state and a frozen forward-test protocol.

This is an additional research worker and dashboard section. It learns numerical
patterns from all eligible daily observations across the fixed 19-coin universe,
including days without a Donchian breakout. The existing Donchian execution engine,
research adoption rules, trade database and position state remain separate.

## Model and target

Version `patterns_tree_v1` is an inspectable regression tree fitted to historical
outcomes by minimizing squared error. Maximum depth is three, with at least 200
examples spanning 30 different days in each leaf. Candidate split thresholds are
training-data 20th/40th/60th/80th percentiles. These are fixed research choices, not
parameters optimized against the displayed test results. Leaf paths become the
readable learned patterns in the dashboard. Leaf average returns and positive
shares are training statistics, not calibrated forecasts of probability or proof
that a pattern works. Negative learned outcomes are retained.

Inputs use only the completed feature day and its past: 1/7/28-day returns,
20-day daily log-return volatility, candle range and close location, volume versus
its prior 20-day average, distance from the prior 20-day high, distance from a
50-day mean, and Bitcoin's 7/28-day returns. No coin identity or future data is
an input. All coins share the same feature schema and model.

A forecast made on day D uses candles through D-1. Its planned entry is the open of
D+1 (tomorrow), and its exit is the open seven days after that entry. This deliberate
one-day wait allows a model trained at any hour to issue a forecast before its
entry price exists. Training targets use the same timing and deduct configured
per-side fees and slippage. The candle cache contains completed days; the exit
open becomes usable by this system once the exit day's full candle is available.
There is no same-day backdated entry or invented forecast after service downtime.

## Chronological evaluation

Training requires at least 730 common labelled feature dates after 60 warmup days.
Each of the three latest 90-day validation folds uses all 19 coins on the same
calendar dates. Every training outcome must be available strictly before the test
fold starts, purging overlapping forward labels. Each fold has at least one prior
training year; training uses at most three years. Test rows are never used to set
that fold's tree thresholds. A mean-return predictor fitted on the same training
rows is the simple prediction baseline.

The final forecasting tree is refitted on the latest eligible three-year window,
including matured outcomes from recent market data. It retrains every 28 days,
with one frozen model between those scheduled fits. Historical diagnostic periods
overlap across monthly runs and have already been inspected: **they are not fresh
holdouts**. Training does not stop at a losing result or search repeatedly for a
profitable tree. The dashboard displays failed folds and negative portfolio results.
The fixed 19-coin historical list is subject to universe-selection/survivorship
limitations and does not establish whole-market performance.

## Paper portfolios

Every daily forecast is immutable and identified by model, leaf/pattern, feature
day, recording time, planned entry and exit. Later outcomes and errors are attached
once, including forecasts that would not trigger an entry. The prediction scorecard
includes all matured examples; overlapping examples and shared market moves mean
these are not independent trades.

The experimental portfolio considers forecasts above a fixed **1% predicted net
return** threshold, prioritizing larger predicted returns (symbol order breaks
ties). It holds at most three positions, one per symbol. Position sizing is the
captured risk percentage divided by 2 × ATR20, limited by available cash. It exits
at the predetermined seven-day open; ATR is not a stop. Entries below the captured
minimum notional are skipped. There is no leverage and no arbitrary account refill.
These model portfolio rules differ intentionally from Donchian's channel exit.

Capital, risk and cost assumptions are captured once from the current settings
when this research experiment starts; the universe is the fixed 19 coins. Later
Donchian settings edits do not rewrite this independent experiment. It uses no
real or Testnet account funds. The dashboard exposes the frozen assumptions.

Historical portfolio comparisons use predictions produced by the chronological
validation folds. Fresh forward portfolio comparisons use only forecasts actually
recorded before their planned entry open, without filling gaps after downtime.
Both show a frozen Donchian 55/20 + ADX(14)>25 benchmark on identical dates, capital,
risk percentage and costs. Strategy entry/exit rules and position limits differ.
Both start flat. Equity includes open positions marked at the latest completed
close. Paper accounting is replayed from the immutable forecasts and cached candles;
future cache corrections can change replayed paper accounting, while recorded
forecast outcomes remain immutable. There is no order book, lot-size, spread or
exchange-outage simulation. Paper results do not replace actual demo P&L.

## Isolation and persistence

`PatternEngine` receives a settings reader, a candle directory and a separate
SQLite path. It receives no execution client, promotion callback, trade-state path
or settings writer. No Testnet adoption API exists. Even a positive comparison
cannot switch the execution strategy. Existing open trades continue normally.

`DATA_DIR/patterns.sqlite3` stores JSON models, metadata, immutable forecasts,
matured outcomes, activity and the latest paper replay. Model trees are JSON rather
than executable pickle objects. The original state and trades databases are not
written by this module. The worker waits while a daily scan is in progress and
handles its own failures. Model numerical dependencies load lazily; a failed
optional module initialization is surfaced without preventing engine startup.

The dashboard refreshes this section alongside the existing sections. It shows
training history (latest 12 models), learned rules, validation periods and errors,
retrospective and fresh paper comparisons, all forecasts with pagination, a model
scorecard, paper positions/exits, and activity. All model and forecast history
remains in SQLite. `/api/patterns/forecasts` requires the existing authenticated
session. The existing CSRF-protected settings endpoint controls `pattern_enabled`.
Pausing pattern research pauses new forecasts and processing; resuming catches up
known outcomes and replays predeclared exits without inventing missed forecasts.

No new API key, AI account, scheduler service or separate deployment is required.
NumPy is the only new dependency. Training and observation wait if any required
coin lacks valid, continuous current history; the missing coins are displayed.
