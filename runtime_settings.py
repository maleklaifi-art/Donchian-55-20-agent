from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass
class RuntimeSettings:
    settings_revision: int = 4
    starting_capital_usdt: float = 200.0
    top_n: int = 1000
    hourly_history_bars: int = 1000
    scan_workers: int = 100
    app_minimum_surge: float = 1.5
    indicator_volume_lookback: int = 168
    indicator_minimum_surge: float = 1.5
    minimum_close_position: float = 0.75
    swing_length: int = 50
    minimum_reward_risk: float = 1.0
    maximum_reward_risk: float = 4.0  # retained on disk only; never an active gate
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    supertrend_wilder: bool = True
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("settings_revision", None)
        data["telegram_bot_token"] = ""
        data["telegram_token_configured"] = bool(self.telegram_bot_token)
        data.pop("maximum_reward_risk", None)
        data.update(strategy_version="app_nr7_st_r2_box_weekday_stop256_v2", excluded_entry_days_utc=["Sunday", "Monday"], maximum_stop_distance_pct_exclusive=2.56)
        data["schedule"] = "Hourly at HH:00 UTC"
        return data


class SettingsStore:
    def __init__(self, path: str, starting_capital_usdt: float = 200.0):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._value = RuntimeSettings(starting_capital_usdt=starting_capital_usdt)
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            previous_revision = int(raw.get("settings_revision", 1))
            self._value = self._validated(raw, self._value)
            if previous_revision < 2 and self._value.scan_workers == 8:
                self._value.scan_workers = 50
            if previous_revision < 3 and self._value.scan_workers == 50:
                self._value.scan_workers = 100
            if previous_revision < 4:
                self._value.minimum_reward_risk = 1.0
            self._value.settings_revision = 4
            if previous_revision < 4:
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

    def _validated(self, raw: Dict[str, Any], old: RuntimeSettings) -> RuntimeSettings:
        value = RuntimeSettings(**asdict(old))
        if "starting_capital_usdt" in raw:
            capital = raw["starting_capital_usdt"]
            if isinstance(capital, bool):
                raise ValueError("starting_capital_usdt must be a positive finite amount")
            capital = float(capital)
            if not math.isfinite(capital) or capital <= 0:
                raise ValueError("starting_capital_usdt must be a positive finite amount")
            value.starting_capital_usdt = capital
        value.settings_revision = int(raw.get("settings_revision", value.settings_revision))
        value.top_n = self._number(raw, "top_n", value.top_n, 1, 1000, True)
        value.hourly_history_bars = self._number(raw, "hourly_history_bars", value.hourly_history_bars, 240, 1000, True)
        value.scan_workers = self._number(raw, "scan_workers", value.scan_workers, 1, 100, True)
        value.app_minimum_surge = self._number(raw, "app_minimum_surge", value.app_minimum_surge, 0.0, 20.0)
        value.indicator_volume_lookback = self._number(raw, "indicator_volume_lookback", value.indicator_volume_lookback, 1, 500, True)
        value.indicator_minimum_surge = self._number(raw, "indicator_minimum_surge", value.indicator_minimum_surge, 0.0, 20.0)
        value.minimum_close_position = self._number(raw, "minimum_close_position", value.minimum_close_position, 0.0, 1.0)
        value.swing_length = self._number(raw, "swing_length", value.swing_length, 10, 200, True)
        value.minimum_reward_risk = self._number(raw, "minimum_reward_risk", value.minimum_reward_risk, 0.0, 20.0)
        value.supertrend_period = self._number(raw, "supertrend_period", value.supertrend_period, 1, 200, True)
        value.supertrend_multiplier = self._number(raw, "supertrend_multiplier", value.supertrend_multiplier, 0.1, 20.0)
        if "supertrend_wilder" in raw:
            if not isinstance(raw["supertrend_wilder"], bool):
                raise ValueError("supertrend_wilder must be a boolean")
            value.supertrend_wilder = raw["supertrend_wilder"]
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

