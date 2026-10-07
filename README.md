# Donchian 55/20 Autonomous Cloud Demo Agent

A cloud-ready **Binance Spot Testnet-only** autonomous trading agent with a private web dashboard.

## What it does

The cloud service runs continuously. Your PC, browser, and home Wi-Fi do not need to stay on after deployment. The cloud server itself must stay online because it needs live Binance market data and Testnet order access.

The agent does not use AI discretion to invent trades. It scans independently in the cloud and executes deterministic rules with Testnet execution.

### Strategy — Donchian 55/20 (daily, long-only spot)

- **Entry** — right after `00:00 UTC`, if the just-closed daily close is strictly above the highest high of the previous **55** daily candles (signal day excluded, no lookahead), submit a market BUY within the grace window (default **15 minutes**). Signals outside the window are rejected as stale.
- **Exit** — if a position is held and the just-closed daily close is strictly below the lowest low of the previous **20** daily candles (signal day excluded), submit a market SELL. There is **no grace window** on exits: restart recovery may flatten the position whenever the condition holds.
- **Sizing** — ATR(20) by Wilder's smoothing on daily candles. Quantity = `(equity × risk%) / (2 × ATR20)`, with risk% defaulting to **1%**. Notional is additionally capped at `min(100 USDT, configured cap, available cash × 0.995)`.
- **No stop orders / no OCO.** Exits happen only through the daily 20-day-low channel exit (legacy positions opened with protective OCO orders are still reconciled by the exit monitor).
- Long-only, spot, one open position per symbol, one entry per signal (exactly-once state).

## Risk management

- Funded demo capital defaults to **$200 USDT** and is editable in the dashboard. Balance = funded capital + recorded realized P&L; equity also includes open P&L.
- Risk per trade is a percentage of equity (default **1%**), converted to quantity via `1/(2 × ATR20)`; the intended planned risk is the distance to the exit channel scaled by that ATR rule.
- Multiple positions are allowed when available demo balance can fund them.
- Daily loss cutoff: **-3R**; new trades stop for the rest of the UTC day.
- No leverage.
- Maximum position notional: **$100 USDT**, subject to available balance and exchange minimums.
- Same symbol cannot have a second open position.
- The same signal cannot be submitted twice.
- Stablecoin/fiat quote pairs are excluded: `BFUSDUSDT`, `EURIUSDT`, `EURUSDT`, `USD1USDT`, `USDEUSDT`, `XUSDUSDT`, `FDUSDUSDT`, `USDCUSDT`, `TUSDUSDT`, `USDPUSDT`, `AEURUSDT`, `RLUSDUSDT`, `UUSDT`.

## Dashboard

The dashboard shows:

- RUNNING / PAUSED status and Binance Spot Testnet readiness.
- Estimated Testnet equity, balance and free USDT.
- Open positions with entry, current price, entry channel (55d high), exit channel (20d low), distance to exit channel, ATR(20), risk and quantity.
- **Closed**, **Entry signals**, **Exit signals** and **No action** tabs with the exact stage and reason for every filtered symbol.
- Win rate, wins/losses, realized R, today's R and P&L.
- START NEW TRADES and STOP NEW TRADES controls.
- Editable strategy settings: funded capital, risk %, entry/exit periods, ATR period/multiplier, daily history length, entry grace window, scan workers.
- Named, switchable universes (default **Core 10**: BTC, ETH, BNB, SOL, XRP, ADA, AVAX, DOT, LINK, LTC) with create/activate/delete controls.
- Optional Telegram notifications.

**STOP NEW TRADES does not close existing positions.** Open positions continue to be flattened by the daily channel exit.

## Persistent data

The service stores persistent data under `DATA_DIR` (default `/data` in Docker):

