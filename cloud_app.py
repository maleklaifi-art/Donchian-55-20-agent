from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs

from database import TradeDatabase
from research_store import ResearchStore
from research_engine import ResearchEngine, config_for, fingerprint
from pattern_engine import PatternEngine
from explorer_engine import ExplorerEngine
from execution_accounting import position_cost
from demo_trader import (
    BotConfig,
    EventLogger,
    PublicMarketClient,
    TestnetExecutionClient,
    close_position,
    demo_balance_usdt,
    load_config,
    load_rules,
    load_state,
    save_state,
    place_signal,
    scan_market_detailed,
    sync_positions,
)
from runtime_settings import RuntimeSettings, SettingsStore
from strategy import DonchianConfig, Signal, is_current_signal_close


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def next_day_boundary(now_seconds: float) -> int:
    """Next exact 00:00 UTC boundary, without an unconditional settle delay."""
    return (int(now_seconds // 86_400) + 1) * 86_400


class DashboardLogger(EventLogger):
    def __init__(self, path: str, db: TradeDatabase):
        super().__init__(path)
        self.db = db
        self.research_store = ResearchStore(db)

    def emit(self, event: str, **payload):
        super().emit(event, **payload)
        try:
            self.db.record_event(event, payload)
            if event in {"VALID_SIGNAL", "EXIT_SIGNAL"}:
                self.db.upsert_signal(payload)
            elif event in {"TRADE_REJECTED", "EXIT_REJECTED"}:
                sk = payload.get("signal_key")
                if sk:
                    self.db.mark_signal(sk, "REJECTED", payload.get("reason"))
            elif event in {"POSITION_OPENED", "POSITION_UPDATED"}:
                self.db.open_trade(payload)
            elif event == "POSITION_CLOSED":
                self.db.close_trade(payload)
            elif event in {"ENTRY_FAILED", "EMERGENCY_FLATTEN", "FATAL_UNPROTECTED_POSITION", "FATAL_POSITION_STATE"}:
                sk = payload.get("signal_key")
                if sk:
                    self.db.mark_signal(sk, "ERROR", payload.get("reason") or event)
        except Exception as exc:
            print(json.dumps({"time": utc_now(), "event": "DASHBOARD_DB_ERROR", "error": str(exc)}), flush=True)
        try:
            self.research_store.execution_event(event, payload)
        except Exception as exc:
            print(json.dumps({"time": utc_now(), "event": "RESEARCH_AUDIT_ERROR", "error": str(exc)}), flush=True)


class CloudDemoEngine:
    def __init__(self, cfg: BotConfig, db: TradeDatabase, settings_store: SettingsStore):
        self.cfg = cfg
        self.db = db
        self.settings_store = settings_store
        self.logger = DashboardLogger(cfg.event_log, db)
        self.market = PublicMarketClient(cfg.request_timeout_s, Path(cfg.state_file).parent / "daily_klines")
        self.state_lock = threading.RLock()
        # Railway's ordinary container filesystem is replaced on redeploy.
        # A real mount is required before collecting new trade history there.
        self.history_persistent = os.path.ismount(Path(cfg.state_file).resolve().parent)
        self.storage_error = (
            "Persistent trade storage is not mounted. Attach a volume at DATA_DIR before enabling new entries."
            if os.getenv("RAILWAY_ENVIRONMENT_ID") and not self.history_persistent else None
        )
        self.meta_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.scan_wakeup = threading.Event()
        self.exec_client: Optional[TestnetExecutionClient] = None
        self.rules = {}
        self.credentials_configured = bool(os.getenv("BINANCE_TESTNET_API_KEY") and os.getenv("BINANCE_TESTNET_API_SECRET"))
        self.execution_ready = False
        self.last_execution_error: Optional[str] = None
        self.last_scan_started: Optional[str] = None
        self.last_scan_completed: Optional[str] = None
        self.last_scan_error: Optional[str] = None
        self.scan_in_progress = False
        self.last_position_sync: Optional[str] = None
        self.last_position_sync_error: Optional[str] = None
        self.last_price_refresh: Optional[str] = None
        self.live_prices: Dict[str, float] = {}
        self.testnet_free_usdt: Optional[float] = None
        self.last_scan_snapshot = {
            "duration_seconds": 0.0,
            "coins_scanned": 0,
            "valid_count": 0,
            "error_count": 0,
            "signals": [],
            "exit_signals": [],
            "filtered": [],
        }
        self.telegram_alerted_file = Path(cfg.signal_log).with_name("telegram_alerted.json")
        try:
            self.telegram_alerted = set(json.loads(self.telegram_alerted_file.read_text(encoding="utf-8")))
        except FileNotFoundError:
            self.telegram_alerted = set()
        except Exception:
            self.telegram_alerted = set()
        self.next_scan_at_ms = 0
        self.started_at = utc_now()
        self.strategy_config = self._apply_settings(settings_store.get())
        self.research_store = self.logger.research_store
        self.research = ResearchEngine(self.research_store, Path(cfg.state_file).parent / "daily_klines",
                                       settings_store, self._promote_research)
        self.patterns = None
        self.pattern_init_error = None
        try:
            self.patterns = PatternEngine(Path(cfg.state_file).parent / "patterns.sqlite3",
                Path(cfg.state_file).parent / "daily_klines", settings_store.get, lambda: self.scan_in_progress)
        except Exception as exc:
            self.pattern_init_error = str(exc)
            self.logger.emit("PATTERN_INIT_ERROR", error=str(exc))
        self.explorer = None
        self.explorer_init_error = None
        try:
            self.explorer = ExplorerEngine(Path(cfg.state_file).parent / "explorer.sqlite3",
                settings_store.get, lambda: self.scan_in_progress or self.research.busy or bool(self.patterns and self.patterns.busy))
        except Exception as exc:
            self.explorer_init_error = str(exc)
            self.logger.emit("EXPLORER_INIT_ERROR", error=str(exc))
        self._configure_execution()
        with self.state_lock:
            existing = load_state(self.cfg.state_file)
            changed = False
            for position in existing.get("open_positions", {}).values():
                if "exit_period" not in position:
                    position["exit_period"] = settings_store.legacy_exit_period
                    position.setdefault("strategy_version", "legacy_donchian")
                    changed = True
            if changed:
                # Only freeze bookkeeping; preserve every fill, quantity, pending
                # order and price. This path never calls an execution API.
                save_state(self.cfg.state_file, existing)
            self.db.backfill_state(existing)

    def _configure_execution(self):
        if not self.credentials_configured:
            self.execution_ready = False
            self.last_execution_error = "Binance Spot Testnet API credentials are not configured."
            return
        try:
            self.exec_client = TestnetExecutionClient(
                os.environ["BINANCE_TESTNET_API_KEY"],
                os.environ["BINANCE_TESTNET_API_SECRET"],
                self.cfg.request_timeout_s,
                self.cfg.recv_window_ms,
            )
            self.rules = load_rules(self.exec_client.exchange_info())
            account = self.exec_client.account()
            self.execution_ready = bool(account.get("canTrade", False))
            self.last_execution_error = None if self.execution_ready else "Testnet account reports canTrade=false."
        except Exception as exc:
            self.execution_ready = False
            self.last_execution_error = str(exc)
            self.logger.emit("EXECUTION_INIT_ERROR", error=str(exc))

    def start(self):
        self.research.start(self.stop_event)
        if self.patterns:
            self.patterns.start(self.stop_event)
        if self.explorer:
            self.explorer.start(self.stop_event)
        threading.Thread(target=self._monitor_loop, name="donchian-monitor", daemon=True).start()
        threading.Thread(target=self._scanner_loop, name="donchian-scanner", daemon=True).start()

    def set_new_entries_enabled(self, enabled: bool):
        kill = Path(self.cfg.kill_switch_file)
        kill.parent.mkdir(parents=True, exist_ok=True)
        if enabled:
            if self.storage_error:
                raise ValueError(self.storage_error)
            if kill.exists():
                kill.unlink()
            self.logger.emit("DASHBOARD_START_NEW_ENTRIES")
            self.scan_wakeup.set()
        else:
            kill.write_text(f"Paused from dashboard at {utc_now()}\n", encoding="utf-8")
            self.logger.emit("DASHBOARD_STOP_NEW_ENTRIES")

    def new_entries_enabled(self) -> bool:
        return not self.storage_error and not Path(self.cfg.kill_switch_file).exists()

    def _scanner_loop(self):
        # First run happens immediately: after a restart it must still flatten
        # positions whose daily exit signal fired while the process was down.
        while not self.stop_event.is_set():
            self._run_scan()
            now = time.time()
            next_day = next_day_boundary(now)
            with self.meta_lock:
                self.next_scan_at_ms = int(next_day * 1000)
            self.scan_wakeup.clear()
            self.scan_wakeup.wait(timeout=max(0.0, next_day - now))

    def _apply_settings(self, settings: RuntimeSettings) -> DonchianConfig:
        active = next(
            (u for u in settings.universes if u["name"].lower() == settings.active_universe.lower()),
            None,
        )
        self.cfg.universe_name = active["name"] if active else settings.active_universe
        self.cfg.universe_symbols = list(active["symbols"]) if active else []
        self.cfg.scan_workers = settings.scan_workers
        self.cfg.daily_history_bars = settings.daily_history_bars
        self.cfg.risk_per_trade_pct = settings.risk_per_trade_pct
        self.cfg.entry_grace_minutes = settings.entry_grace_minutes
        # Capital is a funding adjustment, never a reset of trade P&L.
        self.cfg.demo_equity_cap_usdt = settings.starting_capital_usdt
        return DonchianConfig(
            adx_period=settings.adx_period, adx_threshold=settings.adx_threshold,
            strategy_version=settings.strategy_version,
            entry_period=settings.entry_period,
            exit_period=settings.exit_period,
            atr_period=settings.atr_period,
            atr_multiplier=settings.atr_multiplier,
            risk_pct=settings.risk_per_trade_pct,
            daily_history_bars=settings.daily_history_bars,
            entry_grace_minutes=settings.entry_grace_minutes,
        )

    def _promote_research(self, cycle):
        # Candidate selection never receives an exchange client or settings writer.
        with self.state_lock:
            if self.scan_in_progress:
                return None  # Try later; do not mix versions within a scan.
            settings = self.settings_store.get()
            if not settings.research_enabled or not settings.research_auto_adopt:
                return None
            p = cycle["payload"]
            if config_for(settings) != p["base"] or settings.strategy_version != p["base_version"]:
                return False
            change = p["change"]
            if change["key"] not in {"entry_period", "adx_threshold"}:
                return False
            if p.get("forward", {}).get("gates") is None or not all(p["forward"]["gates"].values()):
                return False
            value = self.settings_store.update({change["key"]: change["after"],
                                                "strategy_version": p["candidate_version"]})
            self.strategy_config = self._apply_settings(value)
            self.logger.emit("RESEARCH_ADOPTED", cycle_id=cycle["id"], change=change,
                             strategy_version=value.strategy_version)
            return True

    def update_settings(self, raw: dict):
        # Serialize funding changes with order submission and exit accounting.
        with self.state_lock:
            raw = dict(raw)
            raw.pop("strategy_version", None)
            before = self.settings_store.get()
            value = self.settings_store.update(raw)
            if config_for(value) != config_for(before):
                value = self.settings_store.update({"strategy_version": "manual_"+fingerprint(config_for(value))[:12]})
            self.strategy_config = self._apply_settings(value)
        self.logger.emit("SETTINGS_UPDATED", settings=value.public_dict())
        return value

    def request_scan(self):
        if self.scan_in_progress:
            return False
        self.scan_wakeup.set()
        return True

    def _run_scan(self):
        with self.meta_lock:
            if self.scan_in_progress:
                return
            self.scan_in_progress = True
            self.last_scan_started = utc_now()
            self.last_scan_error = None

        try:
            settings = self.settings_store.get()
            self.strategy_config = self._apply_settings(settings)
            with self.state_lock:
                state = load_state(self.cfg.state_file)
            snapshot = scan_market_detailed(
                self.market,
                self.cfg,
                self.strategy_config,
                self.logger,
                open_symbols=set(state.get("open_positions", {})),
                position_rules=state.get("open_positions", {}),
            )
            with self.meta_lock:
                self.last_scan_snapshot = snapshot
                self.last_scan_completed = utc_now()
            try:
                self.research_store.record_decisions(snapshot, config_for(settings), self.execution_ready,
                                                     self.new_entries_enabled())
            except Exception as exc:
                # Optional research logging must never prevent a held trade's exit.
                self.logger.emit("RESEARCH_AUDIT_ERROR", error=str(exc))
            self._send_telegram_snapshot(settings, snapshot)
            if self.execution_ready and self.exec_client is not None:
                self._execute_snapshot(snapshot)
        except Exception as exc:
            with self.meta_lock:
                self.last_scan_error = str(exc)
            self.logger.emit("SCAN_FATAL_ERROR", error=str(exc))
        finally:
            with self.meta_lock:
                self.scan_in_progress = False
            self.research.wakeup.set()
            if self.patterns:
                self.patterns.wakeup.set()
            if self.explorer:
                self.explorer.wakeup.set()

    def _execute_snapshot(self, snapshot: dict):
        """Exits first (frees capital), then entries, all in one state batch."""
        with self.state_lock:
            state = load_state(self.cfg.state_file)
            self.db.backfill_state(state)
            for row in snapshot.get("exit_signals", []):
                if self.storage_error:
                    continue
                try:
                    close_position(Signal(**row), self.market, self.exec_client, self.rules, state, self.cfg, self.logger)
                except Exception as exc:
                    self.logger.emit("EXIT_ERROR", symbol=row.get("symbol"), signal_key=f"{row.get('symbol')}:{row.get('signal_close_ms')}", error=str(exc))
            # Exits can realize gains/losses and release cash in this same batch.
            realized = self.db.stats()["realized_pnl_usdt"]
            for row in snapshot.get("signals", []):
                sig = Signal(**row)
                if self.storage_error:
                    self.logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=f"{sig.symbol}:{sig.signal_close_ms}", reason=self.storage_error)
                    continue
                try:
                    place_signal(
                        sig, self.market, self.exec_client, self.rules, state, self.cfg, self.logger,
                        realized_pnl_usdt=realized,
                    )
                except Exception as exc:
                    self.logger.emit("ENTRY_ERROR", symbol=sig.symbol, signal_key=f"{sig.symbol}:{sig.signal_close_ms}", error=str(exc))

    def _send_telegram_snapshot(self, settings: RuntimeSettings, snapshot: dict):
        if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        now_ms = int(time.time() * 1000)
        alerts = [("DONCHIAN 55/20 BREAKOUT", s) for s in snapshot.get("signals", [])]
        alerts += [("DONCHIAN EXIT CHANNEL BREAK", s) for s in snapshot.get("exit_signals", [])]
        for headline, signal in alerts:
            if not is_current_signal_close(signal["signal_close_ms"], now_ms):
                continue
            signal_key = f"{signal['symbol']}:{signal['signal_close_ms']}"
            if signal_key in self.telegram_alerted:
                continue
            try:
                import requests
                message = (
                    f"{headline}\n{signal['symbol']}\n"
                    f"Daily close: {signal['entry']:.12g}\n"
                    f"55-day high: {signal['entry_channel']:.12g}\n"
                    f"20-day low: {signal['exit_channel']:.12g}\n"
                    f"ATR({settings.atr_period}): {signal['atr']:.12g}\n"
                    f"Sizing distance: {signal['stop_distance']:.12g} (2 x ATR)"
                )
                response = requests.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    data={"chat_id": settings.telegram_chat_id, "text": message},
                    timeout=10,
                )
                response.raise_for_status()
                self.telegram_alerted.add(signal_key)
                retained = sorted(self.telegram_alerted)[-1000:]
                self.telegram_alerted = set(retained)
                self.telegram_alerted_file.write_text(json.dumps(retained), encoding="utf-8")
            except Exception as exc:
                self.logger.emit("TELEGRAM_ERROR", error=str(exc), symbol=signal.get("symbol"))

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                self._sync_positions_and_prices()
                with self.meta_lock:
                    self.last_position_sync = utc_now()
                    self.last_position_sync_error = None
            except Exception as exc:
                with self.meta_lock:
                    self.last_position_sync_error = str(exc)
                self.logger.emit("MONITOR_ERROR", error=str(exc))
            self.stop_event.wait(15.0)

    def _sync_positions_and_prices(self):
        with self.state_lock:
            state = load_state(self.cfg.state_file)
            if self.execution_ready and self.exec_client is not None:
                sync_positions(self.exec_client, state, self.logger, self.cfg, self.market, self.rules)
                try:
                    account = self.exec_client.account()
                    self.testnet_free_usdt = next(
                        (float(b.get("free", 0) or 0) for b in account.get("balances", []) if b.get("asset") == self.cfg.quote_asset),
                        0.0,
                    )
                except Exception as exc:
                    self.logger.emit("ACCOUNT_REFRESH_ERROR", error=str(exc))
            open_positions = list(load_state(self.cfg.state_file).get("open_positions", {}).values())

        for pos in open_positions:
            sym, sk = pos.get("symbol"), pos.get("signal_key")
            if not sym or not sk:
                continue
            try:
                px = self.market.best_ask(sym)
                self.live_prices[sym] = px
                self.db.update_live_price(sk, px)
            except Exception as exc:
                self.logger.emit("PRICE_REFRESH_ERROR", symbol=sym, error=str(exc), signal_key=sk)
        with self.meta_lock:
            self.last_price_refresh = utc_now()

    def snapshot(self):
        with self.state_lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        stats = self.db.stats()
        open_trades = self.db.open_trades()
        unrealized = 0.0
        open_risk = 0.0
        open_value = 0.0
        open_cost = 0.0
        for t in open_trades:
            px = self.live_prices.get(t["symbol"]) or t.get("current_price") or t.get("entry_price") or 0.0
            entry = float(t.get("entry_price") or 0)
            qty = float(t.get("quantity") or 0)
            try:
                stored = json.loads(t.get("payload_json") or "{}")
            except Exception:
                stored = {}
            t["current_price"] = px
            cost = position_cost({**t, **stored})
            t["unrealized_pnl_usdt"] = px * qty - cost
            t["unrealized_pct"] = t["unrealized_pnl_usdt"] / cost * 100.0 if cost > 0 else 0.0
            # Donchian channels are carried in the trade payload, not columns.
            t["entry_channel"] = stored.get("entry_channel", t.get("entry_channel"))
            t["exit_channel"] = stored.get("exit_channel", t.get("exit_channel"))
            t["atr"] = stored.get("atr", t.get("atr"))
            t["strategy_version"] = stored.get("strategy_version", "legacy_donchian")
            t["exit_period"] = stored.get("exit_period", self.settings_store.legacy_exit_period)
            exit_channel = t.get("exit_channel")
            t["distance_to_exit_pct"] = ((px / float(exit_channel)) - 1.0) * 100.0 if px > 0 and exit_channel else None
            unrealized += t["unrealized_pnl_usdt"]
            open_risk += float(t.get("risk_usdt") or 0)
            open_value += px * qty
            open_cost += cost
        stats["unrealized_pnl_usdt"] = unrealized
        stats["total_pnl_usdt"] = stats["realized_pnl_usdt"] + unrealized
        stats["open_risk_usdt"] = open_risk
        raw_equity = (self.testnet_free_usdt or 0.0) + open_value if self.testnet_free_usdt is not None else None
        balance = demo_balance_usdt(self.cfg, {}, stats["realized_pnl_usdt"])
        stats["demo_balance_usdt"] = balance
        stats["estimated_demo_equity_usdt"] = balance + unrealized
        stats["available_demo_cash_usdt"] = max(0.0, balance - open_cost)
        stats["estimated_testnet_equity_usdt"] = raw_equity

        settings = self.settings_store.get()
        with self.meta_lock:
            meta = {
                "service_running": True,
                "started_at": self.started_at,
                "new_entries_enabled": self.new_entries_enabled(),
                "credentials_configured": self.credentials_configured,
                "execution_ready": self.execution_ready,
                "execution_error": self.last_execution_error,
                "scan_in_progress": self.scan_in_progress,
                "last_scan_started": self.last_scan_started,
                "last_scan_completed": self.last_scan_completed,
                "last_scan_error": self.last_scan_error,
                "last_position_sync": self.last_position_sync,
                "last_position_sync_error": self.last_position_sync_error,
                "last_price_refresh": self.last_price_refresh,
                "testnet_free_usdt": self.testnet_free_usdt,
                "history_persistent": self.history_persistent,
                "storage_error": self.storage_error,
                "next_scan_at_ms": self.next_scan_at_ms,
            }
        return {
            "meta": meta,
            "risk": {
                "risk_per_trade_pct": settings.risk_per_trade_pct,
                "sizing_rule": "risk % of equity / (2 x ATR)",
                # Keep the response key for existing clients; null means uncapped.
                "max_position_notional_usdt": None,
                "equity_basis": "funded balance plus open P&L, bounded by Testnet backing",
                "cash_reserve_pct": 0.0,
                "daily_loss_limit_r": None,
                "atr_history": "continuous from listing",
                "fee_accounting": "actual fees for new trades; legacy records unchanged",
                "demo_equity_cap_usdt": self.cfg.demo_equity_cap_usdt,
                "starting_capital_usdt": self.cfg.demo_equity_cap_usdt,
                "entry_period": settings.entry_period,
                "exit_period": settings.exit_period,
                "atr_period": settings.atr_period,
                "atr_multiplier": settings.atr_multiplier,
                "entry_grace_minutes": settings.entry_grace_minutes,
                "schedule": "Daily at 00:00 UTC",
            },
            "stats": stats,
            "open_trades": open_trades,
            "closed_trades": self.db.closed_trades(100),
            "rejected_signals": self.db.signals("REJECTED", 60),
            "entry_signals": self.last_scan_snapshot.get("signals", []),
            "exit_signals": self.last_scan_snapshot.get("exit_signals", []),
            "no_action": self.last_scan_snapshot.get("filtered", []),
            "recent_signals": self.db.signals(None, 60),
            "recent_events": self.db.recent_events(60),
            "scan": self.last_scan_snapshot,
            "settings": settings.public_dict(),
            "research": self.research.snapshot(),
            "patterns": self.patterns.snapshot() if self.patterns else {
                "enabled": False, "status": {"stage": "ERROR", "message": self.pattern_init_error}},
            "explorer": self.explorer.snapshot() if self.explorer else {
                "enabled": False, "status": {"stage": "ERROR", "message": self.explorer_init_error}},
        }

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = os.getenv("CONFIG_PATH", str(ROOT / "config.example.json"))
cfg = load_config(CONFIG_PATH)
cfg.state_file = str(DATA_DIR / "state.json")
cfg.event_log = str(DATA_DIR / "events.jsonl")
cfg.signal_log = str(DATA_DIR / "signals.json")
cfg.kill_switch_file = str(DATA_DIR / "STOP_TRADING")

