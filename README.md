# NR7 Autonomous Cloud Demo Agent

A cloud-ready **Binance Spot Testnet-only** autonomous trading agent with a private web dashboard.

## What it does

The cloud service runs continuously. Your PC, browser, and home Wi-Fi do not need to stay on after deployment. The cloud server itself must stay online because it needs live Binance market data and Testnet order access.

The agent does not use AI discretion to invent trades. It executes the frozen rule set only.

### Frozen entry stack

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
- Nearest confirmed, fixed, still-unrevisited pivot high above the whole breakout candle exists and becomes the single TP.
- Stop loss is the exact active NR7 Box Low.

**R:R layer**
- `Risk = Entry - Box Low`
- `Reward = TP - Entry`
- The signal must satisfy **1 < R:R <= 4**.
- The R:R is checked again at the live entry price and again after the Testnet market fill. If the fill changes R:R outside `(1, 4]`, the demo exposure is flattened.

## Frozen risk management

- Risk per trade: **1%** of the configured demo bankroll.
- Maximum open positions: **3**.
- Maximum combined open risk: **3%**.
- Daily loss cutoff: **-3R**; new trades stop for the rest of the UTC day.
- No leverage.
- Maximum position notional: 33% of the demo bankroll.
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
- Entry, current price, one TP, exact SL, quantity, R:R and risk.
- App-volume surge and indicator-volume surge for every trade.
- Closed trades with TP/SL outcome, realized R and estimated USDT P&L.
- Win rate, wins/losses, total R, today's R and P&L.
- Rejected signals and the rejection reason.
- Recent valid signals.
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
AUTO_START_NEW_TRADES=false
DATA_DIR=/data
PORT=8080
COOKIE_SECURE=true
```

`AUTO_START_NEW_TRADES=false` is intentional. After every fresh deployment/restart, the service comes up safely with new entries paused. Open the dashboard and press **START NEW TRADES** after checking that Testnet execution says READY.

Use `/healthz` as the cloud provider health-check path.

### No daily commands

After the one-time deployment:

1. Open the cloud dashboard URL.
2. Log in with `DASHBOARD_PASSWORD`.
3. Press **START NEW TRADES**.
4. Close the browser if you want. The cloud agent continues working.

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
- This package was compiled and smoke-tested locally, including dashboard authentication, API status, persistence, and simulated open→close trade history. A real authenticated Testnet order cannot be verified until your own Testnet credentials are present in the deployed environment.
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
