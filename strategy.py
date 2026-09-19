from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


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
    # App stage (exact anter-pre-final logic).
    app_minimum_surge: float = 1.5
    max_confirmed_age_minutes: int = 60

    # Indicator stage.
    indicator_volume_lookback: int = 168
    indicator_minimum_surge: float = 1.5
    minimum_close_position: float = 0.75
    minimum_reward_risk: float = 1.0
    maximum_reward_risk: float = 4.0
    swing_length: int = 50


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


def find_nr7_daily(daily: List[Candle]) -> Optional[Tuple[float, float]]:
    """Exact app search: latest PRIOR 1D candle whose range is the minimum of its 7-bar window."""
    if len(daily) < 10:
        return None
    ranges = [d.high - d.low for d in daily]
    current_idx = len(daily) - 1  # current, still-forming UTC daily candle
    # Mirrors Python: range(current_idx - 1, 6, -1)
    for i in range(current_idx - 1, 6, -1):
        last7 = ranges[i - 6:i + 1]
        if len(last7) == 7 and ranges[i] == min(last7):
            return daily[i].high, daily[i].low
    return None


def app_stablecoin(daily: List[Candle], current_price: float) -> bool:
    """Exact technical stablecoin/low-volatility filter from anter-pre-final."""
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
    """Current live 1D quote volume / average quote volume of the prior 8 daily candles."""
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
    """Exact saved Pine state at the last supplied closed 1H candle."""
    swing_leg = 0
    current_swing_high = None
    current_swing_low = None
    swing_high_crossed = False
    swing_low_crossed = False
    swing_trend = 0
    confirmed_highs: List[Tuple[float, int]] = []

    for i, c in enumerate(candles):
        # A target is removed only by a later candle, after its confirmation bar.
        confirmed_highs = [
            (lvl, bar) for (lvl, bar) in confirmed_highs
            if not (i > bar and c.high >= lvl)
        ]

        previous_leg = swing_leg
        new_leg_high = False
        new_leg_low = False
        pivot_i = i - swing_length
        if pivot_i >= 0:
            recent = candles[i - swing_length + 1:i + 1]
            new_leg_high = candles[pivot_i].high > max(x.high for x in recent)
            new_leg_low = candles[pivot_i].low < min(x.low for x in recent)

        if new_leg_high:
            swing_leg = 0
        elif new_leg_low:
            swing_leg = 1

        new_pivot = swing_leg != previous_leg
        if new_pivot and swing_leg == 0 and pivot_i >= 0:
            current_swing_high = candles[pivot_i].high
            swing_high_crossed = False
            confirmed_highs.append((current_swing_high, i))
        if new_pivot and swing_leg == 1 and pivot_i >= 0:
            current_swing_low = candles[pivot_i].low
            swing_low_crossed = False

        if i > 0:
            pc = candles[i - 1].close
            if current_swing_high is not None and _crossover(pc, c.close, current_swing_high) and not swing_high_crossed:
                swing_high_crossed = True
                swing_trend = 1
            if current_swing_low is not None and _crossunder(pc, c.close, current_swing_low) and not swing_low_crossed:
                swing_low_crossed = True
                swing_trend = -1

    current_high_is_strong = swing_trend == -1
    target = None
    if current_high_is_strong and candles:
        last_high = candles[-1].high
        eligible = [lvl for (lvl, _) in confirmed_highs if lvl > last_high]
        if eligible:
            target = min(eligible)
    return current_high_is_strong, target


def _confidence(indicator_surge: float, close_position: float, rr: float, cfg: StrategyConfig):
    volume_strength = min(max((indicator_surge - cfg.indicator_minimum_surge) / cfg.indicator_minimum_surge, 0.0), 1.0)
    close_room = 1.0 - cfg.minimum_close_position
    close_strength = 1.0 if close_room <= 0 else min(max((close_position - cfg.minimum_close_position) / close_room, 0.0), 1.0)
    rr_strength = min(max((rr - cfg.minimum_reward_risk) / 2.0, 0.0), 1.0)
    score = round(50.0 + 20.0 * volume_strength + 15.0 * close_strength + 15.0 * rr_strength)
    level = "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else "LOW")
    return score, level


