#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from strategy import (
    DEFAULT_UNIVERSE_SYMBOLS,
    Candle,
    DonchianConfig,
    Evaluation,
    Signal,
    evaluate_donchian,
    is_current_signal_close,
    within_entry_grace,
)

PRODUCTION_MARKET_BASE = "https://api.binance.com"
TESTNET_EXECUTION_BASE = "https://testnet.binance.vision"
# User-specified stablecoin/fiat pairs. Exact matches avoid excluding unrelated coins.
EXCLUDED_SYMBOLS = frozenset({
    "BFUSDUSDT", "EURIUSDT", "EURUSDT", "USD1USDT", "USDEUSDT", "XUSDUSDT",
    "FDUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDPUSDT", "AEURUSDT", "RLUSDUSDT", "UUSDT",
})


@dataclass
class BotConfig:
    scan_workers: int = 100
    daily_history_bars: int = 120
    quote_asset: str = "USDT"

    # Active universe (dashboard-editable named universes).
    universe_name: str = "Core 10"
    universe_symbols: List[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE_SYMBOLS))

    # Per-trade risk: risk_per_trade_pct of equity, sized via 2 x ATR(20).
    risk_per_trade_pct: float = 1.0
    daily_loss_limit_r: float = 3.0
    demo_equity_cap_usdt: float = 200.0  # funded capital; balance also includes realized P&L
    entry_grace_minutes: int = 15

    # API / runtime.
    request_timeout_s: int = 15
    recv_window_ms: int = 5000
    state_file: str = "state.json"
    event_log: str = "events.jsonl"
    signal_log: str = "signals.json"
    kill_switch_file: str = "STOP_TRADING"


@dataclass
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    quote_order_qty_market_allowed: bool


@dataclass
class Position:
    symbol: str
    signal_key: str
    entry_order_id: int
    entry_client_order_id: str
    entry_price: float
    quantity: float
    risk_usdt: float
    opened_at: str
    # Donchian bookkeeping (new positions).
    atr: Optional[float] = None
    exit_channel: Optional[float] = None
    entry_channel: Optional[float] = None
    # Legacy OCO bookkeeping (positions opened before the Donchian cutover).
    tp: Optional[float] = None
    sl: Optional[float] = None
    rr: Optional[float] = None
    oco_order_list_id: int = 0
    oco_list_client_order_id: str = ""
    tp_order_id: int = 0
    sl_order_id: int = 0


class EventLogger:
    def __init__(self, path: str):
        self.path = Path(path)

    def emit(self, event: str, **payload):
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        print(json.dumps(row, separators=(",", ":"), default=str), flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")


class PublicMarketClient:
    def __init__(self, timeout_s: int):
        self.base = PRODUCTION_MARKET_BASE
        self.timeout_s = timeout_s
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "donchian-55-20-market/1.0"})
        # Keep enough reusable connections for the scanner's parallel workers.
        adapter = HTTPAdapter(pool_connections=200, pool_maxsize=200, max_retries=0)
        self.s.mount("https://", adapter)

    def get(self, path: str, params: Optional[dict] = None):
        last_error = None
        for attempt in range(5):
            try:
                r = self.s.get(self.base + path, params=params, timeout=self.timeout_s)
                if r.status_code in (418, 429):
                    retry_after = float(r.headers.get("Retry-After", "2"))
                    time.sleep(max(1.0, retry_after) * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                last_error = exc
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"public request failed {path}: {last_error}")

    def resolve_universe(self, requested: List[str], quote_asset: str) -> List[str]:
        """Validate the configured universe against live exchangeInfo.

        Order is preserved. Never falls back to ticker ranking; symbols that are
        missing, halted or not spot-tradable are simply dropped from the scan.
        """
        info = self.get("/api/v3/exchangeInfo")
        eligible = {
            item["symbol"] for item in info["symbols"]
            if item.get("status") == "TRADING"
            and item.get("isSpotTradingAllowed") is True
            and item.get("quoteAsset") == quote_asset.upper()
        }
        return [
            symbol for symbol in requested
            if symbol not in EXCLUDED_SYMBOLS and symbol in eligible
        ]

    def klines(self, symbol: str, interval: str, limit: int = 1000) -> List[Candle]:
        rows = self.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        return [
            Candle(
                open_time=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]), close_time=int(r[6]), quote_volume=float(r[7]),
            )
            for r in rows
        ]

    def aggregate_trades(self, symbol: str, *, start_ms=None, from_id=None):
        # Only used to reconcile legacy OCO positions.
        params = {"symbol": symbol, "limit": 1000}
        if from_id is not None:
            params["fromId"] = from_id
        else:
            params["startTime"] = start_ms
        return self.get("/api/v3/aggTrades", params)

    def best_ask(self, symbol: str) -> float:
        x = self.get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(x["askPrice"])


