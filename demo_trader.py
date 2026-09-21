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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from strategy import (
    Candle,
    Evaluation,
    Signal,
    StrategyConfig,
    evaluate_latest,
    evaluate_latest_detailed,
    latest_nr7_box_from_daily,
    reward_risk_is_accepted,
)

PRODUCTION_MARKET_BASE = "https://api.binance.com"
TESTNET_EXECUTION_BASE = "https://testnet.binance.vision"


@dataclass
class BotConfig:
    max_symbols: int = 1000
    scan_workers: int = 50
    hourly_history_bars: int = 1000
    quote_asset: str = "USDT"

    # Frozen demo limits requested by the user.
    risk_per_trade_usdt: float = 2.0
    max_open_positions: int = 1
    max_trades_per_utc_day: int = 1
    max_total_open_risk_usdt: float = 2.0
    daily_loss_limit_r: float = 3.0
    max_position_notional_pct: float = 100.0
    max_entry_drift_pct: float = 0.30
    demo_equity_cap_usdt: float = 200.0

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
    tp: float
    sl: float
    rr: float
    risk_usdt: float
    opened_at: str
    oco_order_list_id: int
    oco_list_client_order_id: str
    tp_order_id: int
    sl_order_id: int


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
        self.s.headers.update({"User-Agent": "nr7-demo-autotrader-market/1.0"})
        # Keep enough reusable connections for the scanner's parallel workers.
        # This changes transport latency only; strategy calculations are untouched.
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
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

    def symbols(self, quote_asset: str, max_symbols: int) -> List[str]:
        tickers = self.get("/api/v3/ticker/24hr")
        suffix = quote_asset.upper()
        blocked = ("UP", "DOWN", "BULL", "BEAR")
        candidates = []
        for item in tickers:
            symbol = str(item.get("symbol", ""))
            if not symbol.endswith(suffix) or any(word in symbol for word in blocked):
                continue
            try:
                candidates.append((symbol, float(item.get("quoteVolume", 0) or 0)))
            except (TypeError, ValueError):
                continue
        candidates.sort(key=lambda row: row[1], reverse=True)
        return [symbol for symbol, _ in candidates[:max_symbols]]

    def klines(self, symbol: str, interval: str, limit: int = 1000) -> List[Candle]:
        rows = self.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)})
        return [
            Candle(
                open_time=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]), close_time=int(r[6]), quote_volume=float(r[7]),
            )
            for r in rows
        ]

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
        self.s.headers.update({"X-MBX-APIKEY": api_key, "User-Agent": "nr7-demo-autotrader-exec/1.0"})
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

    def sell_oco_market_triggers(self, symbol: str, quantity: str, tp: str, sl: str, list_client_id: str):
        # TAKE_PROFIT and STOP_LOSS are trigger-to-market orders. Exact trigger levels are preserved;
        # fill price can differ in a real market, which is why this build remains testnet-only.
        p = {
            "symbol": symbol,
            "side": "SELL",
            "quantity": quantity,
            "listClientOrderId": list_client_id,
            "aboveType": "TAKE_PROFIT",
            "aboveStopPrice": tp,
            "belowType": "STOP_LOSS",
            "belowStopPrice": sl,
            "newOrderRespType": "RESULT",
        }
        try:
            return self._signed("POST", "/api/v3/orderList/oco", p)
        except Exception:
            try:
                return self.order_list(orig_client_order_id=list_client_id)
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
        # an emergency-flattened entry still consumes the one-trade daily cap.
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
    scfg: StrategyConfig,
    logger: EventLogger,
    on_signal=None,
) -> dict:
    symbols = market.symbols(cfg.quote_asset, cfg.max_symbols)
    now_ms = int(time.time() * 1000)
    signals: List[Signal] = []
    phase_one = []
    rejected = []
    filtered = []
    errors = 0
    started = time.monotonic()

    def one(sym: str):
        h = market.klines(sym, "1h", cfg.hourly_history_bars)
        evaluation = evaluate_latest_detailed(sym, h, scfg, now_ms)
        # Fast path is one 1000-bar hourly request. Only if that window has no
        # preserved NR7 box do we make a rare daily-history fallback request.
        if evaluation.rejection_stage == "NR7" and h:
            daily = market.klines(sym, "1d", 1000)
            closed_hourly = [c for c in h if c.close_time < now_ms]
            if closed_hourly:
                signal_day = closed_hourly[-1].open_time // 86_400_000
                fallback_box = latest_nr7_box_from_daily(daily, signal_day, now_ms)
                if fallback_box is not None:
                    evaluation = evaluate_latest_detailed(sym, h, scfg, now_ms, box_override=fallback_box)
        return evaluation

    with ThreadPoolExecutor(max_workers=max(1, cfg.scan_workers)) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                evaluation: Evaluation = fut.result()
                if evaluation.phase_one:
                    candidate = evaluation.phase_one.to_dict()
                    candidate["status"] = "PASSED" if evaluation.signal else "INDICATOR_NOT_PASSED"
                    candidate["indicator_stage"] = evaluation.rejection_stage
                    candidate["indicator_reason"] = evaluation.rejection_reason
                    if evaluation.indicator_volume_surge is not None:
                        candidate["indicator_volume_surge"] = evaluation.indicator_volume_surge
                    if evaluation.close_position is not None:
                        candidate["close_position"] = evaluation.close_position
                    if evaluation.rr is not None:
                        candidate["rr"] = evaluation.rr
                    phase_one.append(candidate)
                if evaluation.signal:
                    sig = evaluation.signal
                    signals.append(sig)
                    logger.emit("VALID_SIGNAL", **sig.to_dict())
                    if on_signal is not None:
                        try:
                            on_signal(sig)
                        except Exception as callback_exc:
                            logger.emit("SIGNAL_CALLBACK_ERROR", symbol=sym, error=str(callback_exc), signal_key=signal_key(sig))
                elif evaluation.phase_one:
                    rejected.append(evaluation.filtered_dict())
                else:
                    # The original App display places every pre-confirmation
                    # failure in Filtered and preserves its exact App reason.
                    filtered.append(evaluation.filtered_dict())
            except Exception as exc:
                errors += 1
                logger.emit("SCAN_ERROR", symbol=sym, error=str(exc))
    signals.sort(key=lambda x: x.signal_close_ms)
    phase_one.sort(key=lambda x: x["symbol"])
    rejected.sort(key=lambda x: x["symbol"])
    filtered.sort(key=lambda x: x["symbol"])
    Path(cfg.signal_log).write_text(json.dumps([x.to_dict() for x in signals], indent=2), encoding="utf-8")
    duration = time.monotonic() - started
    snapshot = {
        "started_at_ms": now_ms,
        "completed_at_ms": int(time.time() * 1000),
        "duration_seconds": round(duration, 3),
        "symbols": symbols,
        "coins_scanned": len(symbols),
        "app_confirmed_count": len(phase_one),
        "valid_count": len(signals),
        "indicator_rejected_count": max(0, len(phase_one) - len(signals)),
        "error_count": errors,
        "phase_one": phase_one,
        "signals": [x.to_dict() for x in signals],
        "rejected": rejected,
        "filtered": filtered,
    }
    logger.emit(
        "SCAN_COMPLETE",
        symbols=len(symbols),
        app_confirmed=len(phase_one),
        valid_signals=len(signals),
        rejected=len(rejected),
        filtered=len(filtered),
        errors=errors,
        duration_seconds=round(duration, 3),
    )
    return snapshot


