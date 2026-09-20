from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


DAY_MS = 86_400_000


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
    # App stage.
    app_minimum_surge: float = 1.5
    max_confirmed_age_minutes: int = 50

    # Exact pasted Pine indicator inputs.
    indicator_volume_lookback: int = 168
    indicator_minimum_surge: float = 1.5
    minimum_close_position: float = 0.75
    pine_minimum_reward_risk: float = 1.0
    swing_length: int = 50

    # Extra autonomous-agent gate requested by the user, applied AFTER Pine validBreakout.
    minimum_reward_risk: float = 2.0
    maximum_reward_risk: float = 4.0


@dataclass
class Signal:
    symbol: str
    signal_close_ms: int
    close_time_utc: str
    entry: float
    tp: float
    sl: float
    rr: float
    box_high: float
    box_low: float
    app_volume_surge: float
    indicator_volume_surge: float
    close_position: float
    confidence_score: int
    confidence_level: str

    def to_dict(self):
        return asdict(self)


def _day_id(ms: int) -> int:
    return int(ms) // DAY_MS


def _latest_closed_hour_index(hourly: List[Candle], now_ms: int) -> int:
    """Do not assume Binance has already published the new live row at exactly :00."""
    idx = -1
    for i, bar in enumerate(hourly):
        if bar.close_time < now_ms:
            idx = i
        else:
            break
    return idx


def find_nr7_daily(daily: List[Candle]) -> Optional[Tuple[float, float]]:
    """App/current-time latest completed NR7 box."""
    if len(daily) < 10:
        return None
    ranges = [d.high - d.low for d in daily]
    # Last row is normally the current forming UTC day.
    current_idx = len(daily) - 1
    for i in range(current_idx - 1, 6, -1):
        last7 = ranges[i - 6:i + 1]
        if len(last7) == 7 and ranges[i] <= min(last7):
            return daily[i].high, daily[i].low
    return None


def pine_nr7_box_at_signal(daily: List[Candle], signal_bar: Candle) -> Optional[Tuple[float, float]]:
    """Reconstruct Pine var boxHigh/boxLow as they existed on the signal bar.

    Pine updates the box only at the first bar of a new UTC day using the
    previous completed daily candle. Therefore a 23:00-23:59 breakout must NOT
    use that same day's daily range even when the cloud evaluates it after 00:00.
    """
    signal_day = _day_id(signal_bar.open_time)
    completed = [d for d in daily if _day_id(d.open_time) < signal_day]
    if len(completed) < 7:
        return None
    ranges = [d.high - d.low for d in completed]
    for i in range(len(completed) - 1, 5, -1):
        last7 = ranges[i - 6:i + 1]
        if ranges[i] <= min(last7):
            return completed[i].high, completed[i].low
    return None


def previous_completed_daily_at_signal(daily: List[Candle], signal_bar: Candle) -> Optional[Candle]:
    """Equivalent to request.security(..., 'D', high/low/close[1]) on the signal bar."""
    signal_day = _day_id(signal_bar.open_time)
    completed = [d for d in daily if _day_id(d.open_time) < signal_day]
    return completed[-1] if completed else None


def app_stablecoin(daily: List[Candle], current_price: float) -> bool:
    if len(daily) < 10:
        return False
    recent = daily[-20:]
    price_range = max(x.high for x in recent) - min(x.low for x in recent)
    range_pct = (price_range / current_price) * 100.0 if current_price > 0 else 100.0
    if range_pct < 3.0:
        return True
    vals = [((x.high - x.low) / x.close * 100.0) for x in recent if x.close > 0]
    if not vals:
        return False
    avg_range = sum(vals) / len(vals)
    if avg_range < 0.4:
        return True
    for peg in (1.0, 0.1):
        if abs(current_price - peg) / peg < 0.15 and avg_range < 1.0:
            return True
    return 1500 < current_price < 5000 and avg_range < 0.5


def exact_app_volume_surge(daily: List[Candle]) -> Optional[float]:
    if len(daily) < 2:
        return None
    p = len(daily) - 1
    prior = daily[max(0, p - 8):p]
    if not prior:
        return None
    avg = sum(x.quote_volume for x in prior) / len(prior)
    if avg <= 0:
        return None
    return daily[p].quote_volume / avg


def _crossover(prev_close: float, close: float, level: Optional[float]) -> bool:
    return level is not None and prev_close <= level < close


def _crossunder(prev_close: float, close: float, level: Optional[float]) -> bool:
    return level is not None and prev_close >= level > close


