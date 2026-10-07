"""Production trigger -> Testnet fill reconciliation. Never submits real orders.

Called with the engine's state lock held. Cursor and exit intent survive restarts.
"""
from datetime import datetime
from decimal import Decimal

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def monitor_production_exit(client, market, state, pos, tp_order, sl_order, cfg, logger, rule):
    # Deferred import avoids a module initialization cycle.
    from demo_trader import save_state, deterministic_id, floor_step, fmt_dec

    watches = state.setdefault("exit_watch", {})
    watch = watches.setdefault(pos.signal_key, {
        "start_ms": int(datetime.fromisoformat(pos.opened_at).timestamp() * 1000) + 1,
        "last_id": None,
    })
    orders = [tp_order, sl_order]
    pending = watch.get("pending_client_id")
    if not pending and any(o.get("status") == "FILLED" for o in orders):
        # Native Testnet fill is authoritative, even if production differs.
        return None

    if not watch.get("reason"):
        # A bounded page budget prevents a busy symbol starving other positions.
        # Never skip ahead; a backlog continues from the persisted cursor next poll.
        for _ in range(4):
            rows = market.aggregate_trades(pos.symbol, start_ms=watch["start_ms"],
                                           from_id=None if watch["last_id"] is None else watch["last_id"] + 1)
            rows = sorted(rows, key=lambda r: int(r["a"]))
            for trade in rows:
                ident, timestamp, price = int(trade["a"]), int(trade["T"]), float(trade["p"])
                if watch["last_id"] is not None and ident <= watch["last_id"]:
                    raise RuntimeError("Aggregate trade cursor did not advance")
                watch["last_id"] = ident
                if timestamp < watch["start_ms"]:
                    continue
                if price >= pos.tp or price <= pos.sl:
                    watch.update(reason="TP" if price >= pos.tp else "SL",
                                 trigger_ms=timestamp, trigger_price=price, trigger_source="production_aggTrade")
                    break
            save_state(cfg.state_file, state)
            if watch.get("reason") or len(rows) < 1000:
                break
        if not watch.get("reason"):
            return None
        logger.emit("PRODUCTION_EXIT_TRIGGER", symbol=pos.symbol, signal_key=pos.signal_key,
                    outcome=watch["reason"], trigger_price=watch["trigger_price"],
                    trigger_ms=watch["trigger_ms"], trade_id=watch["last_id"])

    # Cancel first, then query BOTH orders again. A fill racing cancellation
    # must reduce the remainder before a market sell is even considered.
    if any(o.get("status") not in TERMINAL for o in orders):
        try:
            client.cancel_order_list(pos.symbol, pos.oco_order_list_id)
        except Exception as exc:
            logger.emit("EXIT_CANCEL_RECHECK", symbol=pos.symbol, error=str(exc))
        orders = [client.order(pos.symbol, order_id=pos.tp_order_id),
                  client.order(pos.symbol, order_id=pos.sl_order_id)]
    if any(o.get("status") not in TERMINAL for o in orders):
        raise RuntimeError("Exit waiting for verified OCO cancellation; no additional sell submitted")

    # All previously completed residual sells are retained as full broker records.
    completed = watch.setdefault("completed_orders", [])
    pending_order = None
    if pending:
        # Never resubmit an uncertain request, even after restart. API query errors
        # keep the intent open and visible for retry/reconciliation.
        pending_order = client.order(pos.symbol, orig_client_order_id=pending)
        if pending_order.get("status") not in TERMINAL:
            return None
        if not any(o.get("clientOrderId") == pending for o in completed):
            completed.append(dict(pending_order, clientOrderId=pending))
        watch.pop("pending_client_id", None)
        save_state(cfg.state_file, state)

    fills = orders + completed
    qty = sum(Decimal(str(o.get("executedQty", "0"))) for o in fills)
    quote = sum(Decimal(str(o.get("cummulativeQuoteQty", "0"))) for o in fills)
    remaining = max(Decimal(0), Decimal(str(pos.quantity)) - qty)
    if remaining > 0:
        if rule is None:
            raise RuntimeError("Exit has no symbol quantity rules; cannot safely size remaining sell")
        sell_qty = floor_step(remaining, rule.step_size)
        if sell_qty > 0:
            if sell_qty < rule.min_qty or sell_qty > rule.max_qty:
                raise RuntimeError("Remaining exit quantity outside symbol lot limits")
            # Seed kept unchanged so re-derived IDs stay stable across restarts.
            cid = deterministic_id("nr7close", pos.signal_key + ":" + str(len(completed)))
            # Write intent BEFORE sending. If write fails, do not send.
            watch["pending_client_id"] = cid
            save_state(cfg.state_file, state)
            result = client.market_sell_qty(pos.symbol, fmt_dec(sell_qty), cid)
            # Reconcile next poll (or immediately recurse with the same canceled
            # legs); never declare a close from a submitted order or price touch.
            if result.get("status") != "FILLED":
                return None
            completed.append(dict(result, clientOrderId=cid))
            watch.pop("pending_client_id", None)
            save_state(cfg.state_file, state)
            fills = orders + completed
            qty = sum(Decimal(str(o.get("executedQty", "0"))) for o in fills)
            quote = sum(Decimal(str(o.get("cummulativeQuoteQty", "0"))) for o in fills)
            remaining = max(Decimal(0), Decimal(str(pos.quantity)) - qty)
            if floor_step(remaining, rule.step_size) > 0:
                return None
        if remaining > 0:
            logger.emit("EXIT_DUST", symbol=pos.symbol, signal_key=pos.signal_key, remaining_qty=str(remaining))
    if qty <= 0 or quote <= 0:
        raise RuntimeError("Exit has no verified execution quantity/quote")
    native_filled = next((o for o in orders if o.get("status") == "FILLED"), None)
    outcome = ("TP" if native_filled.get("orderId") == pos.tp_order_id else "SL") if native_filled else watch["reason"]
    latest = max((o for o in fills if Decimal(str(o.get("executedQty", "0"))) > 0),
                 key=lambda o: int(o.get("updateTime", o.get("transactTime", 0))))
    return ({"status": "FILLED", "executedQty": str(qty), "cummulativeQuoteQty": str(quote),
             "updateTime": latest.get("updateTime", latest.get("transactTime")),
             "orderId": latest.get("orderId", 0)}, outcome)