class TestnetExecutionClient:
    """Binance Spot Testnet only. No production execution URL exists in this class."""
    def __init__(self, api_key: str, api_secret: str, timeout_s: int, recv_window_ms: int):
        if not api_key or not api_secret:
            raise ValueError("BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are required for demo execution")
        self.base = TESTNET_EXECUTION_BASE
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.timeout_s = timeout_s
        self.recv_window_ms = recv_window_ms
        self.s = requests.Session()
        self.s.headers.update({"X-MBX-APIKEY": api_key, "User-Agent": "donchian-55-20-exec/1.0"})
        self.time_offset_ms = 0
        self.sync_time()

    def _public(self, path: str, params: Optional[dict] = None):
        r = self.s.get(self.base + path, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def sync_time(self):
        server = int(self._public("/api/v3/time")["serverTime"])
        self.time_offset_ms = server - int(time.time() * 1000)

    def _signed(self, method: str, path: str, params: Optional[dict] = None):
        p = dict(params or {})
        p["recvWindow"] = self.recv_window_ms
        p["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        # Binance's current docs require percent-encoding before the HMAC is computed.
        payload = urlencode(p, doseq=True)
        signature = hmac.new(self.api_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        query = payload + "&signature=" + signature
        url = self.base + path + "?" + query
        r = self.s.request(method, url, timeout=self.timeout_s)
        if r.status_code >= 400:
            raise RuntimeError(f"Binance testnet {method} {path} failed {r.status_code}: {r.text}")
        return r.json()

    def account(self):
        return self._signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})

    def open_orders(self):
        return self._signed("GET", "/api/v3/openOrders")

    def exchange_info(self):
        return self._public("/api/v3/exchangeInfo")

    def book_ticker(self, symbol: str):
        return self._public("/api/v3/ticker/bookTicker", {"symbol": symbol})

    def order(self, symbol: str, order_id: Optional[int] = None, orig_client_order_id: Optional[str] = None):
        p: Dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = order_id
        elif orig_client_order_id:
            p["origClientOrderId"] = orig_client_order_id
        else:
            raise ValueError("order id required")
        return self._signed("GET", "/api/v3/order", p)

    def order_list(self, order_list_id: Optional[int] = None, orig_client_order_id: Optional[str] = None):
        p: Dict[str, Any] = {}
        if order_list_id is not None:
            p["orderListId"] = order_list_id
        elif orig_client_order_id:
            p["origClientOrderId"] = orig_client_order_id
        else:
            raise ValueError("order list id required")
        return self._signed("GET", "/api/v3/orderList", p)

    def cancel_order_list(self, symbol: str, order_list_id: int):
        return self._signed("DELETE", "/api/v3/orderList", {"symbol": symbol, "orderListId": order_list_id})

    def market_buy_quote(self, symbol: str, quote_qty: str, client_order_id: str):
        p = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": quote_qty,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        try:
            return self._signed("POST", "/api/v3/order", p)
        except Exception:
            # Unknown-status safety: query deterministic clientOrderId before deciding it failed.
            try:
                return self.order(symbol, orig_client_order_id=client_order_id)
            except Exception:
                raise

    def market_sell_qty(self, symbol: str, quantity: str, client_order_id: str):
        p = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        try:
            return self._signed("POST", "/api/v3/order", p)
        except Exception:
            try:
                return self.order(symbol, orig_client_order_id=client_order_id)
            except Exception:
                raise


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def floor_step(value: float, step: Decimal) -> Decimal:
    v = D(value)
    if step <= 0:
        return v
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def floor_tick(value: float, tick: Decimal) -> Decimal:
    return floor_step(value, tick)


def ceil_tick(value: float, tick: Decimal) -> Decimal:
    v = D(value)
    if tick <= 0:
        return v
    return (v / tick).to_integral_value(rounding=ROUND_UP) * tick


def nearest_tick(value: float, tick: Decimal) -> Decimal:
    v = D(value)
    if tick <= 0:
        return v
    return (v / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def fmt_dec(x: Decimal) -> str:
    s = format(x, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def load_rules(info: dict) -> Dict[str, SymbolRules]:
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        fmap = {f.get("filterType"): f for f in s.get("filters", [])}
        pf = fmap.get("PRICE_FILTER", {})
        lot = fmap.get("LOT_SIZE", {})
        notional = fmap.get("NOTIONAL") or fmap.get("MIN_NOTIONAL") or {}
        min_notional = notional.get("minNotional", "0")
        out[s["symbol"]] = SymbolRules(
            symbol=s["symbol"],
            base_asset=s.get("baseAsset", ""),
            quote_asset=s.get("quoteAsset", ""),
            tick_size=D(pf.get("tickSize", "0")),
            step_size=D(lot.get("stepSize", "0")),
            min_qty=D(lot.get("minQty", "0")),
            max_qty=D(lot.get("maxQty", "999999999")),
            min_notional=D(min_notional),
            quote_order_qty_market_allowed=bool(s.get("quoteOrderQtyMarketAllowed", True)),
        )
    return out


def load_config(path: str) -> BotConfig:
    cfg = BotConfig()
    p = Path(path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def initial_state() -> dict:
    return {"seen_signals": [], "executed_entries": [], "open_positions": {}, "realized": []}


def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return initial_state()
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        st.setdefault("seen_signals", [])
        st.setdefault("executed_entries", [])
        st.setdefault("open_positions", {})
        st.setdefault("realized", [])
        return st
    except Exception:
        raise RuntimeError(f"state file is unreadable: {path}; refusing to trade until repaired")


def save_state(path: str, state: dict):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent or Path(".")))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def daily_realized_r(state: dict) -> float:
    day = today_utc()
    return sum(float(x.get("r", 0.0)) for x in state.get("realized", []) if x.get("day") == day)


def trades_opened_today(state: dict) -> int:
    day = today_utc()
    executed = state.get("executed_entries", [])
    if executed:
        # This ledger is written immediately after any filled market entry, so
        # an emergency-flattened entry remains counted in execution history.
        return len({
            str(row.get("signal_key", ""))
            for row in executed
            if str(row.get("opened_at", ""))[:10] == day and row.get("signal_key")
        })

    # Backward compatibility for state files created before the execution
    # ledger existed. Avoid double-counting a trade as both open and realized.
    keys = set()
    opened = 0
    for pos in state.get("open_positions", {}).values():
        if str(pos.get("opened_at", ""))[:10] == day:
            key = str(pos.get("signal_key", ""))
            keys.add(key or f"open:{opened}")
            opened += 1
    for trade in state.get("realized", []):
        if str(trade.get("opened_at", ""))[:10] == day:
            key = str(trade.get("signal_key", ""))
            keys.add(key or f"realized:{opened}")
            opened += 1
    return len(keys)


def signal_key(sig: Signal) -> str:
    return f"{sig.symbol}:{sig.signal_close_ms}"


def deterministic_id(prefix: str, raw: str, max_len: int = 32) -> str:
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]
    return (prefix + digest)[:max_len]


def account_free(account: dict, asset: str) -> float:
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0) or 0)
    return 0.0


