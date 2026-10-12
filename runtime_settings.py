from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from strategy import DEFAULT_UNIVERSE_SYMBOLS


# Retain legacy values on disk for compatibility, but never expose or activate jobs.
REMOVED_JOB_SETTINGS = frozenset({
    "research_enabled", "research_auto_adopt", "pattern_enabled", "explorer_enabled",
    "research_review_days", "research_forward_days", "research_min_trades",
    "research_min_symbols", "research_min_notional",
})


def default_universes() -> List[Dict[str, Any]]:
    return [{"name": "Core 19", "symbols": list(DEFAULT_UNIVERSE_SYMBOLS)}]


@dataclass
class RuntimeSettings:
    settings_revision: int = 6
    adx_period: int = 14
    adx_threshold: float = 25.0
    strategy_version: str = "donchian_55_20_adx25_v1"
    legacy_position_exit_period: int = 20
    research_enabled: bool = True
    research_auto_adopt: bool = True
    pattern_enabled: bool = True
    explorer_enabled: bool = True
    research_review_days: int = 7
    research_forward_days: int = 90
    research_min_trades: int = 20
    research_min_symbols: int = 5
    research_fee_bps: float = 25.0
    research_slippage_bps: float = 5.0
    research_min_notional: float = 5.0
    starting_capital_usdt: float = 200.0
    risk_per_trade_pct: float = 1.0
    entry_period: int = 55
    exit_period: int = 20
    atr_period: int = 20
    atr_multiplier: float = 2.0
    daily_history_bars: int = 120
    entry_grace_minutes: int = 15
    scan_workers: int = 100
    universes: List[Dict[str, Any]] = field(default_factory=default_universes)
    active_universe: str = "Core 19"
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""

    def public_dict(self) -> dict:
        data = asdict(self)
        for key in REMOVED_JOB_SETTINGS:
            data.pop(key, None)
        data.pop("settings_revision", None)
        data.pop("legacy_position_exit_period", None)
        data["telegram_bot_token"] = ""
        data["telegram_token_configured"] = bool(self.telegram_bot_token)
        active = next(
            (u for u in self.universes if u["name"].lower() == self.active_universe.lower()),
            None,
        )
        data["active_universe_symbols"] = list(active["symbols"]) if active else []
        data["strategy_version"] = self.strategy_version
        data["schedule"] = "Daily at 00:00 UTC"
        data["sizing_rule"] = "risk % of equity / (2 x ATR)"
        return data


