# NR7 Autonomous Cloud Demo Agent

A cloud-ready **Binance Spot Testnet-only** autonomous trading agent with a private web dashboard.

## What it does

The cloud service runs continuously. Your PC, browser, and home Wi-Fi do not need to stay on after deployment. The cloud server itself must stay online because it needs live Binance market data and Testnet order access.

The agent does not use AI discretion to invent trades. It scans independently in the cloud and executes deterministic rules with Testnet execution. This updated cloud strategy is independent of the unchanged mobile scanner.

### Current entry stack (v2)

**App + NR7/confirmed Supertrend Pine + exclude Sunday/Monday UTC entries + stop distance <2.56%.**

The complete specification, formulas, migration rules, execution limitations and embedded Pine are in [the AI handoff](docs/NR7_AGENT_STRATEGY_HANDOFF.md).

The App layer is retained, including daily quote-volume surge and the stablecoin filter. Its volume threshold is strict `>`; setting it to **0 disables only App volume**. The separate Pine hourly volume threshold is `>=` and also supports 0.

The supplied Pine layer requires a closed 1H NR7 breakout, strong close, confirmed Strong High and unrevisited pivot-high filter, previous-day R2 above the breakout high, and initialized green custom Supertrend (hl2, ATR10, multiplier3, Wilder by default). R:R must be **strictly >1.0** by default, with **no upper bound**. Weekdays use the exact UTC closing boundary; stop distance uses the signal close and must be strictly between 0 and 2.56%.

Saved settings remain editable. Revision 4 replaces the old inclusive RR interval once; other saved thresholds remain unchanged. The mobile branch is not updated by this change.

## Risk management

- Demo bankroll cap: **$200 USDT**.
- Maximum risk per trade: **$2 USDT**, sized from the actual entry-to-SL distance.
- Multiple positions and trades per day are allowed when available demo balance can fund them.
- The $2 risk cap applies separately to each position; combined open risk may exceed $2.
- Daily loss cutoff: **-3R**; new trades stop for the rest of the UTC day.
- No leverage.
- Maximum position notional: **$100 USDT**, subject to available balance and exchange minimums.
- Maximum entry drift from signal close: 0.30% by default.
- Same symbol cannot have a second open position.
- The same signal cannot be submitted twice.
- If protective TP/SL placement fails after a fill, the bot attempts to immediately flatten the Testnet position.

## Dashboard

The dashboard shows:

- RUNNING / PAUSED status.
- Binance Spot Testnet readiness.
- Estimated Testnet equity and free USDT.
- Open positions and current unrealized P&L.
- Entry, current price, previous-day Pivot R2 TP, exact SL, quantity, R:R and risk.
- App-volume surge and indicator-volume surge for every trade.
- Closed trades with TP/SL outcome, realized R and estimated USDT P&L.
- Win rate, wins/losses, total R, today's R and P&L.
- Filtered App failures with the exact App stage and reason.
- App-confirmed candidates with any later indicator failure reason.
- Full App + indicator Passed signals with Entry, TP, SL and diagnostics.
- START NEW TRADES and STOP NEW TRADES controls.

**STOP NEW TRADES does not cancel protection on existing positions.** Existing positions continue to be monitored for native Testnet fills and production trade-price touches of their fixed TP/SL.

## Persistent data

The service stores persistent data under `DATA_DIR` (default `/data` in Docker):

- `state.json` — exactly-once signal and open-position state.
- `events.jsonl` — append-only event log.
- `signals.json` — most recent scan's valid signals.
- `trades.sqlite3` — dashboard signals, rejections, trades and history.
- `STOP_TRADING` — kill-switch file for new entries.

Mount a persistent cloud disk at `/data` so deploys/restarts do not erase trade history.

## Cloud deployment

This package is Docker-ready. Create a cloud **web service/container** from this folder or repository, expose port `8080`, and attach persistent storage at `/data`.

Set these environment variables in the cloud provider's dashboard:

```text
BINANCE_TESTNET_API_KEY=<Spot Testnet key>
BINANCE_TESTNET_API_SECRET=<Spot Testnet secret>
DASHBOARD_PASSWORD=<long private password>
AUTO_START_NEW_TRADES=true
DATA_DIR=/data
PORT=8080
COOKIE_SECURE=true
```

`AUTO_START_NEW_TRADES=true` makes Testnet entries automatic after deployment. The dashboard still provides **STOP NEW TRADES** as a kill switch. This package has no production execution URL.

Use `/healthz` as the cloud provider health-check path.

### No daily commands

After the one-time deployment:

