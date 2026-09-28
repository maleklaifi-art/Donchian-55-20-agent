# NR7 Autonomous Cloud Demo Agent

A cloud-ready **Binance Spot Testnet-only** autonomous trading agent with a private web dashboard.

## What it does

The cloud service runs continuously. Your PC, browser, and home Wi-Fi do not need to stay on after deployment. The cloud server itself must stay online because it needs live Binance market data and Testnet order access.

The agent does not use AI discretion to invent trades. It scans independently in the cloud and executes the same deterministic rules as the phone's independent local scanner, with Testnet execution added. Neither scanner depends on the other.

### Frozen entry stack

The universe matches the original phone scanner: Binance 24H-ticker symbols ending in USDT, excluding names containing UP/DOWN/BULL/BEAR, ranked by quote volume and capped by the editable Top N setting.

The cloud scanner uses 100 concurrent market-data workers and a 200-connection reuse pool by default. This changes transport time only; every symbol still downloads the same configured hourly history and runs every App and indicator rule.

A BUY is allowed only when **all** of these pass on a completed 1H candle:

**App layer**
- Prior completed UTC day NR7 box.
- Confirmed breakout: current completed 1H close > Box High and previous completed 1H close <= Box High.
- Current UTC day's accumulated quote volume / average quote volume of the previous 8 completed UTC days is **strictly > 1.5x**.
- The saved app stablecoin filter must pass.

**Indicator layer**
- Breakout candle volume / average of the prior 168 completed 1H candles is **>= 1.5x**.
- Breakout candle closes in its upper 25% (`closePosition >= 0.75`).
- LuxAlgo-compatible Strong High state is active.
- Nearest confirmed, fixed, still-unrevisited pivot high above the whole breakout candle exists as an entry filter only.
- The single TP is Classic Pivot R2 from the immediately previous completed UTC daily candle, and R2 must be strictly above the entire breakout candle.
- Stop loss is the exact active NR7 Box Low.

**R:R layer**
- `Risk = Entry - Box Low`
- `Reward = TP - Entry`
- The signal must satisfy inclusive **2 <= R:R <= 4**; exactly 2.00 and 4.00 are accepted.
- The R:R is checked again at the live entry price and again after the Testnet market fill. If either value is outside `[2, 4]`, the order is rejected or the filled demo exposure is flattened.

The confidence score follows the supplied Pine formula and uses its original `minimumRewardRisk = 1.0` scoring baseline, but eligibility and execution use the inclusive `[2, 4]` gate.

## Risk management

- Demo bankroll cap: **$200 USDT**.
- Maximum risk per trade: **$2 USDT**, sized from the actual entry-to-SL distance.
- Multiple positions and trades per day are allowed when available demo balance can fund them.
- The $2 risk cap applies separately to each position; combined open risk may exceed $2.
- Daily loss cutoff: **-3R**; new trades stop for the rest of the UTC day.
- No leverage.
- Maximum position notional: the available amount within the $200 demo cap.
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

**STOP NEW TRADES does not cancel protection on existing positions.** Existing positions continue to be monitored until their Testnet TP/SL resolves.

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

Closed-trade USDT P&L is derived from the entry fill and the filled Testnet protective exit order. It is intended for demo validation. The displayed USDT P&L is an **estimated gross result and may not include every commission conversion**. R results remain the strategy-normalized TP/SL result (`+actual R:R` on TP, `-1R` on SL).

## Important demo limitations

- The strategy scans live Binance Spot market data and executes on Binance Spot Testnet. A Testnet market can differ from production; the bot rechecks the actual Testnet fill before keeping a position.
- Binance Testnet may reset balances/orders periodically.
- This cloud package was Python-compiled and smoke-tested locally, including dashboard authentication and API status. A real authenticated Testnet order cannot be verified until your own Testnet credentials are present in the deployed environment.
- Do not switch to real funds based only on a small demo sample. Review enough forward demo trades, execution errors, slippage and strategy stability first.

## Main files

- `cloud_app.py` — always-on cloud engine + dashboard server.
- `demo_trader.py` — Testnet execution, risk manager, order protection and scanner plumbing.
- `strategy.py` — frozen App + Indicator strategy logic.
- `database.py` — persistent dashboard database.
- `templates/dashboard.html` — responsive dashboard UI.
- `Dockerfile` — cloud container build.
- `docker-compose.yml` — optional self-hosted Docker deployment.
- `cloud.env.example` — environment-variable template.
- `strategy_source.pine` — saved Pine reference used for the indicator translation.

## Default dashboard password

If `DASHBOARD_PASSWORD` is missing, the service uses `demo-change-me` and displays a red security warning. Do not expose a deployment publicly with that default.