- `state.json` — exactly-once signal and open-position state.
- `events.jsonl` — append-only event log.
- `signals.json` — most recent scan's signals.
- `settings.json` — dashboard-editable settings (capital, strategy, universes).
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
4. Close the browser if you want. The cloud agent scans on startup and again at every exact `00:00 UTC` boundary, and can execute qualifying demo trades within the grace window when available balance allows.

## Dashboard terms

- **Data** — symbol excluded because the daily candles are stale, short, gapped or inconsistent.
- **Entry** — held or flat symbol whose latest close did not exceed the 55-day channel.
- **Held** — already-open position with no exit condition on the latest close.
- **EXIT** — held position whose latest close fell below the 20-day channel.
- **CLOSED / OPEN** — Testnet trades the cloud agent opened and later closed through a channel exit (or legacy TP/SL reconciliation).

No PowerShell, terminal, ChatGPT session, or home PC is required for normal operation.

## Security

- This version has **no production Binance execution URL** in the execution client.
- Use **Spot Testnet API credentials only**.
- Never put API secrets into source files or the browser dashboard.
- Store credentials only in the cloud provider's encrypted environment-variable/secret settings.
- Keep the dashboard password private.
- Put the public service behind HTTPS. Most managed cloud web services provide HTTPS at their public URL.

## P&L note

Closed-trade USDT P&L is derived from the entry fill and the filled Testnet exit order. It is intended for demo validation. The displayed USDT P&L is an **estimated gross result and may not include every commission conversion**. Realized R uses actual gross P&L / planned price risk. Exit labels describe the exit reason; win/loss counts use P&L sign.

## Important demo limitations

- The strategy scans live Binance Spot market data and executes on Binance Spot Testnet. A Testnet market can differ from production; the bot rechecks the actual Testnet fill before keeping a position.
- Binance Testnet may reset balances/orders periodically.
- This cloud package was Python-compiled and smoke-tested locally, including dashboard authentication and API status. A real authenticated Testnet order cannot be verified until your own Testnet credentials are present in the deployed environment.
- Do not switch to real funds based only on a small demo sample. Review enough forward demo trades, execution errors, slippage and strategy stability first.

## Main files

- `cloud_app.py` — always-on cloud engine + dashboard server + daily scheduler.
- `demo_trader.py` — universe resolution, scanning, Testnet execution, risk manager and state.
- `strategy.py` — Donchian 55/20 evaluation, Wilder ATR, signal filtering.
- `runtime_settings.py` — dashboard-editable settings (capital, strategy, universes).
- `exit_monitor.py` — legacy OCO position reconciliation (new positions use channel exits).
- `database.py` — persistent dashboard database.
- `templates/dashboard.html` — responsive dashboard UI.
- `Dockerfile` — cloud container build.
- `docker-compose.yml` — optional self-hosted Docker deployment.
- `cloud.env.example` — environment-variable template.

## Default dashboard password

If `DASHBOARD_PASSWORD` is missing, the service uses `demo-change-me` and displays a red security warning. Do not expose a deployment publicly with that default.

## Editable capital and durable history

Use **Capital & strategy settings → Funded capital (USDT) → SAVE SETTINGS**. Any finite amount in the accepted range is enforced by the API. Changing this funding base does not reset P&L or alter open positions; for example, 500 capital and +10 Closed P&L gives a 510 balance. It does not deposit funds into Testnet. Available demo cash subtracts open entry cost; order placement also checks actual Testnet free funds. If capital is reduced below current commitments, new entries wait for available funds while exits continue.

Balance and equity use the existing recorded price-to-price P&L before fees, not a fee-inclusive exchange account statement. Closed trades are retained in SQLite without truncation; the dashboard currently displays the most recent 100 while accounting includes every saved Closed record. A restart does not add the same profit twice.

Mount a persistent Railway volume at `/data` to keep future trades, settings, capital and position state across redeployments. The Railway service refuses **new** entries if this directory is not a mount; existing exit monitoring remains active. Local development continues to use its normal data folder. Storage readiness appears in Engine status. No volume is created by the application itself.