class SettingsStore:
    def __init__(self, path: str, starting_capital_usdt: float = 200.0):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._value = RuntimeSettings(starting_capital_usdt=starting_capital_usdt)
        self.legacy_exit_period = 20
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            previous_revision = int(raw.get("settings_revision", 1))
            self.legacy_exit_period = int(raw.get("legacy_position_exit_period", raw.get("exit_period", 20)))
            self._value.legacy_position_exit_period = self.legacy_exit_period
            if previous_revision < 5:
                # Pre-Donchian files predate the documented capital range; clamp
                # instead of refusing to boot, then rewrite at revision 5.
                try:
                    capital = float(raw.get("starting_capital_usdt", 200.0))
                    if math.isfinite(capital):
                        raw["starting_capital_usdt"] = min(max(capital, 10.0), 1_000_000.0)
                except (TypeError, ValueError):
                    pass
            if previous_revision < 6:
                # Explicit cutover to the user's agreed 55/20 + ADX25 baseline.
                raw.update(entry_period=55, exit_period=20, adx_period=14,
                           adx_threshold=25.0, strategy_version="donchian_55_20_adx25_v1")
                active_name = raw.get("active_universe", "Core 19")
                universes = raw.get("universes") or default_universes()
                for universe in universes:
                    if universe["name"].lower() == active_name.lower():
                        universe["symbols"] = list(DEFAULT_UNIVERSE_SYMBOLS)
                raw["universes"] = universes
            self._value = self._validated(raw, self._value)
            if previous_revision < 2 and self._value.scan_workers == 8:
                self._value.scan_workers = 50
            if previous_revision < 3 and self._value.scan_workers in (8, 50):
                self._value.scan_workers = 100
            self._value.settings_revision = 6
            if previous_revision < 6:
                self._write(self._value)
        except Exception as exc:
            raise RuntimeError(f"settings file is unreadable: {self.path}: {exc}") from exc

    @staticmethod
    def _number(raw: Dict[str, Any], key: str, old: Any, minimum: float, maximum: float, integer: bool = False):
        if key not in raw:
            return old
        value = int(raw[key]) if integer else float(raw[key])
        if not math.isfinite(value) or value < minimum or value > maximum:
            raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
        return value

    @staticmethod
    def _validated_universes(raw_universes: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_universes, list) or not raw_universes:
            raise ValueError("universes must be a non-empty list")
        seen = set()
        cleaned: List[Dict[str, Any]] = []
        for item in raw_universes:
            if not isinstance(item, dict):
                raise ValueError("each universe must be an object with name and symbols")
            name = str(item.get("name", "")).strip()
            if not name:
                raise ValueError("each universe needs a name")
            if name.lower() in seen:
                raise ValueError(f"duplicate universe name: {name}")
            seen.add(name.lower())
            symbols = item.get("symbols")
            if not isinstance(symbols, list) or not symbols:
                raise ValueError(f"universe {name} needs a non-empty symbols list")
            cleaned_symbols: List[str] = []
            for symbol in symbols:
                symbol = str(symbol).strip().upper()
                if not symbol.endswith("USDT"):
                    raise ValueError(f"universe symbols must end with USDT: {symbol}")
                if symbol not in cleaned_symbols:
                    cleaned_symbols.append(symbol)
            cleaned.append({"name": name, "symbols": cleaned_symbols})
        return cleaned

    def _validated(self, raw: Dict[str, Any], old: RuntimeSettings) -> RuntimeSettings:
        value = RuntimeSettings(**asdict(old))
        value.adx_period = self._number(raw, "adx_period", value.adx_period, 2, 100, True)
        value.adx_threshold = self._number(raw, "adx_threshold", value.adx_threshold, 0, 100)
        for key in ("research_enabled", "research_auto_adopt", "pattern_enabled", "explorer_enabled"):
            if key in raw:
                if not isinstance(raw[key], bool):
                    raise ValueError(f"{key} must be boolean")
                setattr(value, key, raw[key])
        for key, low, high, integer in (
            ("research_review_days", 1, 30, True), ("research_forward_days", 30, 730, True),
            ("research_min_trades", 10, 1000, True), ("research_min_symbols", 2, 19, True),
            ("research_fee_bps", 0, 200, False), ("research_slippage_bps", 0, 200, False),
            ("research_min_notional", 0, 1000, False)):
            setattr(value, key, self._number(raw, key, getattr(value, key), low, high, integer))
        # Only internal promotion code supplies this key; the HTTP handler removes it.
        if "strategy_version" in raw:
            value.strategy_version = str(raw["strategy_version"])

        value.starting_capital_usdt = self._number(
            raw, "starting_capital_usdt", value.starting_capital_usdt, 10, 1_000_000
        )
        value.risk_per_trade_pct = self._number(raw, "risk_per_trade_pct", value.risk_per_trade_pct, 0.01, 10)
        value.entry_period = self._number(raw, "entry_period", value.entry_period, 10, 500, True)
        value.exit_period = self._number(raw, "exit_period", value.exit_period, 5, 500, True)
        value.atr_period = self._number(raw, "atr_period", value.atr_period, 2, 200, True)
        value.atr_multiplier = self._number(raw, "atr_multiplier", value.atr_multiplier, 0.1, 20)
        value.daily_history_bars = self._number(raw, "daily_history_bars", value.daily_history_bars, 60, 1000, True)
        value.entry_grace_minutes = self._number(raw, "entry_grace_minutes", value.entry_grace_minutes, 1, 1440, True)
        value.scan_workers = self._number(raw, "scan_workers", value.scan_workers, 1, 100, True)
        if "universes" in raw:
            value.universes = self._validated_universes(raw["universes"])
        if "active_universe" in raw:
            value.active_universe = str(raw["active_universe"]).strip()
        if not value.active_universe:
            raise ValueError("active_universe must name one of the configured universes")
        names = {u["name"].lower() for u in value.universes}
        if value.active_universe.lower() not in names:
            known = ", ".join(u["name"] for u in value.universes)
            raise ValueError(f"active_universe must be one of: {known}")
        if "telegram_enabled" in raw:
            value.telegram_enabled = bool(raw["telegram_enabled"])
        if "telegram_chat_id" in raw:
            value.telegram_chat_id = str(raw["telegram_chat_id"]).strip()
        token = str(raw.get("telegram_bot_token", "")).strip()
        if token:
            value.telegram_bot_token = token
        if raw.get("clear_telegram_bot_token") is True:
            value.telegram_bot_token = ""
        return value

    def get(self) -> RuntimeSettings:
        with self._lock:
            return RuntimeSettings(**asdict(self._value))

    def update(self, raw: Dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            value = self._validated(raw, self._value)
            self._write(value)
            self._value = value
            return RuntimeSettings(**asdict(value))

    def _write(self, value: RuntimeSettings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(value), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
