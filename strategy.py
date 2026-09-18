from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
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
    minimum_reward_risk: float = 1.0        # strict > 1
    maximum_reward_risk: float = 4.0        # <= 4
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
    daily = build_daily(prior_hourly)
    if len(daily) < 7:
        return None
    box = None
    ranges = [d.high - d.low for d in daily]
    for i in range(6, len(daily)):
        if ranges[i] <= min(ranges[i - 6 : i + 1]):
            box = (daily[i].high, daily[i].low)
    return box


def app_volume_surge(hourly: List[Candle], signal_index: int) -> Optional[float]:
    """App rule: current UTC day's accumulated quote volume / prior 8 full UTC days average."""
    sig_day = _utc_day(hourly[signal_index].open_time)
    today_qv = sum(
        x.quote_volume for x in hourly[: signal_index + 1]
        if _utc_day(x.open_time) == sig_day
    )
    prior = [x for x in hourly[: signal_index + 1] if _utc_day(x.open_time) < sig_day]
    daily = build_daily(prior)
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
            if current_swing_high is not None and _crossover(pc, c.close, current_swing_high) and not swing_high_crossed:
                swing_high_crossed = True
                swing_trend = 1
            if current_swing_low is not None and _crossunder(pc, c.close, current_swing_low) and not swing_low_crossed:
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
    volume_strength = min(max((indicator_surge - cfg.indicator_minimum_surge) / cfg.indicator_minimum_surge, 0.0), 1.0)
    close_room = 1.0 - cfg.minimum_close_position
    close_strength = 1.0 if close_room <= 0 else min(max((close_position - cfg.minimum_close_position) / close_room, 0.0), 1.0)
    rr_strength = min(max((rr - cfg.minimum_reward_risk) / 2.0, 0.0), 1.0)
    score = round(50.0 + 20.0 * volume_strength + 15.0 * close_strength + 15.0 * rr_strength)
    level = "HIGH" if score >= 80 else ("MEDIUM" if score >= 65 else "LOW")
    return score, level


def evaluate_latest(symbol: str, hourly: List[Candle], cfg: StrategyConfig, now_ms: int) -> Optional[Signal]:
    hourly = [c for c in hourly if is_closed(c, now_ms)]
    min_bars = max(cfg.indicator_volume_lookback + 2, cfg.swing_length * 2 + 3, 24 * 10)
    if len(hourly) < min_bars:
        return None

    i = len(hourly) - 1
    c, p = hourly[i], hourly[i - 1]

    box = latest_prior_nr7_box(hourly, i)
    if box is None:
        return None
    box_high, box_low = box

    # Shared app + indicator confirmed breakout condition.
    if not (c.close > box_high and p.close <= box_high):
        return None

    # APP CONDITIONS.
    app_surge = app_volume_surge(hourly, i)
    if app_surge is None or not (app_surge > cfg.app_minimum_surge):
        return None
    if is_stablecoin_at_signal(hourly, i):
        return None

    # INDICATOR CONDITIONS.
    prior_volumes = [x.volume for x in hourly[i - cfg.indicator_volume_lookback:i]]
    if len(prior_volumes) != cfg.indicator_volume_lookback:
        return None
    avg_vol = sum(prior_volumes) / len(prior_volumes)
    if avg_vol <= 0:
        return None
    indicator_surge = c.volume / avg_vol
    if indicator_surge < cfg.indicator_minimum_surge:
        return None

    candle_range = c.high - c.low
    close_position = (c.close - c.low) / candle_range if candle_range > 0 else 0.0
    if close_position < cfg.minimum_close_position:
        return None

    states = strong_pivot_state(hourly, cfg.swing_length)
    current_high_is_strong, target = states[i]
    if not current_high_is_strong or target is None or not (target > c.high):
        return None

    risk = c.close - box_low
    reward = target - c.close
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if not (rr > cfg.minimum_reward_risk and rr <= cfg.maximum_reward_risk):
        return None

    score, level = _confidence(indicator_surge, close_position, rr, cfg)
    dt = datetime.fromtimestamp(c.close_time / 1000, tz=timezone.utc).isoformat()
    return Signal(
        symbol=symbol,
        signal_close_ms=c.close_time,
        close_time_utc=dt,
        entry=c.close,
        tp=target,
        sl=box_low,
        rr=rr,
        box_high=box_high,
        box_low=box_low,
        app_volume_surge=app_surge,
        indicator_volume_surge=indicator_surge,
        close_position=close_position,
        confidence_score=score,
        confidence_level=level,
    )
