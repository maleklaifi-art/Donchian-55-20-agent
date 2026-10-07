# NR7 agent strategy and implementation handoff

Version: `app_nr7_st_r2_box_weekday_stop256_v2`  
Prepared: 2026-10-02  
Repository: https://github.com/maleklaifi/nr7-cloud-demo-agent  
Requested target: `main`; mobile branches are outside this change.

## 1. Instruction to the next AI

Maintain the existing cloud agent and dashboard. Keep the App layer, then require the supplied NR7 + confirmed Supertrend Pine strategy, no Sunday/Monday entries at UTC candle close, and a strictly positive stop distance below 2.56%. Replace the old inclusive R:R interval: the Pine default is **R:R > 1.0, with no upper bound**. Existing positions retain their original fixed TP and SL. Supertrend red is NOT an exit in this version.

The user explicitly requested that App volume surge accept **0** and reported that DUSK touched TP but remained open until its stop. The implementation must retain both TP and SL handling, recover brief touches between polling cycles, and use actual exchange fills for recorded exits. Do not invent historical fills to repair a reported result.

This document plus the embedded Pine reference defines the intended rules. Existing code is also authoritative for operational controls explicitly retained below. Do not reintroduce old R:R 1–4 or 2–4 rules, change mobile, reset balances/history, replace authentication/Telegram/UI infrastructure, or switch execution to real funds.

## 2. Entry sequence and clocks

- Market: current tradable Binance Spot USDT symbols, ranked by 24-hour quote volume up to the user's saved Top N (default 1,000). Preserve existing symbol-name exclusions containing UP/DOWN/BULL/BEAR and the App stablecoin/low-volatility filter.
- Evaluate the immediately preceding **completed 1-hour candle**. Never enter from the developing candle or stale historical signals.
- The service scans at hourly UTC boundaries and on its existing manual/start triggers. A repeated scan cannot submit the same symbol/candle twice.
- Binance `closeTime` is inclusive, e.g. 23:59:59.999. Pine `time_close` is the exact boundary, e.g. 00:00:00. Use `closeTime + 1 ms` for the weekday filter.
- Exclude **Sunday and Monday at the entry candle's UTC closing boundary**. A Saturday 23:00–Sunday 00:00 candle is excluded. A Monday 23:00–Tuesday 00:00 candle is allowed. Displaying local time does not change these calculations.
- Reject malformed, nonfinite, duplicate, out-of-order or gapped hourly data. Zero volume settings do not disable data-quality or history requirements.
- Signal values use the breakout close. They are not recomputed from the next developing candle.

## 3. App layer retained

The user said to KEEP the App layer. This is an additional filter beyond the Pine reference.

1. Obtain the latest qualifying NR7 box from completed prior UTC days.
2. Require `close > current_box_high AND previous_close <= current_box_high` on the closed 1H candle.
3. Preserve the App volume definition in this repository: the signal UTC day's accumulated **quote volume through the signal candle**, divided by the mean quote volume of the prior eight full UTC days. It is not the Pine hourly base-volume ratio. Do not silently replace one with the other.
4. If `app_minimum_surge > 0`, require App ratio **strictly greater than** that setting. Equality fails. Default remains 1.5.
5. If `app_minimum_surge == 0`, disable only the App volume gate. An undefined volume ratio is displayed as 0 for compatibility; it is not evidence of actual positive volume.
6. Keep the App stablecoin/low-volatility filter. Its existing implementation takes hourly bars from `max(0, signal_index - 24*21)` through the signal, aggregates by UTC date (including partial edge/current days), and keeps the last 20 daily bars. With fewer than 10 daily bars it does not classify as stablecoin. Let `price` be the signal close, `range_pct = 100*(max(daily_high)-min(daily_low))/price`, and `avg_range = mean(100*(daily_high-daily_low)/daily_close)`. Reject if `range_pct < 3` OR `avg_range < 0.4`; also reject if within strictly 15% of price 1.0 or 0.1 AND `avg_range < 1`; also reject if `1500 < price < 5000` AND `avg_range < 0.5`. Do not remove this filter when disabling volume.