def scan_market(market: PublicMarketClient, cfg: BotConfig, scfg: StrategyConfig, logger: EventLogger, on_signal=None) -> List[Signal]:
    return [Signal(**row) for row in scan_market_detailed(market, cfg, scfg, logger, on_signal)["signals"]]


def sync_positions(exec_client: TestnetExecutionClient, state: dict, logger: EventLogger, cfg: BotConfig):
    changed = False
    for sym, pd in list(state.get("open_positions", {}).items()):
        pos = Position(**pd)
        try:
            tp_ord = exec_client.order(sym, order_id=pos.tp_order_id)
            sl_ord = exec_client.order(sym, order_id=pos.sl_order_id)
            tp_filled = tp_ord.get("status") == "FILLED"
            sl_filled = sl_ord.get("status") == "FILLED"
            if tp_filled and sl_filled:
                logger.emit("FATAL_POSITION_STATE", symbol=sym, reason="both OCO legs report FILLED")
                continue
            if tp_filled or sl_filled:
                filled_order = tp_ord if tp_filled else sl_ord
                result_r = pos.rr if tp_filled else -1.0
                outcome = "TP" if tp_filled else "SL"
                close_ms = int(filled_order.get("updateTime", int(time.time() * 1000)))
                close_dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
                executed_qty = float(filled_order.get("executedQty", pos.quantity) or pos.quantity)
                cumulative_quote = float(filled_order.get("cummulativeQuoteQty", 0) or 0)
                exit_price = (cumulative_quote / executed_qty) if executed_qty > 0 and cumulative_quote > 0 else (pos.tp if tp_filled else pos.sl)
                entry_quote = pos.entry_price * pos.quantity
                exit_quote = exit_price * executed_qty
                pnl_usdt = (exit_price - pos.entry_price) * min(pos.quantity, executed_qty)
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
    if len(state.get("open_positions", {})) >= cfg.max_open_positions:
        logger.emit("NEW_ENTRIES_BLOCKED", reason="max open positions", count=len(state["open_positions"]))
        return False
    opened_today = trades_opened_today(state)
    if opened_today >= cfg.max_trades_per_utc_day:
        logger.emit(
            "NEW_ENTRIES_BLOCKED",
            reason="max trades per UTC day",
            count=opened_today,
            limit=cfg.max_trades_per_utc_day,
        )
        return False
    dr = daily_realized_r(state)
    if dr <= -abs(cfg.daily_loss_limit_r):
        logger.emit("NEW_ENTRIES_BLOCKED", reason="daily loss limit", daily_r=dr)
        return False
    return True


