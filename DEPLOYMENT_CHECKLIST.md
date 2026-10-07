# One-time Cloud Setup Checklist

Use this once when deploying the demo agent.

- [ ] Create Binance **Spot Testnet** API key and secret with Testnet trading permission.
- [ ] Create a cloud web service from this Docker project.
- [ ] Add a persistent disk/volume mounted at `/data`.
- [ ] Add `BINANCE_TESTNET_API_KEY` as a secret environment variable.
- [ ] Add `BINANCE_TESTNET_API_SECRET` as a secret environment variable.
- [ ] Set a long private `DASHBOARD_PASSWORD`.
- [ ] Set `AUTO_START_NEW_TRADES=true` for automatic Testnet execution.
- [ ] Set `DATA_DIR=/data`.
- [ ] Set `PORT=8080` if your provider does not inject its own `PORT`.
- [ ] Set `COOKIE_SECURE=true` on a public HTTPS deployment.
- [ ] Configure health check `/healthz`.
- [ ] Deploy and open the public HTTPS dashboard URL.
- [ ] Confirm **Execution: READY**.
- [ ] Confirm **New entries: ENABLED** after startup when `AUTO_START_NEW_TRADES=true`.
- [ ] Review risk settings: editable funded capital (default $200), with realized P&L added to balance, risk % per trade (default 1% of equity sized through 2×ATR), $100 maximum position notional, multiple positions funded by available balance.
- [ ] Confirm the dashboard reports Testnet **READY**; use **STOP NEW TRADES** whenever you want to pause entries.
- [ ] Leave it on demo until the forward-test sample is large enough to evaluate.

Normal daily use after this setup requires only the dashboard; no terminal command is required.


For the Donchian release, verify the strategy settings show entry 55 / exit 20 / ATR(20) × 2, the 15-minute entry grace window and **Daily at 00:00 UTC**; the old NR7/App/Supertrend knobs are gone. Named universes and capital are edited in the dashboard.

## Existing service upgrade: preserve history first

Do not deploy or attach a new volume to a running service that only has ephemeral data until its current data has been exported. An empty volume does not recover existing files.

1. Pause new entries with the existing dashboard control; keep exit monitoring running.
2. Export a consistent backup of the running `DATA_DIR`, including `state.json`, `settings.json`, `trades.sqlite3`, event/signal files, and any kill-switch file. Use SQLite's backup API for the database; copying only a live SQLite main file can omit WAL transactions. Keep the export private because settings may contain notification credentials.
3. Arrange durable storage and restore the backup before enabling the replacement service. Storage changes require the owner's authorization. Only one agent may manage the account at a time.
4. Start with new entries disabled. Confirm closed-trade keys, count and P&L match the backup, and reconcile every existing open position/protection against Testnet.
5. Confirm the effective $100 notional limit, risk % and symbol exclusions, then resume new entries.

This code change has no data-file deletion or schema replacement. It cannot preserve files that the hosting platform discards during deployment.

## Updated owner instruction — 2026-10-07

The owner no longer requires a backup of old Closed history. That backup is not an approval condition for this release. New Closed trades and capital/settings must persist from the new deployment onward.

Before cutover, obtain approval for Railway's billed persistent volume and attach it at `/data`. Preserve any still-open position state, exit-watch cursors, deduplication records and current settings, or wait until the account is flat and explicitly review the settings for the fresh deployment. Dropping old Closed history does not authorize abandoning open-position management. Do not blindly merge while storage is ephemeral.

After deployment verify storage is persistent, set the desired funded capital, and confirm 500 capital plus +10 recorded P&L displays 510 balance (do not insert fake trades into production for this check). The $100 order cap and risk-% sizing remain absolute. Verify real closed records and settings survive the next planned restart; do not create test orders solely to validate persistence.