def scan_market_detailed(
    market: PublicMarketClient,
    cfg: BotConfig,
    scfg: DonchianConfig,
    logger: EventLogger,
    open_symbols: Optional[set] = None,
) -> dict:
    holding = set(open_symbols or ())
    # Removing a coin from the entry list must not abandon an existing position.
    # Held coins are evaluated for exits only, until they have been closed.
    requested = list(dict.fromkeys([*cfg.universe_symbols, *sorted(holding)]))
    symbols = market.resolve_universe(requested, cfg.quote_asset)
    now_ms = int(time.time() * 1000)
    entries: List[Signal] = []
    exits: List[Signal] = []
    filtered = []
    errors = 0
    started = time.monotonic()

    def one(sym: str):
        # Start immediately at midnight. Retry only if the exchange still
        # returns yesterday's data; never evaluate an unfinished candle.
        for attempt in range(3):
            daily = market.klines(sym, "1d", scfg.daily_history_bars)
            evaluation = evaluate_donchian(
                sym, daily, scfg, now_ms, holding=sym in holding, universe=cfg.universe_name,
            )
            stale = (evaluation.rejection_stage == "Data"
                     and (evaluation.rejection_reason or "").startswith("Stale daily candles:"))
            if not (stale and within_entry_grace(now_ms, scfg.entry_grace_minutes) and attempt < 2):
                return evaluation
            time.sleep(1.0)

    with ThreadPoolExecutor(max_workers=max(1, cfg.scan_workers)) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                evaluation: Evaluation = fut.result()
                if evaluation.signal is not None:
                    sig = evaluation.signal
                    if sig.direction == "EXIT":
                        exits.append(sig)
                        logger.emit("EXIT_SIGNAL", **sig.to_dict())
                    else:
                        entries.append(sig)
                        logger.emit("VALID_SIGNAL", **sig.to_dict())
                else:
                    filtered.append(evaluation.filtered_dict())
            except Exception as exc:
                errors += 1
                logger.emit("SCAN_ERROR", symbol=sym, error=str(exc))
    entries.sort(key=lambda x: x.signal_close_ms)
    exits.sort(key=lambda x: x.symbol)
    filtered.sort(key=lambda x: x["symbol"])
    Path(cfg.signal_log).write_text(json.dumps([x.to_dict() for x in entries], indent=2), encoding="utf-8")
    duration = time.monotonic() - started
    snapshot = {
        "started_at_ms": now_ms,
        "completed_at_ms": int(time.time() * 1000),
        "duration_seconds": round(duration, 3),
        "symbols": symbols,
        "coins_scanned": len(symbols),
        "universe": cfg.universe_name,
        "valid_count": len(entries),
        "entry_count": len(entries),
        "exit_count": len(exits),
        "error_count": errors,
        "signals": [x.to_dict() for x in entries],
        "exit_signals": [x.to_dict() for x in exits],
        "filtered": filtered,
    }
    logger.emit(
        "SCAN_COMPLETE",
        symbols=len(symbols),
        universe=cfg.universe_name,
        entries=len(entries),
        exits=len(exits),
        no_action=len(filtered),
        errors=errors,
        duration_seconds=round(duration, 3),
    )
    return snapshot