def strong_pivot_state(candles: List[Candle], swing_length: int) -> Tuple[bool, Optional[float]]:
    """Line-for-line state translation of the pasted LuxAlgo-compatible filter."""
    swing_leg = 0
    current_swing_high: Optional[float] = None
    current_swing_low: Optional[float] = None
    swing_high_crossed = False
    swing_low_crossed = False
    swing_trend = 0
    confirmed_highs: List[Tuple[float, int]] = []

    for i, c in enumerate(candles):
        # Pine removes a confirmed high only when a LATER bar touches it.
        confirmed_highs = [
            (level, confirmation_bar)
            for (level, confirmation_bar) in confirmed_highs
            if not (i > confirmation_bar and c.high >= level)
        ]

        previous_leg = swing_leg
        pivot_i = i - swing_length
        new_leg_high = False
        new_leg_low = False

        if pivot_i >= 0:
            # Pine ta.highest(high, swingLength) / ta.lowest() on the current bar
            # covers current through swingLength-1 bars ago; high[swingLength]
            # and low[swingLength] are the candidate pivot values just outside it.
            recent = candles[i - swing_length + 1:i + 1]
            new_leg_high = candles[pivot_i].high > max(x.high for x in recent)
            new_leg_low = candles[pivot_i].low < min(x.low for x in recent)

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
            prev_close = candles[i - 1].close
            if (
                current_swing_high is not None
                and _crossover(prev_close, c.close, current_swing_high)
                and not swing_high_crossed
            ):
                swing_high_crossed = True
                swing_trend = 1

            if (
                current_swing_low is not None
                and _crossunder(prev_close, c.close, current_swing_low)
                and not swing_low_crossed
            ):
                swing_low_crossed = True
                swing_trend = -1

    current_high_is_strong = swing_trend == -1
    fixed_strong_high_filter: Optional[float] = None

    if current_high_is_strong and candles and confirmed_highs:
        breakout_high = candles[-1].high
        for level, _ in confirmed_highs:
            if level > breakout_high and (
                fixed_strong_high_filter is None or level < fixed_strong_high_filter
            ):
                fixed_strong_high_filter = level

    return current_high_is_strong, fixed_strong_high_filter


def previous_day_pivot_r2_at_signal(
    daily: List[Candle], signal_bar: Candle
) -> Tuple[Optional[float], Optional[float]]:
    previous = previous_completed_daily_at_signal(daily, signal_bar)
    if previous is None:
        return None, None
    classic_pivot = (previous.high + previous.low + previous.close) / 3.0
    pivot_r2 = classic_pivot + (previous.high - previous.low)
    return classic_pivot, pivot_r2


def _confidence(indicator_surge: float, close_position: float, rr: float, cfg: StrategyConfig):
    """Exact Pine confidence formula: it uses Pine minimumRewardRisk=1.0."""
    volume_strength = min(
        max((indicator_surge - cfg.indicator_minimum_surge) / cfg.indicator_minimum_surge, 0.0),
        1.0,
    )
    close_position_room = 1.0 - cfg.minimum_close_position
    close_strength = (
        min(max((close_position - cfg.minimum_close_position) / close_position_room, 0.0), 1.0)
        if close_position_room > 0
        else 1.0
    )
    reward_risk_strength = min(
        max((rr - cfg.pine_minimum_reward_risk) / 2.0, 0.0),
        1.0,
    )
    score = round(
        50.0
        + 20.0 * volume_strength
        + 15.0 * close_strength
        + 15.0 * reward_risk_strength
    )
    level = "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else "LOW")
    return score, level


