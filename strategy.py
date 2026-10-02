from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float = 0.0


@dataclass
class StrategyConfig:
    app_minimum_surge: float = 1.5          # strict > 1.5
    indicator_volume_lookback: int = 168
    indicator_minimum_surge: float = 1.5    # >= 1.5
    minimum_close_position: float = 0.75
    pine_minimum_reward_risk: float = 1.0   # Pine confidence-score baseline
    minimum_reward_risk: float = 1.0        # legacy compatibility; not an entry gate
    maximum_reward_risk: float = 4.0        # legacy compatibility; ignored
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    supertrend_wilder: bool = True
    swing_length: int = 50


@dataclass
class Signal:
    symbol: str
    signal_close_ms: int
    close_time_utc: str
    entry: float
    tp: float
    sl: float
    classic_pivot: float
    strong_high_filter: float
    rr: float
    box_high: float
    box_low: float
    app_volume_surge: float
    indicator_volume_surge: float
    close_position: float
    confidence_score: int
    confidence_level: str
    stop_distance_pct: float = 0.0
    supertrend_direction: int = 1
    strategy_version: str = "app_nr7_st_r2_box_weekday_stop256_v2"

    def to_dict(self):
        return asdict(self)


@dataclass
class PhaseOneSignal:
    symbol: str
    signal_close_ms: int
    close_time_utc: str
    entry: float
    previous_close: float
    box_high: float
    box_low: float
    app_volume_surge: float

    def to_dict(self):
        return asdict(self)


@dataclass
class Evaluation:
    symbol: str
    price: float
    phase_one: Optional[PhaseOneSignal] = None
    signal: Optional[Signal] = None
    rejection_stage: Optional[str] = None
    rejection_reason: Optional[str] = None
    indicator_volume_surge: Optional[float] = None
    close_position: Optional[float] = None
    rr: Optional[float] = None
    app_volume_surge: Optional[float] = None

    def filtered_dict(self):
        row = {
            "symbol": self.symbol,
            "price": self.price,
            "stage": self.rejection_stage or "Unknown",
            "reason": self.rejection_reason or "No valid signal",
            "phase_one_passed": self.phase_one is not None,
        }
        if self.phase_one is not None:
            row.update(self.phase_one.to_dict())
        if self.indicator_volume_surge is not None:
            row["indicator_volume_surge"] = self.indicator_volume_surge
        if self.close_position is not None:
            row["close_position"] = self.close_position
        if self.rr is not None:
            row["rr"] = self.rr
        if self.app_volume_surge is not None:
            row["app_volume_surge"] = self.app_volume_surge
        return row