Status meanings remain: Filtered = App/data failure; Confirmed = App passed; Passed = all entry filters passed; Closed = an executed Testnet position was closed. Later Pine/filter failures remain visible with an explicit reason.

## 4. Pine entry layer, exact defaults

| Component | Rule |
|---|---|
| NR7 | Previous completed UTC day's high minus low is <= the minimum range of the seven completed days ending with that day. Ties qualify. |
| Persistent box | Replace box high/low only at a new day after a qualifying completed NR7 day. Keep it until another qualifying day replaces it. |
| Breakout | Closed 1H `close > boxHigh AND close[1] <= boxHigh`; use current boxHigh on both comparisons. |
| Volume | `volume / SMA(volume,168)[1] >= 1.5`. The breakout volume is excluded from the denominator. |
| Close position | `(close-low)/(high-low) >= 0.75`; flat candle gives 0. |
| Swing length | 50 bars. Use the custom confirmed swing-leg algorithm in the embedded Pine. |
| Strong High | Swing bias must be -1. A confirmed, still-unrevisited stored pivot high strictly above the complete breakout candle high must exist. |
| TP | Previous completed day's Classic Pivot R2, calculated below. |
| TP entry constraint | R2 must be strictly above the breakout candle high, not merely above its close. |
| SL | Active NR7 box low. |
| R:R | `(R2-close)/(close-boxLow) > 1.0` by default. Strict comparison. No maximum. |
| Supertrend | The supplied custom hl2 formula, ATR period 10, multiplier 3, Wilder ATR enabled. |
| ST confirmation | Must be initialized and green (+1) at the closed signal candle. A green flip on that close OR an already green trend qualifies. |
| Weekday | Exclude Sunday and Monday at exact UTC closing boundary. |
| Stop distance | `100*(signal_close-boxLow)/signal_close` must be >0 and **<2.56**, without rounding before comparison. |

Daily formulas:

```text
P = (previous_daily_high + previous_daily_low + previous_daily_close) / 3
R2 = P + previous_daily_high - previous_daily_low
risk_distance = signal_close - box_low
reward_distance = R2 - signal_close
RR = reward_distance / risk_distance
```

The confirmed pivot algorithm is stateful. On each candle, remove stored highs touched after their confirmation bar; detect a new leg using the bar 50 bars back versus the most recent 50 bars; high-leg detection takes precedence; add a pivot only when the leg changes. For `ta.crossover(close, currentSwingHigh)` the prior close compares to the **prior series value** of currentSwingHigh, not a newly assigned current level. The same applies to crossunder. The Python translation now preserves those series semantics.

Custom Supertrend uses hl2, Wilder ATR seeded with the initial true-range SMA, prior final up/down bands, and strict band crossings. Do not substitute a library Supertrend with different initialization or direction signs. The optional SMA ATR mode matches `ta.sma(ta.tr, period)`; first true range is unavailable in that mode.

Boundary examples: RR=1 fails; RR=4 and RR=10 may pass. Entry 100/stop 97.44 is exactly 2.56% and fails; entry 100/stop 97.440001 passes the stop-distance test. Other filters must still pass.

## 5. Settings and migration

- Both App and Pine surge controls accept 0 through HTML, settings API, validation, JSON persistence, reload, calculations and display. Never use `value or default` for these values.
- A nonzero App setting uses strict `>`; a nonzero Pine setting uses `>=`.
- Disabling App volume does NOT disable Pine volume. Disabling Pine volume does NOT disable App volume.
- With Pine volume disabled, its confidence contribution is 0 and there is no division by zero. Confidence remains a rule score, not a probability or entry gate.
- Preserve saved Top N, workers, history length, App/Pine volume settings, close-position setting, swing length, Telegram settings and credentials.
- Revision 4 migrates the old final R:R lower bound to the supplied Pine default 1.0 once. The old upper bound is ignored and removed from the editable UI. Subsequent saved Pine lower-bound edits persist.
- Supertrend ATR period, multiplier and Wilder/SMA choice are visible/editable. Source remains hl2. Weekday exclusions and 2.56% threshold are fixed rules in this version.
- The full supplied-default strategy requires 1.5/168/0.75/50/1.0/ATR10×3 Wilder. Preserving an existing user override can change signals; always log and inspect actual saved settings when comparing results.