db = TradeDatabase(str(DATA_DIR / "trades.sqlite3"))
settings_store = SettingsStore(str(DATA_DIR / "settings.json"), cfg.demo_equity_cap_usdt)
engine = CloudDemoEngine(cfg, db, settings_store)
if not as_bool(os.getenv("AUTO_START_NEW_TRADES"), True):
    Path(cfg.kill_switch_file).write_text("Safe startup: new entries paused. Use dashboard START.\n", encoding="utf-8")
else:
    Path(cfg.kill_switch_file).unlink(missing_ok=True)
if as_bool(os.getenv("START_ENGINE"), True):
    engine.start()

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD") or "demo-change-me"
INSECURE_DEFAULT_PASSWORD = DASHBOARD_PASSWORD == "demo-change-me"
COOKIE_SECURE = as_bool(os.getenv("COOKIE_SECURE"), False)
SESSION_TTL_S = int(os.getenv("SESSION_TTL_S", "43200"))
SESSIONS: Dict[str, dict] = {}
SESSIONS_LOCK = threading.RLock()

LOGIN_HTML = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


def make_session():
    sid = secrets.token_urlsafe(32)
    rec = {"csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_TTL_S}
    with SESSIONS_LOCK:
        SESSIONS[sid] = rec
    return sid, rec


def get_session(cookie_header: str):
    if not cookie_header:
        return None, None
    jar = cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return None, None
    morsel = jar.get("donchian_session")
    if not morsel:
        return None, None
    sid = morsel.value
    with SESSIONS_LOCK:
        rec = SESSIONS.get(sid)
        if not rec or rec["expires"] < time.time():
            SESSIONS.pop(sid, None)
            return None, None
        rec["expires"] = time.time() + SESSION_TTL_S
        return sid, rec


def render_login(error: str = ""):
    warning = ""
    if INSECURE_DEFAULT_PASSWORD:
        warning = '<div class="warn">Default dashboard password is active. Set <b>DASHBOARD_PASSWORD</b> in your cloud environment before exposing this service publicly.</div>'
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return LOGIN_HTML.replace("__INSECURE_WARNING__", warning).replace("__ERROR__", err)


def render_dashboard(csrf: str):
    warning = ""
    if INSECURE_DEFAULT_PASSWORD:
        warning = '<div class="warning">Security warning: change the default dashboard password by setting <b>DASHBOARD_PASSWORD</b> in the cloud environment.</div>'
    return DASHBOARD_HTML.replace("__CSRF_TOKEN_JSON__", json.dumps(csrf)).replace("__INSECURE_WARNING__", warning)


class Handler(BaseHTTPRequestHandler):
    server_version = "DonchianCloudDemo/1.0"

    def log_message(self, fmt, *args):
        print(json.dumps({"time": utc_now(), "event": "HTTP", "message": fmt % args}), flush=True)

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict):
        self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _redirect(self, location: str, extra_headers: Optional[dict] = None):
        headers = {"Location": location, **(extra_headers or {})}
        self._send(303, b"", headers=headers)

    def _session(self):
        return get_session(self.headers.get("Cookie", ""))

    def _read_form(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(n, 65536)).decode("utf-8", errors="replace")
        return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > 131072:
            raise ValueError("invalid JSON body size")
        parsed = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        sid, sess = self._session()
        if path == "/healthz":
            return self._json(200, {"ok": True, "mode": "BINANCE_SPOT_TESTNET_ONLY", "time": utc_now()})
        if path == "/login":
            if sess:
                return self._redirect("/")
            return self._send(200, render_login().encode("utf-8"))
        if path == "/":
            if not sess:
                return self._redirect("/login")
            return self._send(200, render_dashboard(sess["csrf"]).encode("utf-8"))
        if path == "/api/research/decisions":
            if not sess:
                return self._json(401, {"error": "unauthorized"})
            query = parse_qs(self.path.partition("?")[2])
            try:
                offset = max(0, int(query.get("offset", [0])[0]))
                symbol = str(query.get("symbol", [""])[0]).upper() or None
                return self._json(200, engine.research_store.decisions(200, offset, symbol,
                                   query.get("skipped", ["0"])[0] == "1"))
            except ValueError:
                return self._json(400, {"error": "Invalid offset"})
        if path == "/api/explorer/trial":
            if not sess:
                return self._json(401, {"error": "unauthorized"})
            if not engine.explorer:
                return self._json(503, {"error": "Explorer unavailable"})
            try:
                cid = int(parse_qs(self.path.partition("?")[2]).get("id", [0])[0])
                trial = engine.explorer.store.trial(cid)
                if trial:
                    trial["payload"].pop("tree", None)
                return self._json(200 if trial else 404, {"trial": trial})
            except ValueError:
                return self._json(400, {"error": "Invalid trial id"})
        if path == "/api/patterns/forecasts":
            if not sess:
                return self._json(401, {"error": "unauthorized"})
            if not engine.patterns:
                return self._json(503, {"error": "Pattern research unavailable"})
            try:
                offset = max(0, int(parse_qs(self.path.partition("?")[2]).get("offset", [0])[0]))
                return self._json(200, engine.patterns.store.forecasts(200, offset))
            except ValueError:
                return self._json(400, {"error": "Invalid offset"})
        if path == "/api/research/cycle":
            if not sess:
                return self._json(401, {"error": "unauthorized"})
            try:
                cid = int(parse_qs(self.path.partition("?")[2]).get("id", [0])[0])
                cycle = engine.research_store.cycle(cid)
                return self._json(200 if cycle else 404, {"cycle": cycle})
            except ValueError:
                return self._json(400, {"error": "Invalid cycle id"})
        if path == "/api/status":
            if not sess:
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, engine.snapshot())
        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        sid, sess = self._session()
        if path == "/login":
            form = self._read_form()
            if secrets.compare_digest(form.get("password", ""), DASHBOARD_PASSWORD):
                sid, rec = make_session()
                flags = [f"donchian_session={sid}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={SESSION_TTL_S}"]
                if COOKIE_SECURE:
                    flags.append("Secure")
                return self._redirect("/", {"Set-Cookie": "; ".join(flags)})
            return self._send(401, render_login("Incorrect password").encode("utf-8"))
        if path == "/logout":
            if sid:
                with SESSIONS_LOCK:
                    SESSIONS.pop(sid, None)
            return self._redirect("/login", {"Set-Cookie": "donchian_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if not sess:
            return self._json(401, {"error": "unauthorized"})
        csrf = self.headers.get("X-CSRF-Token", "")
        if not csrf or not secrets.compare_digest(csrf, sess["csrf"]):
            return self._json(403, {"error": "invalid csrf token"})
        if path == "/api/start":
            if engine.storage_error:
                return self._json(409, {"ok": False, "error": engine.storage_error})
            if not engine.execution_ready:
                return self._json(409, {"ok": False, "error": engine.last_execution_error or "Testnet execution is not ready"})
            engine.set_new_entries_enabled(True)
            return self._json(200, {"ok": True, "new_entries_enabled": True})
        if path == "/api/stop":
            engine.set_new_entries_enabled(False)
            return self._json(200, {"ok": True, "new_entries_enabled": False})
        if path == "/api/config":
            try:
                value = engine.update_settings(self._read_json())
                return self._json(200, {"ok": True, "settings": value.public_dict()})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
        return self._send(404, b"Not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(json.dumps({
        "event": "CLOUD_DASHBOARD_START",
        "port": port,
        "data_dir": str(DATA_DIR),
        "mode": "BINANCE_SPOT_TESTNET_ONLY",
        "strategy": "donchian_55_20_v1",
        "default_password_warning": INSECURE_DEFAULT_PASSWORD,
    }), flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_event.set()
        server.server_close()