def sync_positions(exec_client: TestnetExecutionClient, state: dict, logger: EventLogger, cfg: BotConfig, market=None, rules=None):
    changed = False
    for sym, pd in list(state.get("open_positions", {}).items()):
        pos = Position(**pd)
        if not pos.tp_order_id and not pos.oco_order_list_id:
            # Donchian positions carry no protective orders; live prices are
            # refreshed by the engine. Only legacy OCO positions are reconciled.
            continue
        try:
            tp_ord = exec_client.order(sym, order_id=pos.tp_order_id)
            sl_ord = exec_client.order(sym, order_id=pos.sl_order_id)
            watched = None
            if market is not None:
                watched = monitor_production_exit(exec_client, market, state, pos, tp_ord, sl_ord, cfg, logger, (rules or {}).get(sym))
            if state.get("exit_watch", {}).get(pos.signal_key, {}).get("pending_client_id") and not watched:
                continue
            tp_filled = tp_ord.get("status") == "FILLED"
            sl_filled = sl_ord.get("status") == "FILLED"
            if tp_filled and sl_filled:
                logger.emit("FATAL_POSITION_STATE", symbol=sym, reason="both OCO legs report FILLED")
                continue
            if tp_filled or sl_filled or watched:
                filled_order, outcome = watched if watched else (tp_ord if tp_filled else sl_ord, "TP" if tp_filled else "SL")
                close_ms = int(filled_order.get("updateTime", int(time.time() * 1000)))
                close_dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
                executed_qty = float(filled_order.get("executedQty", 0) or 0)
                cumulative_quote = float(filled_order.get("cummulativeQuoteQty", 0) or 0)
                if executed_qty <= 0 or cumulative_quote <= 0:
                    logger.emit("POSITION_SYNC_ERROR", symbol=sym, error="Filled exit missing actual execution values; retaining position")
                    continue
                exit_price = cumulative_quote / executed_qty
                entry_quote = pos.entry_price * pos.quantity
                exit_quote = exit_price * executed_qty
                pnl_usdt = (exit_price - pos.entry_price) * min(pos.quantity, executed_qty)
                result_r = pnl_usdt / pos.risk_usdt if pos.risk_usdt > 0 else 0.0
                pnl_pct = ((exit_price / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
                record = {
                    "day": close_dt.date().isoformat(), "symbol": sym, "r": result_r,
                    "outcome": outcome, "closed_at": close_dt.isoformat(),
                    "signal_key": pos.signal_key, "opened_at": pos.opened_at,
                    "entry_price": pos.entry_price, "exit_price": exit_price,
                    "quantity": min(pos.quantity, executed_qty), "tp": pos.tp, "sl": pos.sl,
                    "rr": pos.rr, "risk_usdt": pos.risk_usdt,
                    "entry_quote": entry_quote, "exit_quote": exit_quote,
                    "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct,
                    "entry_order_id": pos.entry_order_id, "exit_order_id": int(filled_order.get("orderId", 0) or 0),
                }
                state["realized"].append(record)
                del state["open_positions"][sym]
                save_state(cfg.state_file, state)
                logger.emit("POSITION_CLOSED", **record, realized_r=result_r)
                changed = True
            else:
                terminal = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
                tp_dead = tp_ord.get("status") in terminal
                sl_dead = sl_ord.get("status") in terminal
                if tp_dead and sl_dead:
                    logger.emit("FATAL_UNPROTECTED_POSITION", symbol=sym, reason="both protective orders inactive", signal_key=pos.signal_key)
        except Exception as exc:
            logger.emit("POSITION_SYNC_ERROR", symbol=sym, error=str(exc))
    if changed:
        save_state(cfg.state_file, state)


def allowed_to_trade(state: dict, cfg: BotConfig, logger: EventLogger) -> bool:
    if Path(cfg.kill_switch_file).exists():
        logger.emit("NEW_ENTRIES_BLOCKED", reason="kill switch file exists", file=cfg.kill_switch_file)
        return False
    dr = daily_realized_r(state)
    if dr <= -abs(cfg.daily_loss_limit_r):
        logger.emit("NEW_ENTRIES_BLOCKED", reason="daily loss limit", daily_r=dr)
        return False
    return True


def demo_balance_usdt(cfg: BotConfig, state: dict, realized_pnl_usdt=None) -> float:
    """Ledger balance, before fees: funded capital plus recorded closed P&L.

    Cloud execution supplies the durable database aggregate. Standalone use
    falls back to its state ledger. Open P&L does not fund new entries.
    """
    if realized_pnl_usdt is None:
        realized_pnl_usdt = sum(float(r.get("pnl_usdt") or 0) for r in state.get("realized", []))
    balance = float(cfg.demo_equity_cap_usdt) + float(realized_pnl_usdt)
    if not math.isfinite(balance):
        raise ValueError("Demo balance must be finite")
    return balance


def place_signal(
    sig: Signal,
    market: PublicMarketClient,
    exec_client: TestnetExecutionClient,
    rules: Dict[str, SymbolRules],
    state: dict,
    cfg: BotConfig,
    logger: EventLogger,
    realized_pnl_usdt=None,
):
    key = signal_key(sig)
    if getattr(sig, "direction", "LONG") != "LONG":
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key, reason="not a LONG entry signal")
        return
    if sig.symbol.upper() in EXCLUDED_SYMBOLS:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key, reason="excluded stablecoin/fiat symbol")
        return
    now_ms = int(time.time() * 1000)
    if not is_current_signal_close(sig.signal_close_ms, now_ms):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key, reason="stale signal candle")
        return
    if not within_entry_grace(now_ms, cfg.entry_grace_minutes):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key, reason="entry grace window expired")
        return
    if key in set(state.get("seen_signals", [])):
        return
    # Mark first so process restart can never double-submit the same signal.
    state["seen_signals"].append(key)
    state["seen_signals"] = state["seen_signals"][-10000:]
    save_state(cfg.state_file, state)

    if sig.symbol in state.get("open_positions", {}):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="position already open", signal_key=key)
        return
    if not allowed_to_trade(state, cfg, logger):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="risk manager blocked new entry", signal_key=key)
        return
    rule = rules.get(sig.symbol)
    if not rule or rule.quote_asset != cfg.quote_asset:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="symbol unavailable on Spot Testnet", signal_key=key)
        return
    if not rule.quote_order_qty_market_allowed:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="testnet symbol disallows quoteOrderQty market buy", signal_key=key)
        return

    account = exec_client.account()
    if not account.get("canTrade", False):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="testnet account canTrade=false", signal_key=key)
        return
    usdt_free = account_free(account, cfg.quote_asset)
    open_notional = sum(
        float(p.get("entry_price", 0)) * float(p.get("quantity", 0))
        for p in state.get("open_positions", {}).values()
    )
    # Realized profits/losses change the funded bankroll. Existing commitments
    # remain reserved, and orders cannot spend more than actual Testnet cash.
    balance = demo_balance_usdt(cfg, state, realized_pnl_usdt)
    available_quote = min(usdt_free, max(0.0, balance - open_notional))
    if available_quote <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="no free demo quote balance", signal_key=key)
        return

    # The risk budget includes open P&L; unrealized gains never become spendable
    # cash. Use fresh public marks, including held coins removed from the list.
    # Missing/invalid marks must not silently size an order from stale entry costs.
    open_value = 0.0
    try:
        if not all(math.isfinite(v) for v in (usdt_free, open_notional, available_quote)):
            raise ValueError("non-finite account or position balance")
        for symbol, position in state.get("open_positions", {}).items():
            quantity = float(position.get("quantity", 0))
            entry_price = float(position.get("entry_price", 0))
            if not math.isfinite(quantity) or quantity < 0 or not math.isfinite(entry_price) or entry_price < 0:
                raise ValueError(f"invalid held position: {symbol}")
            if quantity == 0:
                continue
            mark = float(market.best_ask(symbol))
            if not math.isfinite(mark) or mark <= 0:
                raise ValueError(f"invalid equity mark: {symbol}")
            open_value += quantity * mark
        unrealized_pnl = open_value - open_notional
        equity = min(balance + unrealized_pnl, usdt_free + open_value)
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("no positive finite account equity")
    except Exception as exc:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key,
                    reason="account equity unavailable", error=str(exc))
        return

    stop_distance = float(sig.stop_distance)
    if not (math.isfinite(stop_distance) and stop_distance > 0):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="invalid ATR stop distance", signal_key=key)
        return

    # Spec sizing: qty = (risk_pct x equity) / (2 x ATR); buy by quote notional.
    risk_budget = min(equity, equity * cfg.risk_per_trade_pct / 100.0)
    if risk_budget <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="no per-trade risk budget", signal_key=key)
        return

    live_ask = float(market.best_ask(sig.symbol))
    if not math.isfinite(live_ask) or live_ask <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, signal_key=key, reason="invalid entry ask")
        return
    qty = risk_budget / stop_distance
    quote_notional = qty * live_ask
    # No fixed dollar or configured percentage notional ceiling. Preserve the
    # existing cash reserve for fees/movement and never borrow to fill the size.
    quote_notional = min(quote_notional, available_quote * 0.995)
    if quote_notional <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="no order budget", signal_key=key)
        return
    if quote_notional < float(rule.min_notional):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="calculated position below min notional", notional=quote_notional, signal_key=key)
        return

    quote_dec = floor_step(quote_notional, Decimal("0.00000001"))
    if quote_dec <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="position rounds to zero", signal_key=key)
        return
    if quote_dec < rule.min_notional:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="available balance below min notional", signal_key=key)
        return

    entry_cid = deterministic_id("dce", key)
    logger.emit(
        "ENTRY_SUBMIT",
        symbol=sig.symbol, signal_key=key, signal_entry=sig.entry, live_ask=live_ask,
        atr=sig.atr, stop_distance=stop_distance, risk_budget_usdt=risk_budget,
        sizing_equity_usdt=equity, unrealized_pnl_usdt=unrealized_pnl,
        available_quote_usdt=available_quote,
        quote_order_qty=fmt_dec(quote_dec),
    )
    entry = exec_client.market_buy_quote(sig.symbol, fmt_dec(quote_dec), entry_cid)
    executed_qty = float(entry.get("executedQty", 0) or 0)
    cumulative_quote = float(entry.get("cummulativeQuoteQty", 0) or 0)
    if executed_qty <= 0:
        logger.emit("ENTRY_FAILED", symbol=sig.symbol, response=entry, signal_key=key)
        return
    avg_fill = cumulative_quote / executed_qty if cumulative_quote > 0 else live_ask
    state.setdefault("executed_entries", []).append({
        "signal_key": key,
        "symbol": sig.symbol,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "entry_order_id": int(entry.get("orderId", 0) or 0),
    })
    state["executed_entries"] = state["executed_entries"][-1000:]
    try:
        save_state(cfg.state_file, state)
    except Exception as state_exc:
        # Accounting takes priority after a fill. Pause later entries because
        # the daily ledger is no longer longer trustworthy.
        try:
            kill = Path(cfg.kill_switch_file)
            kill.parent.mkdir(parents=True, exist_ok=True)
            kill.write_text("Paused: state save failed after a filled entry.\n", encoding="utf-8")
        except Exception:
            pass
        print(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "event": "STATE_SAVE_ERROR_AFTER_FILL",
            "symbol": sig.symbol,
            "signal_key": key,
            "error": str(state_exc),
        }), flush=True)

    commission_base = 0.0
    for fill in entry.get("fills", []) or []:
        if fill.get("commissionAsset") == rule.base_asset:
            commission_base += float(fill.get("commission", 0) or 0)
    position_qty_raw = max(0.0, executed_qty - commission_base)
    position_qty = floor_step(position_qty_raw, rule.step_size)

    if position_qty < rule.min_qty or position_qty > rule.max_qty:
        # Emergency flatten any filled exposure rather than hold an unusable lot.
        emergency_qty = floor_step(executed_qty, rule.step_size)
        if emergency_qty >= rule.min_qty:
            exec_client.market_sell_qty(sig.symbol, fmt_dec(emergency_qty), deterministic_id("dcx", key))
        logger.emit("EMERGENCY_FLATTEN", symbol=sig.symbol, reason="position quantity invalid", signal_key=key)
        return

    pos = Position(
        symbol=sig.symbol,
        signal_key=key,
        entry_order_id=int(entry["orderId"]),
        entry_client_order_id=entry_cid,
        entry_price=avg_fill,
        quantity=float(position_qty),
        risk_usdt=float(position_qty) * stop_distance,
        opened_at=datetime.now(timezone.utc).isoformat(),
        atr=sig.atr,
        exit_channel=sig.exit_channel,
        entry_channel=sig.entry_channel,
    )
    state["open_positions"][sig.symbol] = asdict(pos)
    save_state(cfg.state_file, state)
    logger.emit("POSITION_OPENED", **asdict(pos))