## 6. Capital and operational controls

Use editable funded capital (default $200) plus recorded realized P&L, $100 maximum position notional, and $2 maximum planned price-to-stop risk per trade. Keep one open position per symbol; allow multiple symbols while available demo balance permits. Do not add a one-position-global or one-trade-per-day rule.

The execution code sizes using the live ask and minimum of risk sizing, $100 notional cap, the existing percentage cap, and available quote balance with its existing 0.5% buffer. It floors protective quantity to the exchange lot step and rounds TP/SL to the nearest exchange tick. It subtracts entry base-asset commission from the quantity available to protect. It rechecks the strict Pine R:R lower bound and absolute planned risk after the Testnet fill; invalid exposure is emergency-flattened by the existing safety path.

Retained execution controls include the 0.30% maximum upward entry drift, Testnet availability/minimum-order constraints, the -3R daily new-entry cutoff, and STOP NEW TRADES. These operational controls can reject a Pine signal. The $2 figure is planned entry-to-stop price risk, not a guaranteed maximum realized loss or a fee-inclusive promise. Gaps, costs and different Testnet fills can change losses.

Existing positions are not resized or assigned new TP/SL after deployment. No history or balance reset is part of this change.

## 7. Exit rules and the reported missed TP

**Strategy exit:** fixed R2 TP OR fixed box-low SL, whichever is hit first. Do not wait for a 1H close. Do not exit on red Supertrend. Do not trail TP with each new daily R2 or move SL with a new NR7 box. Sunday/Monday exclusions apply only to entries.

The repo scans production Binance prices and executes on Binance Spot Testnet. These are distinct markets. Previously, the monitor queried only Testnet OCO fills while the dashboard displayed production prices. That is a verified code-level mismatch; it is a plausible explanation for the user's DUSK observation, not a verified reconstruction of that specific trade.

Implemented approach:

1. Keep the existing exchange-hosted Testnet TP/SL OCO as protection. Existing Testnet orders are retained. These orders are trigger-to-market orders; Pine's emulator uses a limit TP and stop, so execution is not identical.
2. Independently inspect production aggregate trades from after entry, ordered by trade ID, with a persisted cursor. A TP touch means a production **executed trade price >= fixed TP**, not merely an ask quote reaching TP. SL uses trade price <= fixed SL.
3. Read older unprocessed trades before newer ones, so a brief TP touch followed by a reversal remains detectable. Up to four 1,000-trade pages are processed per position per poll; backlog continues next poll without skipping.
4. Persist the first observed trigger reason and price/time/ID before placing an exit. Do not replace a latched TP reason with a later stop.
5. Cancel that position's Testnet OCO, then query both legs again. Do not submit an additional sell until their cancellation/terminal status is verified.
6. Subtract quantities already filled by either leg and by any prior residual sell. Sell only the remaining permitted lot quantity, on Testnet.
7. Persist a deterministic client order ID before submitting a sell. On timeout or restart, query that ID first; never blindly repeat an uncertain sell.
8. A pending/partial order leaves the position open until reconciled. Terminal partial residual sells can be followed by a new deterministic attempt for the remaining quantity. Below-step residual dust is logged.
9. Record closure only from actual broker executed quantity and cumulative quote. TP classification is the trigger reason; the realized price may differ. Never backdate a Testnet fill to the historical production touch or fabricate a fill at R2.
10. Compute realized R from actual gross P&L / recorded planned risk. Dashboard win/loss counts use P&L sign; a TP-triggered exit can lose money. Fees are not fully converted by this legacy dashboard, so displayed P&L remains a gross estimate.