def app_scan_trace(
    symbol: str,
    daily: List[Candle],
    hourly: List[Candle],
    current_price: float,
    cfg: StrategyConfig,
    now_ms: int,
) -> Dict:
    """Classify one symbol at the app stage for the latest-scan dashboard."""
    row: Dict = {
        "symbol": symbol,
        "price": current_price,
        "category": "Filter",
        "reason": "",
        "app_volume_surge": None,
        "box_high": None,
        "box_low": None,
        "status": "FILTERED",
    }

    if len(daily) < 10:
        row["category"] = "Data"
        row["reason"] = "Not enough 1D data"
        return row
    if len(hourly) < 2:
        row["category"] = "Data"
        row["reason"] = "Not enough 1H data"
        return row

    if app_stablecoin(daily, current_price):
        row["category"] = "Filter"
        row["reason"] = "Stablecoin / low volatility"
        return row

    box = find_nr7_daily(daily)
    if box is None:
        row["category"] = "NR7"
        row["reason"] = "No NR7 box"
        return row
    box_high, box_low = box
    row["box_high"] = box_high
    row["box_low"] = box_low

    surge = exact_app_volume_surge(daily)
    row["app_volume_surge"] = surge
    if surge is None:
        row["category"] = "Volume"
        row["reason"] = "Volume baseline unavailable"
        return row
    if surge < cfg.app_minimum_surge:
        row["category"] = "Volume"
        row["reason"] = f"Vol surge {surge:.2f}x < {cfg.app_minimum_surge:.2f}x"
        return row

    signal_idx = _latest_closed_hour_index(hourly, now_ms)
    if signal_idx < 1:
        row["category"] = "Data"
        row["reason"] = "No completed 1H breakout candle available"
        return row

    last_completed = hourly[signal_idx]
    before_that = hourly[signal_idx - 1]
    confirmed = last_completed.close > box_high and before_that.close <= box_high

    age_minutes = int(max(0, now_ms - last_completed.close_time) // 60_000)
    if confirmed and age_minutes <= cfg.max_confirmed_age_minutes:
        row["status"] = "CONFIRMED"
        row["category"] = "Confirmed"
        row["reason"] = f"Fresh confirmed breakout ({age_minutes} min old)"
        row["signal_close_ms"] = last_completed.close_time
        return row

    row["category"] = "Breakout"
    if confirmed:
        row["reason"] = (
            f"Confirmed breakout is {age_minutes} min old > "
            f"{cfg.max_confirmed_age_minutes} min"
        )
    else:
        row["reason"] = "No fresh confirmed breakout"
    return row


def evaluate_latest(
    symbol: str,
    daily: List[Candle],
    hourly: List[Candle],
    current_price: float,
    cfg: StrategyConfig,
    now_ms: int,
) -> Tuple[Optional[Signal], Optional[Dict]]:
    """Exact pasted Pine indicator, followed by the extra strict 2 < R:R < 4 gate."""
    if len(daily) < 10 or len(hourly) < max(
        cfg.indicator_volume_lookback + 1,
        cfg.swing_length + 2,
    ):
        return None, None

    # ----- APP STAGE -----
    if app_stablecoin(daily, current_price):
        return None, None

    app_box = find_nr7_daily(daily)
    if app_box is None:
        return None, None
    app_box_high, app_box_low = app_box

    app_surge = exact_app_volume_surge(daily)
    if app_surge is None or app_surge < cfg.app_minimum_surge:
        return None, None

    signal_idx = _latest_closed_hour_index(hourly, now_ms)
    if signal_idx < 1:
        return None, None

    c = hourly[signal_idx]
    p = hourly[signal_idx - 1]
    app_confirmed_breakout = c.close > app_box_high and p.close <= app_box_high
    if not app_confirmed_breakout:
        return None, None

    age_minutes = int(max(0, now_ms - c.close_time) // 60_000)
    if cfg.max_confirmed_age_minutes >= 0 and age_minutes > cfg.max_confirmed_age_minutes:
        return None, None

    close_time_utc = datetime.fromtimestamp(
        c.close_time / 1000,
        tz=timezone.utc,
    ).isoformat()

    # ----- EXACT PASTED PINE INDICATOR STAGE -----
    pine_box = pine_nr7_box_at_signal(daily, c)
    pine_box_high = pine_box[0] if pine_box else None
    pine_box_low = pine_box[1] if pine_box else None

    pine_confirmed_breakout = (
        pine_box_high is not None
        and c.close > pine_box_high
        and p.close <= pine_box_high
    )

    prior_volumes = hourly[
        signal_idx - cfg.indicator_volume_lookback:signal_idx
    ]
    indicator_surge: Optional[float] = None
    if len(prior_volumes) == cfg.indicator_volume_lookback:
        average_volume = (
            sum(x.volume for x in prior_volumes)
            / cfg.indicator_volume_lookback
        )
        if average_volume > 0:
            indicator_surge = c.volume / average_volume

    candle_range = c.high - c.low
    close_position = (
        (c.close - c.low) / candle_range
        if candle_range > 0
        else 0.0
    )

    current_high_is_strong, fixed_strong_high_filter = strong_pivot_state(
        hourly[:signal_idx + 1],
        cfg.swing_length,
    )

    classic_pivot, pivot_r2_target = previous_day_pivot_r2_at_signal(daily, c)

    risk = (
        c.close - pine_box_low
        if pine_box_low is not None
        else None
    )
    reward = (
        pivot_r2_target - c.close
        if pivot_r2_target is not None
        else None
    )
    reward_risk = (
        reward / risk
        if reward is not None and risk is not None and risk > 0
        else None
    )
    pivot_r2_above_breakout = (
        pivot_r2_target is not None
        and pivot_r2_target > c.high
    )

    pine_failures: List[str] = []

    if not pine_confirmed_breakout:
        pine_failures.append("Pine confirmed NR7 breakout is false")

    if indicator_surge is None:
        pine_failures.append("Indicator volume baseline unavailable")
    elif indicator_surge < cfg.indicator_minimum_surge:
        pine_failures.append(
            f"1H volume surge {indicator_surge:.2f}x < "
            f"{cfg.indicator_minimum_surge:.2f}x"
        )

    if close_position < cfg.minimum_close_position:
        pine_failures.append(
            f"Close position {close_position:.3f} < "
            f"{cfg.minimum_close_position:.2f}"
        )

    if not current_high_is_strong:
        pine_failures.append("Strong High state is false")
    elif fixed_strong_high_filter is None:
        pine_failures.append(
            "No confirmed unrevisited Strong High above the breakout candle"
        )

    if pivot_r2_target is None:
        pine_failures.append("Previous-day Pivot R2 unavailable")
    elif not pivot_r2_above_breakout:
        pine_failures.append(
            f"Pivot R2 {pivot_r2_target:.8g} is not above "
            f"breakout high {c.high:.8g}"
        )

    if risk is None or risk <= 0:
        pine_failures.append("Entry-to-box-low risk is not positive")
    elif reward_risk is None:
        pine_failures.append("Pivot R2 reward/risk unavailable")
    elif reward_risk <= cfg.pine_minimum_reward_risk:
        pine_failures.append(
            f"Pine R:R {reward_risk:.2f} <= "
            f"{cfg.pine_minimum_reward_risk:.2f}"
        )

    # Pine confidence is calculated from the Pine inputs, not the extra 2R gate.
    confidence_score = None
    confidence_level = None
    if (
        indicator_surge is not None
        and reward_risk is not None
    ):
        confidence_score, confidence_level = _confidence(
            float(indicator_surge),
            close_position,
            float(reward_risk),
            cfg,
        )

    # ----- EXTRA AGENT R:R GATE REQUESTED BY USER -----
    rr_failures: List[str] = []
    if reward_risk is None:
        rr_failures.append("Agent R:R unavailable")
    else:
        if reward_risk <= cfg.minimum_reward_risk:
            rr_failures.append(
                f"Agent R:R {reward_risk:.2f} <= "
                f"{cfg.minimum_reward_risk:.2f}"
            )
        if reward_risk >= cfg.maximum_reward_risk:
            rr_failures.append(
                f"Agent R:R {reward_risk:.2f} >= "
                f"{cfg.maximum_reward_risk:.2f}"
            )

    failures = pine_failures + rr_failures

    candidate = {
        "symbol": symbol,
        "signal_close_ms": c.close_time,
        "close_time_utc": close_time_utc,
        "entry": c.close,
        "tp": pivot_r2_target,
        "sl": pine_box_low,
        "rr": reward_risk,
        "box_high": pine_box_high,
        "box_low": pine_box_low,
        "app_box_high": app_box_high,
        "app_box_low": app_box_low,
        "app_volume_surge": app_surge,
        "indicator_volume_surge": indicator_surge,
        "close_position": close_position,
        "confidence_score": confidence_score,
        "confidence_level": confidence_level,
        "age_minutes": age_minutes,
        "app_pass": True,
        "indicator_pass": not pine_failures,
        "rr_gate_pass": not rr_failures,
        "strong_high_state": current_high_is_strong,
        "fixed_strong_high_filter": fixed_strong_high_filter,
        "classic_pivot": classic_pivot,
        "pivot_r2_target": pivot_r2_target,
        "reason": "; ".join(failures) if failures else None,
    }

    if failures:
        return None, candidate

    signal = Signal(
        symbol=symbol,
        signal_close_ms=c.close_time,
        close_time_utc=close_time_utc,
        entry=c.close,
        tp=float(pivot_r2_target),
        sl=float(pine_box_low),
        rr=float(reward_risk),
        box_high=float(pine_box_high),
        box_low=float(pine_box_low),
        app_volume_surge=app_surge,
        indicator_volume_surge=float(indicator_surge),
        close_position=close_position,
        confidence_score=int(confidence_score),
        confidence_level=str(confidence_level),
    )

    return signal, candidate
