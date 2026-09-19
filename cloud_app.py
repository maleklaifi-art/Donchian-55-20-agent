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
from demo_trader import (
    BotConfig,
    EventLogger,
    PublicMarketClient,
    StrategyConfig,
    TestnetExecutionClient,
    load_config,
    load_rules,
    load_state,
    place_signal,
    scan_market,
    sync_positions,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class DashboardLogger(EventLogger):
    def __init__(self, path: str, db: TradeDatabase):
        super().__init__(path)
        self.db = db

    def emit(self, event: str, **payload):
        super().emit(event, **payload)
        try:
            self.db.record_event(event, payload)
            if event in {"APP_CONFIRMED", "VALID_SIGNAL"}:
                self.db.upsert_signal(payload)
            elif event == "TRADE_REJECTED":
                sk = payload.get("signal_key")
                if sk:
                    self.db.mark_signal(sk, "REJECTED", payload.get("reason"))
            elif event == "POSITION_OPENED":
                self.db.open_trade(payload)
            elif event == "POSITION_CLOSED":
                self.db.close_trade(payload)
            elif event in {"ENTRY_FAILED", "EMERGENCY_FLATTEN", "FATAL_UNPROTECTED_POSITION", "FATAL_POSITION_STATE"}:
                sk = payload.get("signal_key")
                if sk:
                    self.db.mark_signal(sk, "ERROR", payload.get("reason") or event)
        except Exception as exc:
            print(json.dumps({"time": utc_now(), "event": "DASHBOARD_DB_ERROR", "error": str(exc)}), flush=True)


class CloudDemoEngine:
    def __init__(self, cfg: BotConfig, db: TradeDatabase):
        self.cfg = cfg
        self.db = db
        self.logger = DashboardLogger(cfg.event_log, db)
        self.scfg = StrategyConfig(
            app_minimum_surge=1.5,
            max_confirmed_age_minutes=cfg.max_confirmed_age_minutes,
            indicator_volume_lookback=168,
            indicator_minimum_surge=1.5,
            minimum_close_position=0.75,
            minimum_reward_risk=1.0,
            maximum_reward_risk=4.0,
            swing_length=50,
        )
        self.market = PublicMarketClient(cfg.request_timeout_s)
        self.state_lock = threading.RLock()
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
        self.latest_scan_rows = []
        self.started_at = utc_now()
        self._configure_execution()
        with self.state_lock:
            self.db.backfill_state(load_state(self.cfg.state_file))

    def editable_settings(self):
        return {
            "max_symbols": self.cfg.max_symbols,
            "scan_workers": self.cfg.scan_workers,
            "max_confirmed_age_minutes": self.cfg.max_confirmed_age_minutes,
            "app_minimum_surge": self.scfg.app_minimum_surge,
            "indicator_volume_lookback": self.scfg.indicator_volume_lookback,
            "indicator_minimum_surge": self.scfg.indicator_minimum_surge,
            "minimum_close_position": self.scfg.minimum_close_position,
            "minimum_reward_risk": self.scfg.minimum_reward_risk,
            "maximum_reward_risk": self.scfg.maximum_reward_risk,
            "swing_length": self.scfg.swing_length,
            "demo_equity_cap_usdt": self.cfg.demo_equity_cap_usdt,
            "max_risk_per_trade_usdt": self.cfg.max_risk_per_trade_usdt,
            "max_open_positions": self.cfg.max_open_positions,
            "daily_loss_limit_r": self.cfg.daily_loss_limit_r,
            "max_entry_drift_pct": self.cfg.max_entry_drift_pct,
            "schedule": "HOURLY_AT_00_UTC",
        }

    def apply_settings(self, raw: dict):
        specs = {
            "max_symbols": (int, 1, 1000),
            "scan_workers": (int, 1, 50),
            "max_confirmed_age_minutes": (int, 0, 59),
            "app_minimum_surge": (float, 0.1, 100.0),
            "indicator_volume_lookback": (int, 1, 500),
            "indicator_minimum_surge": (float, 0.1, 100.0),
            "minimum_close_position": (float, 0.0, 1.0),
            "minimum_reward_risk": (float, 0.0, 20.0),
            "maximum_reward_risk": (float, 0.1, 20.0),
            "swing_length": (int, 10, 500),
            "demo_equity_cap_usdt": (float, 10.0, 1000000.0),
            "max_risk_per_trade_usdt": (float, 0.01, 100000.0),
            "max_open_positions": (int, 1, 20),
            "daily_loss_limit_r": (float, 0.1, 100.0),
            "max_entry_drift_pct": (float, 0.0, 10.0),
        }
        parsed = {}
        for key, (typ, lo, hi) in specs.items():
            if key not in raw:
                continue
            try:
                value = typ(raw[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a valid number")
            if value < lo or value > hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            parsed[key] = value

        min_rr = parsed.get("minimum_reward_risk", self.scfg.minimum_reward_risk)
        max_rr = parsed.get("maximum_reward_risk", self.scfg.maximum_reward_risk)
        if max_rr <= min_rr:
            raise ValueError("maximum_reward_risk must be greater than minimum_reward_risk")

        with self.meta_lock:
            for key in ("max_symbols","scan_workers","max_confirmed_age_minutes","demo_equity_cap_usdt",
                        "max_risk_per_trade_usdt","max_open_positions","daily_loss_limit_r","max_entry_drift_pct"):
                if key in parsed:
                    setattr(self.cfg, key, parsed[key])
            for key in ("app_minimum_surge","indicator_volume_lookback","indicator_minimum_surge",
                        "minimum_close_position","minimum_reward_risk","maximum_reward_risk","swing_length"):
                if key in parsed:
                    setattr(self.scfg, key, parsed[key])

            settings = self.editable_settings()
            SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

        self.logger.emit("SETTINGS_UPDATED", settings=settings)
        return settings

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
        threading.Thread(target=self._monitor_loop, name="nr7-monitor", daemon=True).start()
        threading.Thread(target=self._scanner_loop, name="nr7-scanner", daemon=True).start()

    def set_new_entries_enabled(self, enabled: bool):
        kill = Path(self.cfg.kill_switch_file)
        kill.parent.mkdir(parents=True, exist_ok=True)
        if enabled:
            if kill.exists():
                kill.unlink()
            self.logger.emit("DASHBOARD_START_NEW_ENTRIES")
            self.scan_wakeup.set()
        else:
            kill.write_text(f"Paused from dashboard at {utc_now()}\n", encoding="utf-8")
            self.logger.emit("DASHBOARD_STOP_NEW_ENTRIES")

    def new_entries_enabled(self) -> bool:
        return not Path(self.cfg.kill_switch_file).exists()

    def _scanner_loop(self):
        # Scan exactly once per hour, aligned to the top of the UTC hour.
        # Example: 10:00, 11:00, 12:00 ... regardless of when the service started.
        while not self.stop_event.is_set():
            now = time.time()
            next_hour = (int(now // 3600) + 1) * 3600
            self.scan_wakeup.clear()
            woke_early = self.scan_wakeup.wait(timeout=max(1.0, next_hour - now))
            if self.stop_event.is_set():
                break
            if woke_early:
                # Manual START can wake the loop, but exact scheduling still waits for :00.
                continue
            self._run_scan()

    def _run_scan(self):
        with self.meta_lock:
            if self.scan_in_progress:
                return
            self.scan_in_progress = True
            self.last_scan_started = utc_now()
            self.last_scan_error = None

        def on_signal(sig):
            sk = f"{sig.symbol}:{sig.signal_close_ms}"
            if not self.execution_ready or self.exec_client is None:
                self.logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=sk, reason="testnet execution unavailable")
                return
            with self.state_lock:
                state = load_state(self.cfg.state_file)
                place_signal(sig, self.market, self.exec_client, self.rules, state, self.cfg, self.logger)

        def on_scan_rows(rows):
            with self.meta_lock:
                self.latest_scan_rows = rows

        try:
            scan_market(self.market, self.cfg, self.scfg, self.logger, on_signal=on_signal, on_scan_rows=on_scan_rows)
            with self.meta_lock:
                self.last_scan_completed = utc_now()
        except Exception as exc:
            with self.meta_lock:
                self.last_scan_error = str(exc)
            self.logger.emit("SCAN_FATAL_ERROR", error=str(exc))
        finally:
            with self.meta_lock:
                self.scan_in_progress = False

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
                sync_positions(self.exec_client, state, self.logger, self.cfg)
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
        stats = self.db.stats()
        open_trades = self.db.open_trades()
        unrealized = 0.0
        open_risk = 0.0
        open_value = 0.0
        for t in open_trades:
            px = self.live_prices.get(t["symbol"]) or t.get("current_price") or t.get("entry_price") or 0.0
            entry = float(t.get("entry_price") or 0)
            qty = float(t.get("quantity") or 0)
            t["current_price"] = px
            t["unrealized_pnl_usdt"] = (px - entry) * qty
            t["unrealized_pct"] = ((px / entry) - 1.0) * 100.0 if entry > 0 and px > 0 else 0.0
            t["distance_to_tp_pct"] = ((float(t.get("tp") or 0) / px) - 1.0) * 100.0 if px > 0 and t.get("tp") else None
            t["distance_to_sl_pct"] = ((float(t.get("sl") or 0) / px) - 1.0) * 100.0 if px > 0 and t.get("sl") else None
            unrealized += t["unrealized_pnl_usdt"]
            open_risk += float(t.get("risk_usdt") or 0)
            open_value += px * qty
        stats["unrealized_pnl_usdt"] = unrealized
        stats["total_pnl_usdt"] = stats["realized_pnl_usdt"] + unrealized
        stats["open_risk_usdt"] = open_risk
        stats["estimated_demo_equity_usdt"] = (
            min((self.testnet_free_usdt or 0.0) + open_value, self.cfg.demo_equity_cap_usdt)
            if self.testnet_free_usdt is not None else None
        )

        with self.meta_lock:
            latest_rows = list(self.latest_scan_rows)
            scan_counts = {
                "scanned": len(latest_rows),
                "filtered": sum(1 for x in latest_rows if x.get("status") == "FILTERED"),
                "breaking_out": sum(1 for x in latest_rows if x.get("status") == "BREAKING_OUT"),
                "app_confirmed": sum(1 for x in latest_rows if x.get("status") == "CONFIRMED"),
                "indicator_passed": sum(1 for x in latest_rows if x.get("status") == "CONFIRMED" and x.get("indicator_pass") is True),
                "indicator_rejected": sum(1 for x in latest_rows if x.get("status") == "CONFIRMED" and x.get("indicator_pass") is False),
            }
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
            }
        return {
            "meta": meta,
            "scan_counts": scan_counts,
            "risk": {
                "risk_per_trade_pct": self.cfg.risk_per_trade_pct,
                "max_risk_per_trade_usdt": self.cfg.max_risk_per_trade_usdt,
                "max_open_positions": self.cfg.max_open_positions,
                "max_total_open_risk_pct": self.cfg.max_total_open_risk_pct,
                "daily_loss_limit_r": self.cfg.daily_loss_limit_r,
                "max_entry_drift_pct": self.cfg.max_entry_drift_pct,
                "demo_equity_cap_usdt": self.cfg.demo_equity_cap_usdt,
                "scan_interval_seconds": self.cfg.scan_interval_seconds,
                "max_confirmed_age_minutes": self.cfg.max_confirmed_age_minutes,
            },
            "stats": stats,
            "open_trades": open_trades,
            "closed_trades": self.db.closed_trades(100),
            "rejected_signals": self.db.signals("REJECTED", 60),
            "recent_signals": self.db.signals(None, 60),
            "recent_events": self.db.recent_events(60),
            "settings": self.editable_settings(),
            "filtered_latest": [x for x in self.latest_scan_rows if x.get("status") == "FILTERED"],
            "breaking_out_latest": [x for x in self.latest_scan_rows if x.get("status") == "BREAKING_OUT"],
            "confirmed_latest": [x for x in self.latest_scan_rows if x.get("status") == "CONFIRMED"],
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
SETTINGS_PATH = DATA_DIR / "dashboard_settings.json"
if SETTINGS_PATH.exists():
    try:
        saved_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        for key in ("max_symbols","scan_workers","max_confirmed_age_minutes","demo_equity_cap_usdt",
                    "max_risk_per_trade_usdt","max_open_positions","daily_loss_limit_r","max_entry_drift_pct"):
            if key in saved_settings and hasattr(cfg, key):
                setattr(cfg, key, saved_settings[key])
    except Exception as exc:
        print(json.dumps({"event":"SETTINGS_LOAD_ERROR","error":str(exc)}), flush=True)

db = TradeDatabase(str(DATA_DIR / "trades.sqlite3"))
engine = CloudDemoEngine(cfg, db)
if SETTINGS_PATH.exists():
    try:
        saved_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        for key in ("app_minimum_surge","indicator_volume_lookback","indicator_minimum_surge",
                    "minimum_close_position","minimum_reward_risk","maximum_reward_risk","swing_length"):
            if key in saved_settings and hasattr(engine.scfg, key):
                setattr(engine.scfg, key, saved_settings[key])
    except Exception:
        pass
if not as_bool(os.getenv("AUTO_START_NEW_TRADES"), False):
    Path(cfg.kill_switch_file).write_text("Safe startup: new entries paused. Use dashboard START.\n", encoding="utf-8")
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
    morsel = jar.get("nr7_session")
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
    server_version = "NR7CloudDemo/1.0"

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
        raw = self.rfile.read(min(n, 65536)).decode("utf-8", errors="replace")
        return json.loads(raw or "{}")

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
                flags = [f"nr7_session={sid}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={SESSION_TTL_S}"]
                if COOKIE_SECURE:
                    flags.append("Secure")
                return self._redirect("/", {"Set-Cookie": "; ".join(flags)})
            return self._send(401, render_login("Incorrect password").encode("utf-8"))
        if path == "/logout":
            if sid:
                with SESSIONS_LOCK:
                    SESSIONS.pop(sid, None)
            return self._redirect("/login", {"Set-Cookie": "nr7_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if not sess:
            return self._json(401, {"error": "unauthorized"})
        csrf = self.headers.get("X-CSRF-Token", "")
        if not csrf or not secrets.compare_digest(csrf, sess["csrf"]):
            return self._json(403, {"error": "invalid csrf token"})
        if path == "/api/start":
            if not engine.execution_ready:
                return self._json(409, {"ok": False, "error": engine.last_execution_error or "Testnet execution is not ready"})
            engine.set_new_entries_enabled(True)
            return self._json(200, {"ok": True, "new_entries_enabled": True})
        if path == "/api/stop":
            engine.set_new_entries_enabled(False)
            return self._json(200, {"ok": True, "new_entries_enabled": False})
        if path == "/api/settings":
            try:
                out = engine.apply_settings(self._read_json())
                return self._json(200, {"ok": True, "settings": out})
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(500, {"ok": False, "error": str(exc)})
        return self._send(404, b"Not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(json.dumps({
        "event": "CLOUD_DASHBOARD_START",
        "port": port,
        "data_dir": str(DATA_DIR),
        "mode": "BINANCE_SPOT_TESTNET_ONLY",
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
