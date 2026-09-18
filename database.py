from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class TradeDatabase:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self):
        with self._connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    event TEXT NOT NULL,
                    symbol TEXT,
                    signal_key TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC);
                CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);

                CREATE TABLE IF NOT EXISTS signals (
                    signal_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    signal_close_ms INTEGER,
                    close_time_utc TEXT,
                    entry REAL,
                    tp REAL,
                    sl REAL,
                    rr REAL,
                    box_high REAL,
                    box_low REAL,
                    app_volume_surge REAL,
                    indicator_volume_surge REAL,
                    close_position REAL,
                    confidence_score INTEGER,
                    confidence_level TEXT,
                    status TEXT NOT NULL DEFAULT 'VALID',
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_updated ON signals(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

                CREATE TABLE IF NOT EXISTS trades (
                    signal_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT,
                    entry_price REAL,
                    current_price REAL,
                    exit_price REAL,
                    quantity REAL,
                    tp REAL,
                    sl REAL,
                    rr REAL,
                    risk_usdt REAL,
                    pnl_usdt REAL,
                    pnl_pct REAL,
                    realized_r REAL,
                    opened_at TEXT,
                    closed_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at DESC);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _f(v, default=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def record_event(self, event: str, payload: Dict[str, Any], time_value: Optional[str] = None):
        t = time_value or self._now()
        symbol = payload.get("symbol")
        sk = payload.get("signal_key")
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO events(time,event,symbol,signal_key,payload_json) VALUES(?,?,?,?,?)",
                (t, event, symbol, sk, json.dumps(payload, default=str)),
            )

    def upsert_signal(self, payload: Dict[str, Any]):
        symbol = str(payload["symbol"])
        close_ms = int(payload["signal_close_ms"])
        sk = payload.get("signal_key") or f"{symbol}:{close_ms}"
        now = self._now()
        with self._lock, self._connect() as c:
            c.execute(
                """
                INSERT INTO signals(
                    signal_key,symbol,signal_close_ms,close_time_utc,entry,tp,sl,rr,box_high,box_low,
                    app_volume_surge,indicator_volume_surge,close_position,confidence_score,confidence_level,
                    status,reason,created_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signal_key) DO UPDATE SET
                    close_time_utc=excluded.close_time_utc, entry=excluded.entry, tp=excluded.tp, sl=excluded.sl,
                    rr=excluded.rr, box_high=excluded.box_high, box_low=excluded.box_low,
                    app_volume_surge=excluded.app_volume_surge,
                    indicator_volume_surge=excluded.indicator_volume_surge,
                    close_position=excluded.close_position, confidence_score=excluded.confidence_score,
                    confidence_level=excluded.confidence_level, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    sk, symbol, close_ms, payload.get("close_time_utc"), self._f(payload.get("entry")),
                    self._f(payload.get("tp")), self._f(payload.get("sl")), self._f(payload.get("rr")),
                    self._f(payload.get("box_high")), self._f(payload.get("box_low")),
                    self._f(payload.get("app_volume_surge")), self._f(payload.get("indicator_volume_surge")),
                    self._f(payload.get("close_position")), payload.get("confidence_score"),
                    payload.get("confidence_level"), "VALID", None, now, now,
                    json.dumps({**payload, "signal_key": sk}, default=str),
                ),
            )

    def mark_signal(self, signal_key: str, status: str, reason: Optional[str] = None):
        with self._lock, self._connect() as c:
            c.execute(
                "UPDATE signals SET status=?, reason=?, updated_at=? WHERE signal_key=?",
                (status, reason, self._now(), signal_key),
            )

    def open_trade(self, payload: Dict[str, Any]):
        sk = str(payload["signal_key"])
        with self._lock, self._connect() as c:
            c.execute(
                """
                INSERT INTO trades(
                    signal_key,symbol,status,outcome,entry_price,current_price,exit_price,quantity,tp,sl,rr,
                    risk_usdt,pnl_usdt,pnl_pct,realized_r,opened_at,closed_at,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signal_key) DO UPDATE SET
                    symbol=excluded.symbol,status='OPEN',entry_price=excluded.entry_price,current_price=excluded.current_price,
                    quantity=excluded.quantity,tp=excluded.tp,sl=excluded.sl,rr=excluded.rr,risk_usdt=excluded.risk_usdt,
                    opened_at=excluded.opened_at,payload_json=excluded.payload_json
                """,
                (
                    sk, payload.get("symbol"), "OPEN", None, self._f(payload.get("entry_price")),
                    self._f(payload.get("entry_price")), None, self._f(payload.get("quantity")), self._f(payload.get("tp")),
                    self._f(payload.get("sl")), self._f(payload.get("rr")), self._f(payload.get("risk_usdt")),
                    None, None, None, payload.get("opened_at"), None, json.dumps(payload, default=str),
                ),
            )
            c.execute("UPDATE signals SET status='TRADED', reason=NULL, updated_at=? WHERE signal_key=?", (self._now(), sk))

    def close_trade(self, payload: Dict[str, Any]):
        sk = str(payload["signal_key"])
        with self._lock, self._connect() as c:
            c.execute(
                """
                UPDATE trades SET
                    status='CLOSED', outcome=?, exit_price=?, quantity=COALESCE(?,quantity), pnl_usdt=?, pnl_pct=?,
                    realized_r=?, closed_at=?, payload_json=?
                WHERE signal_key=?
                """,
                (
                    payload.get("outcome"), self._f(payload.get("exit_price")), self._f(payload.get("quantity")),
                    self._f(payload.get("pnl_usdt")), self._f(payload.get("pnl_pct")),
                    self._f(payload.get("realized_r", payload.get("r"))), payload.get("closed_at"),
                    json.dumps(payload, default=str), sk,
                ),
            )
            c.execute("UPDATE signals SET status='CLOSED', updated_at=? WHERE signal_key=?", (self._now(), sk))

    def update_live_price(self, signal_key: str, current_price: float):
        with self._lock, self._connect() as c:
            c.execute("UPDATE trades SET current_price=? WHERE signal_key=? AND status='OPEN'", (float(current_price), signal_key))

    def backfill_state(self, state: Dict[str, Any]):
        for p in state.get("open_positions", {}).values():
            try:
                self.open_trade(p)
            except Exception:
                pass
        for r in state.get("realized", []):
            sk = r.get("signal_key")
            if not sk:
                continue
            with self._lock, self._connect() as c:
                exists = c.execute("SELECT 1 FROM trades WHERE signal_key=?", (sk,)).fetchone()
                if not exists:
                    c.execute(
                        """INSERT INTO trades(signal_key,symbol,status,outcome,entry_price,current_price,exit_price,quantity,tp,sl,rr,
                           risk_usdt,pnl_usdt,pnl_pct,realized_r,opened_at,closed_at,payload_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            sk, r.get("symbol"), "CLOSED", r.get("outcome"), self._f(r.get("entry_price")), None,
                            self._f(r.get("exit_price")), self._f(r.get("quantity")), self._f(r.get("tp")), self._f(r.get("sl")),
                            self._f(r.get("rr")), self._f(r.get("risk_usdt")), self._f(r.get("pnl_usdt")), self._f(r.get("pnl_pct")),
                            self._f(r.get("r")), r.get("opened_at"), r.get("closed_at"), json.dumps(r, default=str),
                        ),
                    )

    def open_trades(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT t.*, s.app_volume_surge,s.indicator_volume_surge,s.close_position,s.confidence_score,s.confidence_level,
                       s.entry AS signal_entry
                FROM trades t LEFT JOIN signals s ON s.signal_key=t.signal_key
                WHERE t.status='OPEN' ORDER BY t.opened_at DESC
                """
            ).fetchall()
        return [dict(x) for x in rows]

    def closed_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT t.*, s.app_volume_surge,s.indicator_volume_surge,s.close_position,s.confidence_score,s.confidence_level
                FROM trades t LEFT JOIN signals s ON s.signal_key=t.signal_key
                WHERE t.status='CLOSED' ORDER BY t.closed_at DESC LIMIT ?
                """, (int(limit),)
            ).fetchall()
        return [dict(x) for x in rows]

    def signals(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as c:
            if status:
                rows = c.execute("SELECT * FROM signals WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, int(limit))).fetchall()
            else:
                rows = c.execute("SELECT * FROM signals ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(x) for x in rows]

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def stats(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as c:
            closed = c.execute(
                "SELECT COUNT(*) n, SUM(CASE WHEN outcome='TP' THEN 1 ELSE 0 END) wins, SUM(CASE WHEN outcome='SL' THEN 1 ELSE 0 END) losses, COALESCE(SUM(pnl_usdt),0) pnl, COALESCE(SUM(realized_r),0) r FROM trades WHERE status='CLOSED'"
            ).fetchone()
            day = c.execute(
                "SELECT COALESCE(SUM(pnl_usdt),0) pnl, COALESCE(SUM(realized_r),0) r FROM trades WHERE status='CLOSED' AND substr(closed_at,1,10)=?",
                (today,),
            ).fetchone()
            open_row = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(risk_usdt),0) risk FROM trades WHERE status='OPEN'"
            ).fetchone()
        n = int(closed["n"] or 0)
        wins = int(closed["wins"] or 0)
        losses = int(closed["losses"] or 0)
        return {
            "closed_count": n,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / n * 100.0) if n else None,
            "realized_pnl_usdt": float(closed["pnl"] or 0),
            "realized_r": float(closed["r"] or 0),
            "today_pnl_usdt": float(day["pnl"] or 0),
            "today_r": float(day["r"] or 0),
            "open_count": int(open_row["n"] or 0),
            "open_risk_usdt": float(open_row["risk"] or 0),
        }