def close_position(
    sig: Signal,
    market: PublicMarketClient,
    exec_client: TestnetExecutionClient,
    rules: Dict[str, SymbolRules],
    state: dict,
    cfg: BotConfig,
    logger: EventLogger,
):
    """Flatten a held position when the just-closed daily close broke the exit channel."""
    key = signal_key(sig)
    now_ms = int(time.time() * 1000)
    if not is_current_signal_close(sig.signal_close_ms, now_ms):
        logger.emit("EXIT_REJECTED", symbol=sig.symbol, signal_key=key, reason="stale exit signal")
        return
    pos = state.get("open_positions", {}).get(sig.symbol)
    if not pos:
        logger.emit("EXIT_REJECTED", symbol=sig.symbol, signal_key=key, reason="no open position")
        return
    rule = rules.get(sig.symbol)
    if not rule:
        logger.emit("EXIT_ERROR", symbol=sig.symbol, signal_key=key, reason="symbol rules unavailable on Spot Testnet")
        return

    account = exec_client.account()
    base_free = account_free(account, rule.base_asset)
    sell_qty = floor_step(min(float(pos.get("quantity", 0)), base_free), rule.step_size)
    if sell_qty < rule.min_qty:
        logger.emit("EXIT_ERROR", symbol=sig.symbol, signal_key=key,
                    reason="nothing sellable: position below min quantity or base balance missing",
                    quantity=pos.get("quantity"), base_free=base_free)
        return

    cid = deterministic_id("dcx", key)
    sell = exec_client.market_sell_qty(sig.symbol, fmt_dec(sell_qty), cid)
    executed_qty = float(sell.get("executedQty", 0) or 0)
    cumulative_quote = float(sell.get("cummulativeQuoteQty", 0) or 0)
    if executed_qty <= 0 or cumulative_quote <= 0:
        # Unknown-status safety: query the deterministic id before deciding.
        try:
            sell = exec_client.order(sig.symbol, orig_client_order_id=cid)
            executed_qty = float(sell.get("executedQty", 0) or 0)
            cumulative_quote = float(sell.get("cummulativeQuoteQty", 0) or 0)
        except Exception:
            pass
    if executed_qty <= 0 or cumulative_quote <= 0:
        logger.emit("EXIT_ERROR", symbol=sig.symbol, signal_key=key,
                    reason="exit fill could not be verified; position retained", response=sell)
        return

    exit_price = cumulative_quote / executed_qty
    entry_price = float(pos.get("entry_price") or 0.0)
    closed_qty = min(float(pos.get("quantity", 0)), executed_qty)
    pnl_usdt = (exit_price - entry_price) * closed_qty
    risk_usdt = float(pos.get("risk_usdt") or 0.0)
    result_r = pnl_usdt / risk_usdt if risk_usdt > 0 else 0.0
    pnl_pct = ((exit_price / entry_price) - 1.0) * 100.0 if entry_price > 0 else 0.0
    closed_at = datetime.now(timezone.utc)
    record = {
        "day": closed_at.date().isoformat(), "symbol": sig.symbol, "r": result_r,
        "outcome": "CHANNEL_EXIT", "closed_at": closed_at.isoformat(),
        # The trade row is keyed by the entry signal; the exit signal gets its own id.
        "signal_key": pos.get("signal_key") or key, "exit_signal_key": key,
        "opened_at": pos.get("opened_at"),
        "entry_price": entry_price, "exit_price": exit_price,
        "quantity": closed_qty, "atr": pos.get("atr"),
        "entry_channel": pos.get("entry_channel"), "exit_channel": pos.get("exit_channel"),
        "risk_usdt": risk_usdt,
        "entry_quote": entry_price * float(pos.get("quantity", 0)),
        "exit_quote": cumulative_quote,
        "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct,
        "entry_order_id": pos.get("entry_order_id"), "exit_order_id": int(sell.get("orderId", 0) or 0),
    }
    state["realized"].append(record)
    del state["open_positions"][sig.symbol]
    try:
        save_state(cfg.state_file, state)
    except Exception as state_exc:
        try:
            kill = Path(cfg.kill_switch_file)
            kill.parent.mkdir(parents=True, exist_ok=True)
            kill.write_text("Paused: state save failed after a filled exit.\n", encoding="utf-8")
        except Exception:
            pass
        print(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "event": "STATE_SAVE_ERROR_AFTER_CLOSE",
            "symbol": sig.symbol,
            "signal_key": key,
            "error": str(state_exc),
        }), flush=True)
    logger.emit("POSITION_CLOSED", **record, realized_r=result_r)