def evaluate_latest(
    symbol: str,
    daily: List[Candle],
    hourly: List[Candle],
    current_price: float,
    cfg: StrategyConfig,
    now_ms: int,
) -> Tuple[Optional[Signal], Optional[Dict]]:
    """Return (valid final signal, app-confirmed candidate).

    The app candidate exists only after the exact app conditions pass.
    If the indicator rejects it, the candidate carries every computed field so
    the dashboard can show exactly why no trade was taken.
    """
    if len(daily) < 10 or len(hourly) < max(cfg.indicator_volume_lookback + 2, cfg.swing_length + 3):
        return None, None

    # ----- EXACT APP STAGE -----
    if app_stablecoin(daily, current_price):
        return None, None

    box = find_nr7_daily(daily)
    if box is None:
        return None, None
    box_high, box_low = box

    app_surge = exact_app_volume_surge(daily)
    if app_surge is None or app_surge < cfg.app_minimum_surge:
        return None, None

    # Like the app, Binance's final row is the live current 1H candle.
    signal_idx = len(hourly) - 2
    if signal_idx < 1:
        return None, None
    c = hourly[signal_idx]
    p = hourly[signal_idx - 1]
    if not (c.close > box_high and p.close <= box_high):
        return None, None

    age_minutes = int(max(0, now_ms - c.close_time) // 60_000)
    if cfg.max_confirmed_age_minutes >= 0 and age_minutes > cfg.max_confirmed_age_minutes:
        return None, None

    close_time_utc = datetime.fromtimestamp(c.close_time / 1000, tz=timezone.utc).isoformat()

    # ----- EXACT INDICATOR STAGE -----
    prior_volumes = [x.volume for x in hourly[signal_idx - cfg.indicator_volume_lookback:signal_idx]]
    indicator_surge = None
    if len(prior_volumes) == cfg.indicator_volume_lookback:
        avg_vol = sum(prior_volumes) / len(prior_volumes)
        if avg_vol > 0:
            indicator_surge = c.volume / avg_vol

    candle_range = c.high - c.low
    close_position = (c.close - c.low) / candle_range if candle_range > 0 else 0.0

    strong_state, target = strong_pivot_state(hourly[:signal_idx + 1], cfg.swing_length)

    risk = c.close - box_low
    reward = (target - c.close) if target is not None else None
    rr = (reward / risk) if reward is not None and risk > 0 else None

    failures: List[str] = []
    if indicator_surge is None:
        failures.append("Indicator volume baseline unavailable")
    elif indicator_surge < cfg.indicator_minimum_surge:
        failures.append(f"1H volume surge {indicator_surge:.2f}x < {cfg.indicator_minimum_surge:.2f}x")
    if close_position < cfg.minimum_close_position:
        failures.append(f"Close position {close_position:.3f} < {cfg.minimum_close_position:.2f}")
    if not strong_state:
        failures.append("Strong High state is false")
    elif target is None:
        failures.append("No confirmed unrevisited Strong High above the breakout candle")
    if risk <= 0:
        failures.append("Entry-to-box-low risk is not positive")
    elif target is not None:
        if rr is None or rr <= cfg.minimum_reward_risk:
            failures.append(f"R:R {rr if rr is not None else 0:.2f} <= {cfg.minimum_reward_risk:.2f}")
        elif rr > cfg.maximum_reward_risk:
            failures.append(f"R:R {rr:.2f} > {cfg.maximum_reward_risk:.2f}")

    candidate = {
        "symbol": symbol,
        "signal_close_ms": c.close_time,
        "close_time_utc": close_time_utc,
        "entry": c.close,
        "tp": target,
        "sl": box_low,
        "rr": rr,
        "box_high": box_high,
        "box_low": box_low,
        "app_volume_surge": app_surge,
        "indicator_volume_surge": indicator_surge,
        "close_position": close_position,
        "confidence_score": None,
        "confidence_level": None,
        "age_minutes": age_minutes,
        "app_pass": True,
        "indicator_pass": not failures,
        "reason": "; ".join(failures) if failures else None,
    }

    if failures:
        return None, candidate

    score, level = _confidence(float(indicator_surge), close_position, float(rr), cfg)
    signal = Signal(
        symbol=symbol,
        signal_close_ms=c.close_time,
        close_time_utc=close_time_utc,
        entry=c.close,
        tp=float(target),
        sl=box_low,
        rr=float(rr),
        box_high=box_high,
        box_low=box_low,
        app_volume_surge=app_surge,
        indicator_volume_surge=float(indicator_surge),
        close_position=close_position,
        confidence_score=score,
        confidence_level=level,
    )
    candidate["tp"] = signal.tp
    candidate["rr"] = signal.rr
    candidate["confidence_score"] = score
    candidate["confidence_level"] = level
    return signal, candidate
