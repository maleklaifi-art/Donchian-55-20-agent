# One-time Cloud Setup Checklist

Use this once when deploying the demo agent.

- [ ] Create Binance **Spot Testnet** API key and secret with Testnet trading permission.
- [ ] Create a cloud web service from this Docker project.
- [ ] Add a persistent disk/volume mounted at `/data`.
- [ ] Add `BINANCE_TESTNET_API_KEY` as a secret environment variable.
- [ ] Add `BINANCE_TESTNET_API_SECRET` as a secret environment variable.
- [ ] Set a long private `DASHBOARD_PASSWORD`.
- [ ] Set `AUTO_START_NEW_TRADES=false`.
- [ ] Set `DATA_DIR=/data`.
- [ ] Set `PORT=8080` if your provider does not inject its own `PORT`.
- [ ] Set `COOKIE_SECURE=true` on a public HTTPS deployment.
- [ ] Configure health check `/healthz`.
- [ ] Deploy and open the public HTTPS dashboard URL.
- [ ] Confirm **Execution: READY**.
- [ ] Confirm **New entries: PAUSED** after startup.
- [ ] Review risk settings: 1% per trade, 3 positions, 3% open risk, -3R/day.
- [ ] Press **START NEW TRADES**.
- [ ] Leave it on demo until the forward-test sample is large enough to evaluate.

Normal daily use after this setup requires only the dashboard; no terminal command is required.