The cloud monitor runs every 15 seconds **plus processing/network time**. It is not tick-latency execution. Native Testnet fills take precedence if already completed before production-trigger handling. Testnet divergence, downtime or a backlog can produce a worse fill than a production touch. This remains a demo execution approximation, not proof of live Pine/backtest parity.

If a request's outcome cannot be established, retain its pending ID, report the error and reconcile instead of risking duplicate sales. An intent persisted just before a process dies but before POST may need operator reconciliation if the exchange has no order record. Check the deterministic ID, both OCO legs, all relevant fills and remaining balance before clearing an intent. Never clear it merely to make the dashboard look closed.

## 8. DUSK audit instructions

Obtain the actual signal key, entry time, frozen TP/SL, production trade timestamps, Testnet order-list ID and both leg IDs, order statuses/fills, and monitor errors for the reported incident. Keep user's local time (Africa/Tunis) distinct from exchange milliseconds. Determine whether the observed touch was a production executed trade, an ask quote or a Testnet trade; whether a TP order was active; and whether it filled but the dashboard failed to reconcile.

Do not claim the incident was conclusively diagnosed without this evidence. The current source fix and synthetic regression tests do not recover missing historical account logs. Already closed trades must not be rewritten as hypothetical TP winners.

## 9. Files and maintenance boundaries

| File | Responsibility |
|---|---|
| `strategy.py` | App layer, exact Pine calculations, ST confirmation and both new entry filters. |
| `runtime_settings.py` | Zero-safe settings, revision migration and public settings metadata. |
| `demo_trader.py` | Existing Testnet client and execution flow, $100 sizing, integration of exits and fill-based realized R. |
| `exit_monitor.py` | Production trade cursor, latched exit intent, cancellation/race/partial-fill reconciliation. |
| `cloud_app.py` | Pass runtime settings and market/rules into scanner/monitor; retain auth/API/Telegram behavior. |
| `database.py` | Recover a saved close even when DB row already exists; classify wins/losses by P&L. |
| `templates/dashboard.html` | Updated strategy wording, zero-capable inputs, ST settings; retain layout/tabs. |
| `strategy_source.pine` | Supplied Pine with two filters and zero-safe Pine volume scoring; no chart lines added. |

The Dockerfile starts `cloud_app.py` directly. Old `deploy_payload` and `deploy_overrides` artifacts are not part of that startup path. Do not run their legacy code-restoration scripts over this version; they contain old strategy logic.

## 10. Verification and truthful limits

Run `python -m pytest -q` with dependencies installed. Local verification for this update: **68 tests passed**, plus Python compilation, JavaScript syntax and local authenticated settings-API checks. The suite covers strict RR, weekday midnight boundaries, 2.56% boundaries, zero settings/reload/migration, hand-calculated Supertrend transitions, App/Pine gates, stale data, multiple positions/cash caps, TP-before-stop, stop-before-TP, cancellation races, partial fills, timeout/restart recovery and database close recovery. All exchange interactions in those tests are simulated.

Retain fresh-signal checks, duplicate-signal prevention, one-position-per-symbol, existing authorization/CSRF requirements, Telegram token masking, and Testnet-only endpoints. Do not print credentials. After deploying, verify the new strategy label and runtime settings and inspect real Testnet monitor logs. A GitHub commit/CI pass alone does not prove Railway deployed successfully.

TradingView compilation and bar-by-bar parity against an exported TradingView signal series were not performed here. The rolling 1,000-hour scanner history can seed stateful pivots/ATR differently from a longer TradingView chart. The preserved App filter, operational controls, universe and Testnet execution also differ from a Pine-only Colab backtest. Do not reuse earlier backtest profit claims as measured results of this updated agent. Validate matching history, settings and data before claiming exact signal or P&L parity.

## 11. Primary API/reference documentation