def is_current_signal_close(close_ms: int, now_ms: int) -> bool:
    """Only the immediately preceding UTC hour can produce a live signal."""
    return close_ms == (now_ms // 3_600_000) * 3_600_000 - 1


def is_closed(c: Candle, now_ms: int) -> bool:
    return c.close_time < now_ms


def _utc_day(ms: int) -> int:
    return ms // 86_400_000


def build_daily(hourly: List[Candle]) -> List[Candle]:
    """Aggregate closed 1H candles into UTC daily candles."""
    if not hourly:
        return []
    out: List[Candle] = []
    cur_day = None
    o = h = l = c = vol = qv = None
    open_ms = close_ms = None
    for bar in hourly:
        day = _utc_day(bar.open_time)
        if cur_day is None or day != cur_day:
            if cur_day is not None:
                out.append(Candle(open_ms, o, h, l, c, vol, close_ms, qv))
            cur_day = day
            open_ms = bar.open_time
            o, h, l, c = bar.open, bar.high, bar.low, bar.close
            vol, qv = bar.volume, bar.quote_volume
            close_ms = bar.close_time
        else:
            h = max(h, bar.high)
            l = min(l, bar.low)
            c = bar.close
            vol += bar.volume
            qv += bar.quote_volume
            close_ms = bar.close_time
    out.append(Candle(open_ms, o, h, l, c, vol, close_ms, qv))
    return out


def latest_prior_nr7_box(hourly: List[Candle], signal_index: int) -> Optional[Tuple[float, float]]:
    """Latest NR7 from a PRIOR completed UTC day, matching the app/Pine state."""
    sig_day = _utc_day(hourly[signal_index].open_time)
    prior_hourly = [x for x in hourly[: signal_index + 1] if _utc_day(x.open_time) < sig_day]
    daily = [d for d in build_daily(prior_hourly) if d.open_time % 86_400_000 == 0 and d.close_time - d.open_time == 86_400_000 - 1]
    if len(daily) < 7:
        return None
    box = None
    ranges = [d.high - d.low for d in daily]
    for i in range(6, len(daily)):
        if ranges[i] <= min(ranges[i - 6 : i + 1]):
            box = (daily[i].high, daily[i].low)
    return box


def previous_completed_daily(hourly: List[Candle], signal_index: int) -> Optional[Candle]:
    """Immediately previous completed UTC daily candle for Classic Pivot R2."""
    sig_day = _utc_day(hourly[signal_index].open_time)
    prior_hourly = [x for x in hourly[: signal_index + 1] if _utc_day(x.open_time) < sig_day]
    daily = [d for d in build_daily(prior_hourly) if d.open_time % 86_400_000 == 0 and d.close_time - d.open_time == 86_400_000 - 1]
    return daily[-1] if daily else None


def app_volume_surge(hourly: List[Candle], signal_index: int) -> Optional[float]:
    """App rule: current UTC day's accumulated quote volume / prior 8 full UTC days average."""
    sig_day = _utc_day(hourly[signal_index].open_time)
    today_qv = sum(
        x.quote_volume for x in hourly[: signal_index + 1]
        if _utc_day(x.open_time) == sig_day
    )
    prior = [x for x in hourly[: signal_index + 1] if _utc_day(x.open_time) < sig_day]
    daily = [d for d in build_daily(prior) if d.open_time % 86_400_000 == 0 and d.close_time - d.open_time == 86_400_000 - 1]
    if len(daily) < 8:
        return None
    baseline = sum(x.quote_volume for x in daily[-8:]) / 8.0
    if baseline <= 0:
        return None
    return today_qv / baseline


def is_stablecoin_at_signal(hourly: List[Candle], signal_index: int) -> bool:
    """Causal reproduction of the saved app's up-to-20-daily-bar stablecoin filter."""
    partial = hourly[max(0, signal_index - 24 * 21): signal_index + 1]
    daily = build_daily(partial)[-20:]
    if len(daily) < 10:
        return False
    price = partial[-1].close
    if price <= 0:
        return False
    range_pct = (max(d.high for d in daily) - min(d.low for d in daily)) / price * 100.0
    avg_range = sum(((d.high - d.low) / d.close * 100.0) for d in daily if d.close > 0) / len(daily)
    if range_pct < 3.0 or avg_range < 0.4:
        return True
    if any(abs(price - peg) / peg < 0.15 for peg in (1.0, 0.1)) and avg_range < 1.0:
        return True
    return 1500 < price < 5000 and avg_range < 0.5


def _crossover(prev_close: float, close: float, level: Optional[float]) -> bool:
    return level is not None and prev_close <= level < close


def _crossunder(prev_close: float, close: float, level: Optional[float]) -> bool:
    return level is not None and prev_close >= level > close


def strong_pivot_state(candles: List[Candle], swing_length: int):
    """Translation of the saved Pine LuxAlgo-compatible state over closed 1H candles."""
    out: List[Tuple[bool, Optional[float]]] = []
    swing_leg = 0
    current_swing_high = None
    current_swing_low = None
    swing_high_crossed = False
    swing_low_crossed = False
    swing_trend = 0
    confirmed_highs: List[Tuple[float, int]] = []

    for i, c in enumerate(candles):
        confirmed_highs = [
            (lvl, bar) for (lvl, bar) in confirmed_highs
            if not (i > bar and c.high >= lvl)
        ]

        previous_high, previous_low = current_swing_high, current_swing_low
        previous_leg = swing_leg
        new_leg_high = False
        new_leg_low = False
        pivot_i = i - swing_length
        if pivot_i >= 0:
            recent = candles[i - swing_length + 1: i + 1]
            recent_high = max(x.high for x in recent)
            recent_low = min(x.low for x in recent)
            new_leg_high = candles[pivot_i].high > recent_high
            new_leg_low = candles[pivot_i].low < recent_low

        if new_leg_high:
            swing_leg = 0
        elif new_leg_low:
            swing_leg = 1

        new_pivot = swing_leg != previous_leg
        new_pivot_low = new_pivot and swing_leg == 1
        new_pivot_high = new_pivot and swing_leg == 0

        if new_pivot_high and pivot_i >= 0:
            current_swing_high = candles[pivot_i].high
            swing_high_crossed = False
            confirmed_highs.append((current_swing_high, i))
        if new_pivot_low and pivot_i >= 0:
            current_swing_low = candles[pivot_i].low
            swing_low_crossed = False

        if i > 0:
            pc = candles[i - 1].close
            if current_swing_high is not None and (previous_high is not None and pc <= previous_high and c.close > current_swing_high) and not swing_high_crossed:
                swing_high_crossed = True
                swing_trend = 1
            if current_swing_low is not None and (previous_low is not None and pc >= previous_low and c.close < current_swing_low) and not swing_low_crossed:
                swing_low_crossed = True
                swing_trend = -1

        current_high_is_strong = swing_trend == -1
        target = None
        if current_high_is_strong:
            eligible = [lvl for (lvl, _) in confirmed_highs if lvl > c.high]
            if eligible:
                target = min(eligible)
        out.append((current_high_is_strong, target))
    return out


def _confidence(indicator_surge: float, close_position: float, rr: float, cfg: StrategyConfig):
    volume_strength = (min(max((indicator_surge - cfg.indicator_minimum_surge) / cfg.indicator_minimum_surge, 0.0), 1.0)
                       if cfg.indicator_minimum_surge > 0 else 0.0)
    close_room = 1.0 - cfg.minimum_close_position
    close_strength = 1.0 if close_room <= 0 else min(max((close_position - cfg.minimum_close_position) / close_room, 0.0), 1.0)
    rr_strength = min(max((rr - cfg.pine_minimum_reward_risk) / 2.0, 0.0), 1.0)
    raw = 50.0 + 20.0 * volume_strength + 15.0 * close_strength + 15.0 * rr_strength
    score = int(math.floor(raw + 0.5))
    level = "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else "LOW")
    return score, level


def reward_risk_is_accepted(rr: float, minimum: float = 1.0, maximum=None) -> bool:
    """Pine uses a strict lower bound and has no upper R:R bound.

    maximum is accepted only for callers of the old API; it is ignored.
    """
    return math.isfinite(rr) and rr > minimum


def entry_day_allowed(signal_close_ms: int) -> bool:
    # Binance closeTime is inclusive (...59.999); Pine time_close is the next
    # exact boundary. Sunday 23:00's candle therefore closes on Monday.
    return datetime.fromtimestamp((signal_close_ms + 1) / 1000, timezone.utc).weekday() not in (0, 6)


def stop_distance_allowed(entry: float, stop: float) -> bool:
    if not (math.isfinite(entry) and math.isfinite(stop) and entry > 0):
        return False
    distance = (Decimal(str(entry)) - Decimal(str(stop))) / Decimal(str(entry)) * 100
    return Decimal(0) < distance < Decimal("2.56")


def supertrend_state(candles: List[Candle], period=10, multiplier=3.0, wilder=True):
    """Supplied Pine's custom hl2 Supertrend, not a library variant.

    Wilder ATR seeds with SMA of the first period true ranges (first TR=H-L).
    SMA alternative matches ta.sma(ta.tr, period), whose first TR is na.
    Each result is None before initialized, then +1 (green) or -1 (red).
    """
    out, ranges = [], []
    atr = up = down = None
    trend = 1
    for i, bar in enumerate(candles):
        tr = bar.high - bar.low if i == 0 else max(bar.high - bar.low, abs(bar.high - candles[i-1].close), abs(bar.low - candles[i-1].close))
        if wilder or i > 0:
            ranges.append(tr)
        if len(ranges) < period:
            out.append(None)
            continue
        atr = ((atr * (period - 1) + tr) / period if wilder and atr is not None
               else sum(ranges[-period:]) / period)
        source = (bar.high + bar.low) / 2
        next_up, next_down = source - multiplier * atr, source + multiplier * atr
        old_up = next_up if up is None else up
        old_down = next_down if down is None else down
        pc = candles[i-1].close if i else bar.close
        up = max(next_up, old_up) if pc > old_up else next_up
        down = min(next_down, old_down) if pc < old_down else next_down
        if trend == -1 and bar.close > old_down:
            trend = 1
        elif trend == 1 and bar.close < old_up:
            trend = -1
        out.append(trend)
    return out


def latest_nr7_box_from_daily(daily: List[Candle], signal_day: int, now_ms: int) -> Optional[Tuple[float, float]]:
    """Find the latest completed NR7 box from an explicit daily fallback history."""
    completed = [d for d in daily if d.close_time < now_ms and _utc_day(d.open_time) < signal_day]
    if len(completed) < 7:
        return None
    ranges = [d.high - d.low for d in completed]
    box = None
    for i in range(6, len(completed)):
        if ranges[i] <= min(ranges[i - 6:i + 1]):
            box = (completed[i].high, completed[i].low)
    return box


def evaluate_latest_detailed(
    symbol: str,
    hourly: List[Candle],
    cfg: StrategyConfig,
    now_ms: int,
    box_override: Optional[Tuple[float, float]] = None,
) -> Evaluation:
    hourly = [c for c in hourly if is_closed(c, now_ms)]
    price = hourly[-1].close if hourly else 0.0

    def reject(
        stage: str,
        reason: str,
        phase_one: Optional[PhaseOneSignal] = None,
        indicator_volume_surge: Optional[float] = None,
        close_position: Optional[float] = None,
        rr: Optional[float] = None,
        app_volume_surge: Optional[float] = None,
    ) -> Evaluation:
        return Evaluation(
            symbol, price, phase_one, None, stage, reason,
            indicator_volume_surge, close_position, rr, app_volume_surge,
        )

    min_bars = max(cfg.indicator_volume_lookback + 2, cfg.swing_length * 2 + 3, 24 * 10)
    if len(hourly) < min_bars:
        return reject("Data", f"Need {min_bars} completed 1H candles; received {len(hourly)}")

    i = len(hourly) - 1
    c, p = hourly[i], hourly[i - 1]

    if not is_current_signal_close(c.close_time, now_ms):
        actual = datetime.fromtimestamp(c.close_time / 1000, tz=timezone.utc).isoformat()
        expected = datetime.fromtimestamp(
            ((now_ms // 3_600_000) * 3_600_000 - 1) / 1000, tz=timezone.utc
        ).isoformat()
        return reject("Data", f"Stale candles: latest close={actual}; expected={expected}")
    if (c.open_time != c.close_time - 3_600_000 + 1
            or p.open_time != c.open_time - 3_600_000
            or p.close_time != c.open_time - 1):
        return reject("Data", "Missing or malformed consecutive 1H candles")

    for j, bar in enumerate(hourly):
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume)
        if (not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0
                or bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close)
                or min(bar.volume, bar.quote_volume) < 0
                or bar.open_time % 3_600_000 != 0
                or bar.close_time != bar.open_time + 3_600_000 - 1
                or (j and bar.open_time != hourly[j-1].open_time + 3_600_000)):
            return reject("Data", "Invalid or non-consecutive 1H warmup data")

    box = box_override or latest_prior_nr7_box(hourly, i)
    if box is None:
        return reject("NR7", "No completed prior UTC-day NR7 box in the fast history window")
    box_high, box_low = box

    # Shared app + indicator confirmed breakout condition.
    if not (c.close > box_high and p.close <= box_high):
        return reject(
            "App Breakout",
            f"Closed 1H cross failed: close={c.close:.12g} prev={p.close:.12g} boxHigh={box_high:.12g}",
        )

    # APP CONDITIONS.
    app_surge = app_volume_surge(hourly, i)
    if app_surge is None and cfg.app_minimum_surge > 0:
        return reject("App Volume", "Insufficient prior daily quote-volume baseline")
    app_surge = app_surge if app_surge is not None else 0.0
    if cfg.app_minimum_surge > 0 and not (app_surge > cfg.app_minimum_surge):
        return reject(
            "App Volume",
            f"{app_surge:.4f}x is not strictly > {cfg.app_minimum_surge:g}x",
            app_volume_surge=app_surge,
        )
    if is_stablecoin_at_signal(hourly, i):
        return reject("Stablecoin", "Saved app stablecoin / low-volatility filter")

    closed_at = datetime.fromtimestamp(c.close_time / 1000, tz=timezone.utc).isoformat()
    phase_one = PhaseOneSignal(
        symbol=symbol,
        signal_close_ms=c.close_time,
        close_time_utc=closed_at,
        entry=c.close,
        previous_close=p.close,
        box_high=box_high,
        box_low=box_low,
        app_volume_surge=app_surge,
    )

    # INDICATOR CONDITIONS.
    prior_volumes = [x.volume for x in hourly[i - cfg.indicator_volume_lookback:i]]
    if len(prior_volumes) != cfg.indicator_volume_lookback:
        return reject("Indicator Volume", "Incomplete hourly volume lookback", phase_one)
    avg_vol = sum(prior_volumes) / len(prior_volumes)
    if avg_vol <= 0 and cfg.indicator_minimum_surge > 0:
        return reject("Indicator Volume", "Prior hourly average volume is zero", phase_one)
    indicator_surge = c.volume / avg_vol if avg_vol > 0 else 0.0
    if indicator_surge < cfg.indicator_minimum_surge:
        return reject(
            "Indicator Volume",
            f"{indicator_surge:.4f}x < {cfg.indicator_minimum_surge:g}x",
            phase_one,
            indicator_surge,
        )

    candle_range = c.high - c.low
    close_position = (c.close - c.low) / candle_range if candle_range > 0 else 0.0
    if close_position < cfg.minimum_close_position:
        return reject(
            "Close Position",
            f"{close_position:.4f} < {cfg.minimum_close_position:g}",
            phase_one,
            indicator_surge,
            close_position,
        )

    states = strong_pivot_state(hourly, cfg.swing_length)
    current_high_is_strong, strong_high_filter = states[i]
    if not current_high_is_strong:
        return reject("Strong High", "LuxAlgo-compatible Strong High state is not active", phase_one, indicator_surge, close_position)
    if strong_high_filter is None or not (strong_high_filter > c.high):
        return reject(
            "Strong High Filter",
            "No confirmed unrevisited pivot high above the breakout candle",
            phase_one,
            indicator_surge,
            close_position,
        )

    # Strong High remains an entry filter only. The actual TP is Classic
    # Pivot R2 from the immediately previous completed UTC daily candle.
    previous_daily = previous_completed_daily(hourly, i)
    if previous_daily is None:
        return reject("Pivot R2", "No previous completed UTC daily candle", phase_one, indicator_surge, close_position)
    classic_pivot = (previous_daily.high + previous_daily.low + previous_daily.close) / 3.0
    pivot_r2_target = classic_pivot + (previous_daily.high - previous_daily.low)
    if not (pivot_r2_target > c.high):
        return reject(
            "Pivot R2",
            f"Previous-day Pivot R2 {pivot_r2_target:.12g} is not above breakout high {c.high:.12g}",
            phase_one,
            indicator_surge,
            close_position,
        )

    risk = c.close - box_low
    reward = pivot_r2_target - c.close
    if risk <= 0 or reward <= 0:
        return reject("R:R", "Non-positive risk or reward", phase_one, indicator_surge, close_position)
    rr = reward / risk
    if not reward_risk_is_accepted(rr, cfg.pine_minimum_reward_risk):
        return reject(
            "R:R",
            f"{rr:.4f} must be strictly > {cfg.pine_minimum_reward_risk:g} (no upper limit)",
            phase_one,
            indicator_surge,
            close_position,
            rr,
        )

    if not entry_day_allowed(c.close_time):
        return reject("Entry Weekday", "Sunday/Monday entries excluded at UTC candle close", phase_one, indicator_surge, close_position, rr)
    if not stop_distance_allowed(c.close, box_low):
        return reject("Stop Distance", "Signal-close stop distance must be > 0 and strictly < 2.56%", phase_one, indicator_surge, close_position, rr)
    st_direction = supertrend_state(hourly, cfg.supertrend_period, cfg.supertrend_multiplier, cfg.supertrend_wilder)[i]
    if st_direction != 1:
        return reject("Supertrend", "Confirmed Supertrend must be initialized and green", phase_one, indicator_surge, close_position, rr)

    score, level = _confidence(indicator_surge, close_position, rr, cfg)
    signal = Signal(
        symbol=symbol,
        signal_close_ms=c.close_time,
        close_time_utc=closed_at,
        entry=c.close,
        tp=pivot_r2_target,
        sl=box_low,
        classic_pivot=classic_pivot,
        strong_high_filter=strong_high_filter,
        rr=rr,
        box_high=box_high,
        box_low=box_low,
        app_volume_surge=app_surge,
        indicator_volume_surge=indicator_surge,
        close_position=close_position,
        confidence_score=score,
        confidence_level=level,
        stop_distance_pct=(c.close - box_low) / c.close * 100,
        supertrend_direction=st_direction,
    )
    return Evaluation(symbol, price, phase_one, signal)


def evaluate_latest(symbol: str, hourly: List[Candle], cfg: StrategyConfig, now_ms: int) -> Optional[Signal]:
    """Compatibility wrapper used by the CLI and existing tests."""
    return evaluate_latest_detailed(symbol, hourly, cfg, now_ms).signal

