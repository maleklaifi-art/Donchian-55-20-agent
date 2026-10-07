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
- [ ] Review risk settings: $200 bankroll cap, $100 maximum position notional, $2 maximum planned risk per trade, multiple positions funded by available balance.
- [ ] Confirm the dashboard reports Testnet **READY**; use **STOP NEW TRADES** whenever you want to pause entries.
- [ ] Leave it on demo until the forward-test sample is large enough to evaluate.

Normal daily use after this setup requires only the dashboard; no terminal command is required.


For v2, verify the label says NR7/Supertrend, no Sun/Mon UTC and stop <2.56%; App volume accepts 0; R:R has no upper cap. See `docs/NR7_AGENT_STRATEGY_HANDOFF.md` for the exact migration and exit behavior.

## Existing service upgrade: preserve history first

Do not deploy or attach a new volume to a running service that only has ephemeral data until its current data has been exported. An empty volume does not recover existing files.

1. Pause new entries with the existing dashboard control; keep exit monitoring running.
2. Export a consistent backup of the running `DATA_DIR`, including `state.json`, `settings.json`, `trades.sqlite3`, event/signal files, and any kill-switch file. Use SQLite's backup API for the database; copying only a live SQLite main file can omit WAL transactions. Keep the export private because settings may contain notification credentials.
3. Arrange durable storage and restore the backup before enabling the replacement service. Storage changes require the owner's authorization. Only one agent may manage the account at a time.
4. Start with new entries disabled. Confirm closed-trade keys, count and P&L match the backup, and reconcile every existing open position/protection against Testnet.
5. Confirm effective $100/$2 limits, signal R:R and symbol exclusions, then resume new entries.

This code change has no data-file deletion or schema replacement. It cannot preserve files that the hosting platform discards during deployment.