- Binance Spot REST API, aggregate trades, order queries, OCO and cancellation: https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- TradingView time and time zones: https://www.tradingview.com/pine-script-docs/concepts/time/
- User-supplied source: `startegy pine script.txt`. The reference below is derived from that attachment. Its original strategy header uses $10,000 and 10% equity per chart; it does **not** simulate the agent's shared $200/$100/$2 portfolio or the separate App layer.

## 12. Complete Pine reference (Pine layer only)

No visible Supertrend, box or target lines are added. BUY labels and data-window diagnostics are retained. This is the strategy reference, not a multi-symbol agent implementation.

```pine
//@version=5
strategy("NR7 + Supertrend | Weekday & Stop 2.56 [Strategy v2]", overlay=true, max_labels_count=500,
  pyramiding=0, initial_capital=10000, default_qty_type=strategy.percent_of_equity, default_qty_value=10,
  commission_type=strategy.commission.percent, commission_value=0.1, slippage=1,
  margin_long=100, margin_short=100, process_orders_on_close=true,
  calc_on_every_tick=false, calc_on_order_fills=false, use_bar_magnifier=false)
// Costs and sizing are editable in Properties: 0.10% per side, 1 tick slippage,
// 10% equity per trade by default. These are assumptions, not measured costs.
// One position per chart symbol, no pyramiding, no daily trade limit.
// Agent reference: Pine signal/exit layer ONLY; the separate App filter is in strategy.py.
// Header sizing is the original per-chart simulation, NOT the agent's $200/$100/$2 shared portfolio.
// Agent uses Testnet trigger-to-market protection; Pine uses simulated limit TP/stop.
// Minimum Volume Surge=0 disables only this Pine volume gate; no division by zero.

// Settings
volumeLookback       = input.int(168, "Volume Lookback", minval=1)
minimumSurge         = input.float(1.5, "Minimum Volume Surge", minval=0.0, step=0.1)
minimumClosePosition = input.float(0.75, "Minimum Close Position", minval=0.0, maxval=1.0, step=0.05)
minimumRewardRisk    = input.float(1.0, "Minimum Reward/Risk", minval=0.0, step=0.1)
swingLength          = input.int(50, "LuxAlgo Swing Length", minval=10)

// Binance Spot daily sessions are UTC. Other exchanges may use different daily sessions.
previousDailyHigh = request.security(syminfo.tickerid, "D", high[1], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)
previousDailyLow = request.security(syminfo.tickerid, "D", low[1], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)
previousDailyClose = request.security(syminfo.tickerid, "D", close[1], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)
previousDailyRange = request.security(syminfo.tickerid, "D", (high - low)[1], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)
lowestRangeSevenDays = request.security(syminfo.tickerid, "D", ta.lowest(high - low, 7)[1], gaps=barmerge.gaps_off, lookahead=barmerge.lookahead_on)

// The previous completed day is NR7 when its range is the smallest of seven days.
previousDayIsNR7 = not na(lowestRangeSevenDays) and previousDailyRange <= lowestRangeSevenDays
newDay = ta.change(time("D")) != 0

// Preserve the latest completed NR7 box.
var float boxHigh = na
var float boxLow = na
if newDay and previousDayIsNR7
    boxHigh := previousDailyHigh
    boxLow := previousDailyLow

// LuxAlgo-compatible confirmed swing-pivot state.
// 0 = bearish leg, 1 = bullish leg.
var int swingLeg = 0
newLegHigh = high[swingLength] > ta.highest(high, swingLength)
newLegLow = low[swingLength] < ta.lowest(low, swingLength)
previousSwingLeg = swingLeg
if newLegHigh
    swingLeg := 0
else if newLegLow
    swingLeg := 1

newPivot = swingLeg != previousSwingLeg
newPivotLow = newPivot and swingLeg == 1
newPivotHigh = newPivot and swingLeg == 0

var float currentSwingHigh = na
var float currentSwingLow = na
var bool swingHighCrossed = false
var bool swingLowCrossed = false
var int swingTrend = 0

// Fixed confirmed highs remain stored until a later candle touches them.
var array<float> confirmedHighLevels = array.new_float()
var array<int> confirmedHighBars = array.new_int()

// Remove old pivot highs touched after their confirmation bar.
if array.size(confirmedHighLevels) > 0
    for targetIndex = array.size(confirmedHighLevels) - 1 to 0
        targetLevel = array.get(confirmedHighLevels, targetIndex)
        confirmationBar = array.get(confirmedHighBars, targetIndex)
        if bar_index > confirmationBar and high >= targetLevel
            array.remove(confirmedHighLevels, targetIndex)
            array.remove(confirmedHighBars, targetIndex)

if newPivotHigh
    currentSwingHigh := high[swingLength]
    swingHighCrossed := false
    array.push(confirmedHighLevels, currentSwingHigh)
    array.push(confirmedHighBars, bar_index)

if newPivotLow
    currentSwingLow := low[swingLength]
    swingLowCrossed := false

// LuxAlgo labels its current High as Strong while swing bias is bearish.
if not na(currentSwingHigh) and ta.crossover(close, currentSwingHigh) and not swingHighCrossed
    swingHighCrossed := true
    swingTrend := 1

if not na(currentSwingLow) and ta.crossunder(close, currentSwingLow) and not swingLowCrossed
    swingLowCrossed := true
    swingTrend := -1

currentHighIsStrong = swingTrend == -1

// Keep the old nearest confirmed, still-unrevisited pivot-high requirement
// as an ENTRY FILTER. It is no longer the take-profit price.
float fixedStrongHighFilter = na
if currentHighIsStrong and array.size(confirmedHighLevels) > 0
    for targetIndex = 0 to array.size(confirmedHighLevels) - 1
        targetLevel = array.get(confirmedHighLevels, targetIndex)
        if targetLevel > high and (na(fixedStrongHighFilter) or targetLevel < fixedStrongHighFilter)
            fixedStrongHighFilter := targetLevel

// New TP: Classic Pivot R2 from the previous completed UTC daily candle.
classicPivot = (previousDailyHigh + previousDailyLow + previousDailyClose) / 3.0
pivotR2Target = classicPivot + (previousDailyHigh - previousDailyLow)

// Exclude the current breakout candle from the hourly volume baseline.
averageVolume = ta.sma(volume, volumeLookback)[1]
requiredVolume = averageVolume * minimumSurge
volumeSurge = not na(averageVolume) and averageVolume > 0 ? volume / averageVolume : 0.0

// Newly confirmed crossing above the automatic NR7 box high.
confirmedBreakout = barstate.isconfirmed and not na(boxHigh) and close > boxHigh and close[1] <= boxHigh

// Require the breakout candle to close in its upper 25% with default settings.
candleRange = high - low
closePosition = candleRange > 0 ? (close - low) / candleRange : 0.0
strongClose = closePosition >= minimumClosePosition

// Entry = confirmed breakout close, TP = previous-day Pivot R2,
// SL = NR7 box low. TP must be above the complete breakout candle.
risk = not na(boxLow) ? close - boxLow : na
reward = not na(pivotR2Target) ? pivotR2Target - close : na
rewardRisk = not na(risk) and risk > 0 and not na(reward) ? reward / risk : na
pivotR2AboveBreakout = not na(pivotR2Target) and pivotR2Target > high
positiveRewardRisk = not na(rewardRisk) and rewardRisk > minimumRewardRisk

isOneHourChart = timeframe.isminutes and timeframe.multiplier == 60

// All original entry conditions remain. Only the TP calculation changed.
validBreakout = isOneHourChart and confirmedBreakout and volumeSurge >= minimumSurge and strongClose and currentHighIsStrong and not na(fixedStrongHighFilter) and pivotR2AboveBreakout and positiveRewardRisk

// Rule-based setup confidence. This is not a probability or guarantee.
volumeStrength = minimumSurge > 0 ? math.min(math.max((volumeSurge - minimumSurge) / minimumSurge, 0.0), 1.0) : 0.0
closePositionRoom = 1.0 - minimumClosePosition
closeStrength = closePositionRoom > 0 ? math.min(math.max((closePosition - minimumClosePosition) / closePositionRoom, 0.0), 1.0) : 1.0
rewardRiskStrength = math.min(math.max((rewardRisk - minimumRewardRisk) / 2.0, 0.0), 1.0)
confidenceScore = math.round(50.0 + 20.0 * volumeStrength + 15.0 * closeStrength + 15.0 * rewardRiskStrength)
confidenceLevel = confidenceScore >= 80 ? "HIGH" : confidenceScore >= 65 ? "MEDIUM" : "LOW"
confidenceColor = confidenceScore >= 80 ? color.green : confidenceScore >= 65 ? color.orange : color.red

// Supplied Supertrend v4 formula ported directly to v5 (not ta.supertrend).
stPeriods = input.int(10, "ATR Period", minval=1, group="Supertrend")
stSource = input.source(hl2, "Source", group="Supertrend")
stMultiplier = input.float(3.0, "ATR Multiplier", minval=0.1, step=0.1, group="Supertrend")
stChangeATR = input.bool(true, "Change ATR Calculation Method? (on = Wilder ATR)", group="Supertrend")
showSetupLabels = input.bool(true, "Show BUY setup labels", group="Display")

stAtrSma = ta.sma(ta.tr, stPeriods)
stAtr = stChangeATR ? ta.atr(stPeriods) : stAtrSma
stUp = stSource - stMultiplier * stAtr
stUp1 = nz(stUp[1], stUp)
stUp := close[1] > stUp1 ? math.max(stUp, stUp1) : stUp
stDn = stSource + stMultiplier * stAtr
stDn1 = nz(stDn[1], stDn)
stDn := close[1] < stDn1 ? math.min(stDn, stDn1) : stDn
stTrend = 1
stTrend := nz(stTrend[1], stTrend)
stTrend := stTrend == -1 and close > stDn1 ? 1 : stTrend == 1 and close < stUp1 ? -1 : stTrend

// A green flip on this close OR an existing green trend both qualify.
// Require an initialized band so startup bars cannot confirm a setup.
stReady = not na(stAtr) and not na(stUp) and not na(stDn)
stBuy = barstate.isconfirmed and stReady and stTrend == 1 and stTrend[1] == -1
stSell = barstate.isconfirmed and stReady and stTrend == -1 and stTrend[1] == 1
// Entry filters use the exact closing boundary in UTC, not bar open time.
entryDay = dayofweek(time_close, "UTC")
entryDayAllowed = entryDay != dayofweek.sunday and entryDay != dayofweek.monday
stopDistancePct = close > 0 and not na(boxLow) ? 100.0 * (close - boxLow) / close : na
stopDistanceAllowed = not na(stopDistancePct) and stopDistancePct > 0 and stopDistancePct < 2.56
mergedBuy = validBreakout and barstate.isconfirmed and stReady and stTrend == 1 and entryDayAllowed and stopDistanceAllowed

// Hold the last closed-bar Supertrend display while the live bar develops.
stDisplayTrend = barstate.isconfirmed ? stTrend : stTrend[1]
stDisplayUp = barstate.isconfirmed ? stUp : stUp[1]
stDisplayDn = barstate.isconfirmed ? stDn : stDn[1]

// Audit values. Original volume readings stay available in the Data Window.
plot(volume, "Volume", display=display.data_window)
plot(requiredVolume, "Required Volume", display=display.data_window)
plot(volumeSurge, "Volume Surge", display=display.data_window)
plot(closePosition, "Close Position", display=display.data_window)
plot(fixedStrongHighFilter, "Unrevisited Pivot High / Entry Filter", display=display.data_window)
plot(currentHighIsStrong ? 1 : 0, "Strong High State", display=display.data_window)
plot(classicPivot, "Previous-Day Classic Pivot", display=display.data_window)
plot(pivotR2Target, "Current Previous-Day R2 (not necessarily active TP)", display=display.data_window)
plot(rewardRisk, "Setup R:R to R2", display=display.data_window)
plot(validBreakout ? 1 : 0, "Original NR7 BUY", display=display.data_window)
plot(stReady ? stDisplayTrend : na, "Confirmed Supertrend Direction (+1 green / -1 red)", display=display.data_window)
plot(mergedBuy ? 1 : 0, "Merged NR7 + Supertrend BUY", display=display.data_window)
plot(mergedBuy ? confidenceScore : na, "Rule Score (not win probability)", display=display.data_window)

var table chartNotice = table.new(position.top_right, 1, 1)
if barstate.islast
    table.cell(chartNotice, 0, 0, isOneHourChart ? "NR7 + ST | 1H | no Sun/Mon UTC | stop <2.56% | R2 / box-low exits" : "Use a 1-hour chart: BUY signals disabled", text_color=color.white, bgcolor=isOneHourChart ? color.new(color.green, 65) : color.red)

// Store trade-specific levels ONCE, when an entry is submitted.
var float activeTP = na
var float activeSL = na
var float signalEntry = na

if strategy.position_size == 0 and ta.change(strategy.closedtrades) > 0
    activeTP := na
    activeSL := na
    signalEntry := na

enterLong = mergedBuy and strategy.position_size == 0
if enterLong
    activeTP := pivotR2Target
    activeSL := boxLow
    signalEntry := close
    strategy.entry("NR7 Long", strategy.long, comment="NR7 + ST BUY", alert_message="NR7 + Supertrend entry filled")

// Submit the bracket together with the entry; keep its original prices.
// Stop and limit orders can fill intrabar. Supertrend NEVER closes a trade.
if enterLong or strategy.position_size > 0
    strategy.exit("R2 / Box Low", from_entry="NR7 Long", limit=activeTP, stop=activeSL, comment_profit="R2 TP", comment_loss="Box Low SL", alert_profit="Fixed R2 take-profit filled", alert_loss="Fixed NR7 box-low stop filled")

if enterLong and showSetupLabels
    signalText = "BUY: NR7 + Supertrend" +
      "\nSignal close: " + str.tostring(close, format.mintick) +
      "\nFixed TP (R2): " + str.tostring(pivotR2Target, format.mintick) +
      "\nFixed SL (box low): " + str.tostring(boxLow, format.mintick) +
      "\nSetup R:R: " + str.tostring(rewardRisk, "#.##") +
      "\nSupertrend: " + (stBuy ? "new confirmed BUY" : "confirmed uptrend") +
      "\nRule score: " + confidenceLevel + " (" + str.tostring(confidenceScore, "#") + "/100)"
    label.new(bar_index, high, signalText, xloc=xloc.bar_index, yloc=yloc.price, style=label.style_label_down, color=confidenceColor, textcolor=color.white, size=size.small)

plotshape(enterLong, title="Submitted BUY", style=shape.triangleup, location=location.belowbar, color=color.lime, size=size.small, text="BUY")
plotshape(mergedBuy and not enterLong, title="Qualifying setup while trade open (no add)", style=shape.circle, location=location.belowbar, color=color.gray, size=size.tiny)
if enterLong
    alert("Confirmed NR7 + Supertrend BUY | " + syminfo.ticker + " | Signal close: " + str.tostring(close, format.mintick) + " | Fixed R2: " + str.tostring(activeTP, format.mintick) + " | Fixed SL: " + str.tostring(activeSL, format.mintick), alert.freq_once_per_bar_close)

```

### Capital update (2026-10-07)

Runtime `starting_capital_usdt` overrides the legacy config field `demo_equity_cap_usdt`, which now represents the funding base, not an equity ceiling. Dashboard balance is funding base plus the SQLite Closed P&L aggregate. Equity adds unrealized P&L. Cloud orders use that same durable Closed aggregate and subtract open entry commitments; actual Testnet cash and the unchanged $100/$2 per-order limits still apply. Existing P&L excludes commissions; do not describe it as net after fees. Settings persist atomically; startup reloads capital without resetting strategy preferences or trade history. Railway new entries require a mounted data directory.
