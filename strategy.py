from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

DAY_MS = 86_400_000

# Spec default universe: 10 Binance USDT pairs (editable from the dashboard).
DEFAULT_UNIVERSE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT",
]


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
class DonchianConfig:
    entry_period: int = 55          # entry channel: highest high of previous N days
    exit_period: int = 20           # exit channel: lowest low of previous N days
    atr_period: int = 20            # Wilder ATR on daily candles
    atr_multiplier: float = 2.0     # sizing distance = multiplier x ATR
    risk_pct: float = 1.0           # risk % of equity per trade
    daily_history_bars: int = 120   # history fetch page size; ATR uses full history
    entry_grace_minutes: int = 15   # entries only within N minutes after 00:00 UTC


@dataclass
class Signal:
    symbol: str
    signal_close_ms: int            # close time of the just-closed daily candle
    close_time_utc: str
    direction: str                  # "LONG" = entry, "EXIT" = channel exit
    entry: float                    # reference price: the signal day's close
    entry_channel: float            # highest high of the previous entry_period days
    exit_channel: float             # lowest low of the previous exit_period days
    atr: float                      # Wilder ATR(atr_period) on daily candles
    stop_distance: float            # sizing distance = multiplier x ATR (no stop order is placed)
    universe: str = ""
    strategy_version: str = "donchian_55_20_v1"

    def to_dict(self):
        return asdict(self)


@dataclass
class Evaluation:
    symbol: str
    price: float
    signal: Optional[Signal] = None
    rejection_stage: Optional[str] = None
    rejection_reason: Optional[str] = None
    entry_channel: Optional[float] = None
    exit_channel: Optional[float] = None
    atr: Optional[float] = None
    holding: bool = False

    @property
    def action(self) -> str:
        if self.signal is not None:
            return self.signal.direction
        return "NO_ACTION"

    def filtered_dict(self) -> dict:
        row = {
            "symbol": self.symbol,
            "price": self.price,
            "stage": self.rejection_stage or "No signal",
            "reason": self.rejection_reason or "No channel signal today",
            "holding": self.holding,
        }
        if self.entry_channel is not None:
            row["entry_channel"] = self.entry_channel
        if self.exit_channel is not None:
            row["exit_channel"] = self.exit_channel
        if self.atr is not None:
            row["atr"] = self.atr
        return row


def is_closed(c: Candle, now_ms: int) -> bool:
    return c.close_time < now_ms


def day_boundary_ms(now_ms: int) -> int:
    """Start of the current UTC day in epoch milliseconds."""
    return (now_ms // DAY_MS) * DAY_MS


def is_current_signal_close(close_ms: int, now_ms: int) -> bool:
    """True only for the most recently completed daily close (00:00 UTC)."""
    return close_ms == day_boundary_ms(now_ms) - 1


def within_entry_grace(now_ms: int, grace_minutes: int) -> bool:
    """Entries execute only in the window right after the 00:00 UTC boundary."""
    elapsed = now_ms - day_boundary_ms(now_ms)
    return 0 <= elapsed <= max(0, int(grace_minutes)) * 60_000


def wilders_atr(daily: List[Candle], period: int) -> Optional[float]:
    """Wilder ATR: SMA seed of the first `period` true ranges, then smoothing."""
    if period < 1 or len(daily) < period:
        return None
    true_ranges = []
    for i, bar in enumerate(daily):
        if i == 0:
            true_ranges.append(bar.high - bar.low)
        else:
            prev_close = daily[i - 1].close
            true_ranges.append(max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            ))
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _valid_daily(candles: List[Candle]) -> bool:
    for j, bar in enumerate(candles):
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume)
        if (not all(math.isfinite(v) for v in values)
                or min(values[:4]) <= 0
                or bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
                or min(bar.volume, bar.quote_volume) < 0
                or bar.open_time % DAY_MS != 0
                or bar.close_time != bar.open_time + DAY_MS - 1
                or (j and bar.open_time != candles[j - 1].open_time + DAY_MS)):
            return False
    return True


def evaluate_donchian(
    symbol: str,
    daily: List[Candle],
    cfg: DonchianConfig,
    now_ms: int,
    holding: bool = False,
    universe: str = "",
) -> Evaluation:
    """Daily Donchian 55/20 evaluation.

    Entry:  just-closed daily close > highest high of the previous entry_period
            days (signal day excluded - no lookahead), executed within the entry
            grace window after 00:00 UTC.
    Exit:   held and just-closed daily close < lowest low of the previous
            exit_period days, re-evaluated every scan (no grace window, so a
            restart can still flatten the position).
    """
    closed = [c for c in daily if is_closed(c, now_ms)]
    price = closed[-1].close if closed else 0.0

    def reject(stage: str, reason: str, **extra) -> Evaluation:
        return Evaluation(symbol, price, None, stage, reason, **extra)

    min_bars = max(cfg.entry_period, cfg.exit_period, cfg.atr_period) + 1
    if len(closed) < min_bars:
        return reject("Data", f"Need {min_bars} completed daily candles; received {len(closed)}")

    signal_day = closed[-1]
    boundary = day_boundary_ms(now_ms)
    if signal_day.close_time != boundary - 1:
        actual = datetime.fromtimestamp(signal_day.close_time / 1000, tz=timezone.utc).isoformat()
        expected = datetime.fromtimestamp((boundary - 1) / 1000, tz=timezone.utc).isoformat()
        return reject("Data", f"Stale daily candles: latest close={actual}; expected={expected}")

    if not _valid_daily(closed):
        return reject("Data", "Invalid or non-consecutive daily candles")

    prior = closed[:-1]
    entry_channel = max(c.high for c in prior[-cfg.entry_period:])
    exit_channel = min(c.low for c in prior[-cfg.exit_period:])
    atr = wilders_atr(closed, cfg.atr_period)
    if atr is None or not (math.isfinite(atr) and atr > 0):
        return reject("Data", f"ATR({cfg.atr_period}) is unavailable")

    context = dict(entry_channel=entry_channel, exit_channel=exit_channel, atr=atr, holding=holding)
    closed_at = datetime.fromtimestamp(signal_day.close_time / 1000, tz=timezone.utc).isoformat()

    if holding:
        if signal_day.close < exit_channel:
            return Evaluation(symbol, price, Signal(
                symbol=symbol,
                signal_close_ms=signal_day.close_time,
                close_time_utc=closed_at,
                direction="EXIT",
                entry=signal_day.close,
                entry_channel=entry_channel,
                exit_channel=exit_channel,
                atr=atr,
                stop_distance=cfg.atr_multiplier * atr,
                universe=universe,
            ), **context)
        return reject(
            "Held",
            f"Close {signal_day.close:.12g} stayed at or above the {cfg.exit_period}-day low channel {exit_channel:.12g}",
            **context,
        )

    if signal_day.close > entry_channel:
        if not within_entry_grace(now_ms, cfg.entry_grace_minutes):
            return reject(
                "Entry",
                f"Breakout confirmed but the {cfg.entry_grace_minutes} min entry window after 00:00 UTC has passed",
                **context,
            )
        return Evaluation(symbol, price, Signal(
            symbol=symbol,
            signal_close_ms=signal_day.close_time,
            close_time_utc=closed_at,
            direction="LONG",
            entry=signal_day.close,
            entry_channel=entry_channel,
            exit_channel=exit_channel,
            atr=atr,
            stop_distance=cfg.atr_multiplier * atr,
            universe=universe,
        ), **context)
    return reject(
        "Entry",
        f"Close {signal_day.close:.12g} did not exceed the {cfg.entry_period}-day high channel {entry_channel:.12g}",
        **context,
    )
