"""Mature recorded decision outcomes; no training, strategy changes or order access."""
import hashlib
import json
import threading
import time
from pathlib import Path

from daily_history import validate
from strategy import Candle, DAY_MS


def config_for(settings):
    active = next(u for u in settings.universes if u['name'].lower()==settings.active_universe.lower())
    return {'rules': {k:getattr(settings,k) for k in ('entry_period','exit_period','atr_period',
                      'atr_multiplier','adx_period','adx_threshold')},
            'execution':{'adx_inclusive':True, 'adx_smoothing':settings.adx_smoothing,
                         'atr_stop_enabled':True, 'entry_fee_reserve_pct':settings.entry_fee_reserve_pct},
            'risk_pct':settings.risk_per_trade_pct, 'capital':settings.starting_capital_usdt,
            'symbols':list(active['symbols']),
            'cost':{'fee':settings.research_fee_bps/10000,'slip':settings.research_slippage_bps/10000,
                    'min_notional':settings.research_min_notional}}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def load_history(directory, symbols, now_ms):
    data={}; errors={}; target=(now_ms//DAY_MS)*DAY_MS-1
    for s in symbols:
        try:
            if not s.isalnum():
                raise ValueError('Invalid symbol')
            raw=json.loads((Path(directory)/(s+'.json')).read_text())
            if raw.get('version')!=1 or raw.get('symbol')!=s:
                raise ValueError('Invalid cache identity')
            bars=[Candle(**b) for b in raw['candles'] if b['close_time']<=target]
            validate(bars)
            if not bars or bars[-1].close_time!=target:
                raise ValueError('Latest completed daily candle is missing')
            data[s]=bars
        except (OSError,ValueError,KeyError,TypeError) as exc:
            errors[s]=str(exc)
    return data,errors


class DecisionTracker:
    def __init__(self, store, history_dir, get_settings, scan_busy=lambda: False):
        self.store = store
        self.history_dir = Path(history_dir)
        self.get_settings = get_settings
        self.scan_busy = scan_busy
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.status = {"notice": "Waiting for the daily scan to update decision outcomes."}
        self._last_signature = None

    def start(self, stop_event):
        def loop():
            while not stop_event.is_set():
                scan_completed = self.wakeup.wait(60)
                self.wakeup.clear()
                if stop_event.is_set():
                    break
                try:
                    self.run_once(force=scan_completed)
                except Exception as exc:
                    with self.lock:
                        self.status = {"notice": "Decision outcomes will retry: " + str(exc)}
        threading.Thread(target=loop, name="decision-outcomes", daemon=True).start()

    def run_once(self, now_ms=None, force=False):
        if self.scan_busy():
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.busy = True
            now_ms = int(time.time()*1000) if now_ms is None else now_ms
            symbols = sorted(set(config_for(self.get_settings())["symbols"]) |
                             set(self.store.decision_symbols()))
            files = []
            for symbol in symbols:
                if not symbol.isalnum():
                    continue
                try:
                    stat = (self.history_dir / (symbol + ".json")).stat()
                    files.append((symbol, stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    files.append((symbol, None, None))
            signature = (now_ms // DAY_MS, tuple(files), self.store.decisions(0)["total"])
            if not force and signature == self._last_signature:
                return
            data, errors = load_history(self.history_dir, symbols, now_ms)
            # Missing coins never block outcomes for the available coins.
            self.store.update_outcomes(data, now_ms)
            self.status = {
                "notice": "Recorded decisions and hypothetical outcomes only. Strategy settings do not change automatically.",
                "updated_ms": now_ms,
                "data": {"available": len(data), "required": len(symbols), "errors": errors},
            }
            self._last_signature = signature
        finally:
            self.busy = False
            self.lock.release()

    def snapshot(self):
        # Status is replaced atomically; never make order/status handling wait for
        # cache loading or SQLite outcome calculations under the worker lock.
        return {**self.status, "busy": self.busy, "decisions": self.store.decisions(200, 0)}