def place_signal(
    sig: Signal,
    market: PublicMarketClient,
    exec_client: TestnetExecutionClient,
    rules: Dict[str, SymbolRules],
    state: dict,
    cfg: BotConfig,
    logger: EventLogger,
    minimum_reward_risk: float = 2.0,
    maximum_reward_risk: float = 4.0,
):
    key = signal_key(sig)
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
    # Keep risk percentages anchored to the configured demo bankroll rather than
    # shrinking them merely because some USDT is currently deployed in open positions.
    equity = min(usdt_free + open_notional, cfg.demo_equity_cap_usdt)
    if equity <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="no free demo quote balance", signal_key=key)
        return

    # Live production book check protects the signal-close assumptions before demo execution.
    live_ask = market.best_ask(sig.symbol)
    drift_pct = (live_ask / sig.entry - 1.0) * 100.0
    if drift_pct > cfg.max_entry_drift_pct:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="entry drift too high", drift_pct=drift_pct, signal_key=key)
        return
    if not (sig.sl < live_ask < sig.tp):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="live price outside SL/TP interval", live_ask=live_ask, signal_key=key)
        return
    execution_rr = (sig.tp - live_ask) / (live_ask - sig.sl)
    if not reward_risk_is_accepted(execution_rr, minimum_reward_risk, maximum_reward_risk):
        logger.emit(
            "TRADE_REJECTED", symbol=sig.symbol,
            reason=f"execution-time RR outside [{minimum_reward_risk:g},{maximum_reward_risk:g}]",
            execution_rr=execution_rr, signal_key=key,
        )
        return

    open_risk = sum(float(p.get("risk_usdt", 0)) for p in state.get("open_positions", {}).values())
    max_total_risk = cfg.max_total_open_risk_usdt
    available_risk = max(0.0, max_total_risk - open_risk)
    risk_budget = min(cfg.risk_per_trade_usdt, available_risk, equity)
    if risk_budget <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="combined open-risk limit", signal_key=key)
        return

    stop_distance = live_ask - sig.sl
    qty_by_risk = risk_budget / stop_distance
    max_notional = equity * cfg.max_position_notional_pct / 100.0
    qty_by_notional = max_notional / live_ask
    qty = min(qty_by_risk, qty_by_notional)
    quote_notional = qty * live_ask
    if quote_notional < float(rule.min_notional):
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="calculated position below min notional", notional=quote_notional, signal_key=key)
        return

    # Leave a tiny buffer for testnet price movement/fees.
    quote_notional = min(quote_notional, usdt_free * 0.995)
    quote_dec = floor_step(quote_notional, Decimal("0.00000001"))
    if quote_dec <= 0:
        logger.emit("TRADE_REJECTED", symbol=sig.symbol, reason="position rounds to zero", signal_key=key)
        return

    entry_cid = deterministic_id("nr7e", key)
    logger.emit(
        "ENTRY_SUBMIT",
        symbol=sig.symbol, signal_key=key, signal_entry=sig.entry, live_ask=live_ask,
        execution_rr=execution_rr, risk_budget_usdt=risk_budget, quote_order_qty=fmt_dec(quote_dec),
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
        # Protection takes priority after a fill. Continue immediately to the
        # OCO/flatten path even if the accounting ledger could not be written.
        # Also attempt to pause later entries because the daily ledger is no
        # longer trustworthy.
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
    protect_qty_raw = max(0.0, executed_qty - commission_base)
    protect_qty = floor_step(protect_qty_raw, rule.step_size)
    tp_tick = nearest_tick(sig.tp, rule.tick_size)
    sl_tick = nearest_tick(sig.sl, rule.tick_size)

    if protect_qty < rule.min_qty or protect_qty > rule.max_qty:
        # Emergency flatten any filled exposure rather than leave an uncontrolled position.
        emergency_qty = floor_step(protect_qty_raw, rule.step_size)
        if emergency_qty >= rule.min_qty:
            exec_client.market_sell_qty(sig.symbol, fmt_dec(emergency_qty), deterministic_id("nr7x", key))
        logger.emit("EMERGENCY_FLATTEN", symbol=sig.symbol, reason="protective quantity invalid", signal_key=key)
        return

    # Re-check the same inclusive mobile-parity R:R gate using the actual fill.
    actual_risk = avg_fill - float(sl_tick)
    actual_reward = float(tp_tick) - avg_fill
    actual_rr = actual_reward / actual_risk if actual_risk > 0 else -1
    actual_position_risk = float(protect_qty) * actual_risk
    if actual_position_risk > cfg.risk_per_trade_usdt + 1e-9 or actual_position_risk > risk_budget + 1e-9:
        exec_client.market_sell_qty(sig.symbol, fmt_dec(protect_qty), deterministic_id("nr7x", key))
        logger.emit(
            "EMERGENCY_FLATTEN", symbol=sig.symbol,
            reason="fill risk exceeded absolute demo limit",
            actual_risk_usdt=actual_position_risk,
            risk_limit_usdt=min(cfg.risk_per_trade_usdt, risk_budget),
            signal_key=key,
        )
        return
    if not reward_risk_is_accepted(actual_rr, minimum_reward_risk, maximum_reward_risk):
        exec_client.market_sell_qty(sig.symbol, fmt_dec(protect_qty), deterministic_id("nr7x", key))
        logger.emit(
            "EMERGENCY_FLATTEN", symbol=sig.symbol,
            reason=f"fill changed RR outside [{minimum_reward_risk:g},{maximum_reward_risk:g}]",
            actual_rr=actual_rr, signal_key=key,
        )
        return

    list_cid = deterministic_id("nr7o", key)
    try:
        oco = exec_client.sell_oco_market_triggers(
            sig.symbol, fmt_dec(protect_qty), fmt_dec(tp_tick), fmt_dec(sl_tick), list_cid
        )
    except Exception as exc:
        # Core safety invariant: never intentionally leave a filled position without protection.
        try:
            exec_client.market_sell_qty(sig.symbol, fmt_dec(protect_qty), deterministic_id("nr7x", key))
            logger.emit("EMERGENCY_FLATTEN", symbol=sig.symbol, reason="OCO placement failed", error=str(exc), signal_key=key)
        except Exception as flatten_exc:
            logger.emit("FATAL_UNPROTECTED_POSITION", symbol=sig.symbol, error=str(flatten_exc), original_error=str(exc), signal_key=key)
        return

    orders = oco.get("orders", []) or []
    reports = oco.get("orderReports", []) or []
    tp_order_id = sl_order_id = None
    for r in reports:
        typ = r.get("type")
        oid = r.get("orderId")
        if typ in ("TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
            tp_order_id = int(oid)
        elif typ in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            sl_order_id = int(oid)
    if tp_order_id is None or sl_order_id is None:
        # Fallback: query each order id and identify its type.
        for x in orders:
            oid = int(x["orderId"])
            od = exec_client.order(sig.symbol, order_id=oid)
            if od.get("type") in ("TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
                tp_order_id = oid
            elif od.get("type") in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
                sl_order_id = oid
    if tp_order_id is None or sl_order_id is None:
        # We know an OCO was accepted but cannot verify both legs: block state and require intervention.
        logger.emit("FATAL_POSITION_STATE", symbol=sig.symbol, reason="could not identify both OCO legs", response=oco, signal_key=key)
        return

    pos = Position(
        symbol=sig.symbol,
        signal_key=key,
        entry_order_id=int(entry["orderId"]),
        entry_client_order_id=entry_cid,
        entry_price=avg_fill,
        quantity=float(protect_qty),
        tp=float(tp_tick),
        sl=float(sl_tick),
        rr=actual_rr,
        risk_usdt=actual_position_risk,
        opened_at=datetime.now(timezone.utc).isoformat(),
        oco_order_list_id=int(oco["orderListId"]),
        oco_list_client_order_id=list_cid,
        tp_order_id=int(tp_order_id),
        sl_order_id=int(sl_order_id),
    )
    state["open_positions"][sig.symbol] = asdict(pos)
    save_state(cfg.state_file, state)
    logger.emit("POSITION_OPENED", **asdict(pos))


def run_once(cfg: BotConfig, execute_demo: bool):
    logger = EventLogger(cfg.event_log)
    scfg = StrategyConfig(
        app_minimum_surge=1.5,
        indicator_volume_lookback=168,
        indicator_minimum_surge=1.5,
        minimum_close_position=0.75,
        pine_minimum_reward_risk=1.0,
        minimum_reward_risk=2.0,
        maximum_reward_risk=4.0,
        swing_length=50,
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
        sync_positions(exec_client, state, logger, cfg)

    signals = scan_market(market, cfg, scfg, logger)
    if not execute_demo:
        logger.emit("SCAN_ONLY", reason="--execute-demo not supplied; no orders submitted")
        return

    for sig in signals:
        place_signal(
            sig, market, exec_client, rules, state, cfg, logger,
            scfg.minimum_reward_risk, scfg.maximum_reward_risk,
        )


def sleep_to_next_hour():
    now = time.time()
    next_hour = (math.floor(now / 3600) + 1) * 3600 + 10
    time.sleep(max(30, next_hour - now))


def main():
    ap = argparse.ArgumentParser(description="NR7 app + indicator Binance Spot Testnet autonomous demo trader")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--execute-demo", action="store_true", help="Actually place orders on Binance Spot Testnet")
    ap.add_argument("--loop", action="store_true", help="Run after every completed UTC 1H candle")
    args = ap.parse_args()

    cfg = load_config(args.config)
    while True:
        run_once(cfg, args.execute_demo)
        if not args.loop:
            break
        sleep_to_next_hour()


if __name__ == "__main__":
    main()
