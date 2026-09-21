from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass
class RuntimeSettings:
    top_n: int = 1000
    hourly_history_bars: int = 1000
    scan_workers: int = 8
    app_minimum_surge: float = 1.5
    indicator_volume_lookback: int = 168
    indicator_minimum_surge: float = 1.5
    minimum_close_position: float = 0.75
    swing_length: int = 50
    minimum_reward_risk: float = 2.0
    maximum_reward_risk: float = 4.0
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""

    def public_dict(self) -> dict:
        data = asdict(self)
        data["telegram_bot_token"] = ""
        data["telegram_token_configured"] = bool(self.telegram_bot_token)
        data["schedule"] = "Hourly at HH:00:10 UTC"
        return data


class SettingsStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._value = RuntimeSettings()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._value = self._validated(raw, self._value)
        except Exception as exc:
            raise RuntimeError(f"settings file is unreadable: {self.path}: {exc}") from exc

    @staticmethod
    def _number(raw: Dict[str, Any], key: str, old: Any, minimum: float, maximum: float, integer: bool = False):
        if key not in raw:
            return old
        value = int(raw[key]) if integer else float(raw[key])
        if value < minimum or value > maximum:
            raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
        return value

    def _validated(self, raw: Dict[str, Any], old: RuntimeSettings) -> RuntimeSettings:
        value = RuntimeSettings(**asdict(old))
        value.top_n = self._number(raw, "top_n", value.top_n, 1, 1000, True)
        value.hourly_history_bars = self._number(raw, "hourly_history_bars", value.hourly_history_bars, 240, 1000, True)
        value.scan_workers = self._number(raw, "scan_workers", value.scan_workers, 1, 50, True)
        value.app_minimum_surge = self._number(raw, "app_minimum_surge", value.app_minimum_surge, 0.1, 20.0)
        value.indicator_volume_lookback = self._number(raw, "indicator_volume_lookback", value.indicator_volume_lookback, 1, 500, True)
        value.indicator_minimum_surge = self._number(raw, "indicator_minimum_surge", value.indicator_minimum_surge, 0.1, 20.0)
        value.minimum_close_position = self._number(raw, "minimum_close_position", value.minimum_close_position, 0.0, 1.0)
        value.swing_length = self._number(raw, "swing_length", value.swing_length, 10, 200, True)
        value.minimum_reward_risk = self._number(raw, "minimum_reward_risk", value.minimum_reward_risk, 0.0, 20.0)
        value.maximum_reward_risk = self._number(raw, "maximum_reward_risk", value.maximum_reward_risk, 0.0, 20.0)
        if value.minimum_reward_risk > value.maximum_reward_risk:
            raise ValueError("minimum_reward_risk cannot exceed maximum_reward_risk")
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
            self._value = value
            return RuntimeSettings(**asdict(value))