1. Open the cloud dashboard URL.
2. Log in with `DASHBOARD_PASSWORD`.
3. Confirm that Testnet execution says **READY**. New demo entries start automatically when `AUTO_START_NEW_TRADES=true`.
4. Close the browser if you want. The cloud agent continues scanning at every exact `HH:00 UTC` boundary and can execute multiple qualifying demo trades when available balance allows.

## Dashboard terms

- **Filtered** — coins that failed an App rule. The exact App stage and reason are retained, including Data, NR7, Breakout, Volume, or Stablecoin.
- **Confirmed** — closed 1H breakouts that passed every App rule. Indicator failures remain visible here with their exact reason.
- **Passed** — Confirmed coins that also passed every indicator rule. Entry, Pivot R2 TP, Box Low SL, R:R and all signal diagnostics are shown.
- **Closed** — Testnet trades that the cloud agent opened and later closed at TP or SL. This category exists only on the cloud agent.

No PowerShell, terminal, ChatGPT session, or home PC is required for normal operation.

## Security

- This version has **no production Binance execution URL** in the execution client.
- Use **Spot Testnet API credentials only**.
- Never put API secrets into source files or the browser dashboard.
- Store credentials only in the cloud provider's encrypted environment-variable/secret settings.
- Keep the dashboard password private.
- Put the public service behind HTTPS. Most managed cloud web services provide HTTPS at their public URL.

## P&L note

Closed-trade USDT P&L is derived from the entry fill and the filled Testnet protective exit order. It is intended for demo validation. The displayed USDT P&L is an **estimated gross result and may not include every commission conversion**. Realized R uses actual gross P&L / planned price risk. TP/SL labels describe the exit reason; win/loss counts use P&L sign.

## Take-profit monitoring

Exchange-hosted Testnet OCO protection remains active. The monitor also reads chronological production aggregate trades between polls, persists the first TP/SL touch, cancels and reconciles the Testnet bracket, then sells only the remaining quantity. This covers a production TP touch followed by a reversal between polls. A closed trade requires actual broker fills; no historical touch is booked as an assumed fill. The monitor polls every 15 seconds plus processing time, with bounded catch-up pages. Unknown sell responses retain their client ID for reconciliation, never blind resubmission. See the handoff for residual dust and ambiguous-request recovery.

## Important demo limitations

- The strategy scans live Binance Spot market data and executes on Binance Spot Testnet. A Testnet market can differ from production; the bot rechecks the actual Testnet fill before keeping a position.
- Binance Testnet may reset balances/orders periodically.
- This cloud package was Python-compiled and smoke-tested locally, including dashboard authentication and API status. A real authenticated Testnet order cannot be verified until your own Testnet credentials are present in the deployed environment.
- Do not switch to real funds based only on a small demo sample. Review enough forward demo trades, execution errors, slippage and strategy stability first.

## Main files

- `cloud_app.py` — always-on cloud engine + dashboard server.
- `demo_trader.py` — Testnet execution, risk manager, order protection and scanner plumbing.
- `strategy.py` — App + NR7/Supertrend Pine strategy and entry filters.
- `exit_monitor.py` — production-trigger monitoring and Testnet residual-exit reconciliation.
- `database.py` — persistent dashboard database.
- `templates/dashboard.html` — responsive dashboard UI.
- `Dockerfile` — cloud container build.
- `docker-compose.yml` — optional self-hosted Docker deployment.
- `cloud.env.example` — environment-variable template.
- `strategy_source.pine` — saved Pine reference used for the indicator translation.

## Default dashboard password

If `DASHBOARD_PASSWORD` is missing, the service uses `demo-change-me` and displays a red security warning. Do not expose a deployment publicly with that default.


## October 2026 execution and display update

Position quantity = min($2 / (live ask - stop), $100 / live ask), further limited by configured lower caps and available demo cash. The existing $200 total demo bankroll and exchange minimums still apply. The $2 limit is planned price-to-stop risk; fees, gaps and slippage can make realized losses larger. Existing positions keep their quantity and protective exits.

The scan and new-order gate exclude these exact pairs: `BFUSDUSDT`, `EURIUSDT`, `EURUSDT`, `USD1USDT`, `USDEUSDT`, `XUSDUSDT`, `FDUSDUSDT`, `USDCUSDT`, `TUSDUSDT`, `USDPUSDT`, `AEURUSDT`, `RLUSDUSDT`, `UUSDT`. Existing holdings and trade history are retained.

Open cards label **R:R (signal)** using the same saved signal as Passed. Actual fill prices still drive P&L and execution risk; the tooltip shows fill R:R. If the saved signal is unavailable, the card explicitly labels its fallback **R:R (fill)**. Closed records are not rewritten by restart backfill.