def run_once(cfg: BotConfig, execute_demo: bool):
    logger = EventLogger(cfg.event_log)
    scfg = DonchianConfig(
        daily_history_bars=cfg.daily_history_bars,
        risk_pct=cfg.risk_per_trade_pct,
        entry_grace_minutes=cfg.entry_grace_minutes,
    )
    market = PublicMarketClient(cfg.request_timeout_s)
    state = load_state(cfg.state_file)

    exec_client = None
    rules: Dict[str, SymbolRules] = {}
    if execute_demo:
        exec_client = TestnetExecutionClient(
            os.environ.get("BINANCE_TESTNET_API_KEY", ""),
            os.environ.get("BINANCE_TESTNET_API_SECRET", ""),
            cfg.request_timeout_s,
            cfg.recv_window_ms,
        )
        rules = load_rules(exec_client.exchange_info())
        sync_positions(exec_client, state, logger, cfg, market, rules)

    snapshot = scan_market_detailed(market, cfg, scfg, logger, open_symbols=set(state.get("open_positions", {})))
    if not execute_demo:
        logger.emit("SCAN_ONLY", reason="--execute-demo not supplied; no orders submitted")
        return

    # Flattening first frees capital for same-boundary entries.
    for row in snapshot.get("exit_signals", []):
        close_position(Signal(**row), market, exec_client, rules, state, cfg, logger)
    for row in snapshot.get("signals", []):
        place_signal(Signal(**row), market, exec_client, rules, state, cfg, logger)


def sleep_to_next_day():
    now = time.time()
    next_day = (math.floor(now / 86_400) + 1) * 86_400
    time.sleep(max(0.0, next_day - now))


def main():
    ap = argparse.ArgumentParser(description="Donchian 55/20 Binance Spot Testnet autonomous demo trader")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--execute-demo", action="store_true", help="Actually place orders on Binance Spot Testnet")
    ap.add_argument("--loop", action="store_true", help="Run once per UTC day after the daily candle closes")
    args = ap.parse_args()

    cfg = load_config(args.config)
    while True:
        run_once(cfg, args.execute_demo)
        if not args.loop:
            break
        sleep_to_next_day()


# Deferred import avoids a module initialization cycle (exit_monitor uses demo_trader helpers).
from exit_monitor import monitor_production_exit  # noqa: E402


if __name__ == "__main__":
    main()
