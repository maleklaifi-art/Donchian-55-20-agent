"""Native Testnet fixed stops; durable intents and conservative unknown-status recovery.

Existing channel-only and legacy OCO positions never enter these paths.
"""
import math
import time
from decimal import Decimal

from strategy import Signal, DAY_MS
from demo_trader import (BinanceAPIError, D, account_exit_fill, close_position,
                         deterministic_id, finish_entry, floor_step, fmt_dec, save_state, order_definitely_rejected)

TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}


def market_quantity(quantity, rule):
    """Round down on the common LOT_SIZE / MARKET_LOT_SIZE grid."""
    steps = [x for x in (rule.step_size, rule.market_step_size) if x > 0]
    places = max((-x.as_tuple().exponent for x in steps), default=0)
    scale = Decimal(10)**places
    step = Decimal(math.lcm(*(int(x*scale) for x in steps)))/scale if steps else Decimal(0)
    cap = min(rule.max_qty, rule.market_max_qty) if rule.market_max_qty > 0 else rule.max_qty
    return floor_step(min(D(quantity), cap), step)


def exit_signal(pos):
    close_ms = (int(time.time()*1000)//DAY_MS)*DAY_MS-1
    return Signal(symbol=pos['symbol'], signal_close_ms=close_ms, close_time_utc='',
        direction='EXIT', entry=pos['entry_price'], entry_channel=pos.get('entry_channel') or 0,
        exit_channel=pos.get('exit_channel') or 0, atr=pos.get('atr') or 0,
        stop_distance=pos.get('fixed_stop_distance') or 0,
        strategy_version=pos.get('strategy_version', 'legacy_donchian'),
        exit_period=pos.get('exit_period',20), atr_stop_enabled=True)


def publish(state, cfg, logger, pos):
    save_state(cfg.state_file, state)
    logger.emit('POSITION_UPDATED', **pos)


def fail_protection(client, market, state, cfg, logger, rules, pos, reason, outcome="PROTECTION_FAILURE"):
    # Only call when no protective order can still be live. Unknown states wait
    # for reconciliation, because a competing market sell could double-execute.
    pos['native_stop_state'] = 'FAILED'
    pos['native_stop_error'] = reason
    pos['exit_reason'] = outcome
    sig = exit_signal(pos)
    pos['pending_exit_signal'] = sig.to_dict()
    publish(state, cfg, logger, pos)
    close_position(sig, market, client, rules, state, cfg, logger)


def query_stop(client, pos):
    return client.order(pos['symbol'], orig_client_order_id=pos['native_stop_client_id'])


def cancel_for_exit(client, market, state, cfg, logger, rules, sig):
    """True only when the native stop is proven inactive and accounted for."""
    pos = state['open_positions'][sig.symbol]
    if not pos.get('native_stop_client_id'):
        return True
    try:
        order = query_stop(client, pos)
        if order.get('status') not in TERMINAL:
            try:
                client.cancel_order(sig.symbol, pos['native_stop_client_id'])
            except Exception:
                pass  # A trigger may have won the race: always query again.
            order = query_stop(client, pos)
        if order.get('status') not in TERMINAL:
            raise ValueError('Protective stop is still active; channel sell waits')
        if float(order.get('executedQty', 0) or 0) > 0:
            pos['exit_reason'] = 'ATR_STOP'
            account_exit_fill(sig, order, market, client, rules, state, cfg, logger)
        pos = state['open_positions'].get(sig.symbol)
        if not pos:
            return False
        pos.update(native_stop_client_id='', native_stop_order_id=0,
                   native_stop_state='DISARMED', native_stop_error='')
        publish(state, cfg, logger, pos)
        return True
    except Exception as exc:
        pos = state['open_positions'].get(sig.symbol)
        if pos:
            pos.update(native_stop_state='UNKNOWN', native_stop_error=str(exc))
            publish(state, cfg, logger, pos)
        return False


def maintain_stop(client, market, state, cfg, logger, rules, symbol):
    pos = state['open_positions'].get(symbol)
    if not pos or not pos.get('atr_stop_enabled'):
        return
    rule = rules.get(symbol)
    if not rule:
        raise ValueError('Missing symbol rules for protective stop')
    if pos.get('pending_exit_signal'):
        close_position(Signal(**pos['pending_exit_signal']), market, client, rules, state, cfg, logger)
        return
    try:
        if pos.get('native_stop_client_id'):
            order = query_stop(client, pos)
        else:
            stop = float(pos.get('fixed_stop_price') or 0)
            quantity = market_quantity(pos['quantity'], rule)
            if not math.isfinite(stop) or stop <= 0 or quantity < max(rule.min_qty,rule.market_min_qty):
                fail_protection(client, market, state, cfg, logger, rules, pos, 'Unusable stop price or quantity')
                return
            pos['native_stop_client_id'] = deterministic_id('dcs', pos['signal_key'])
            pos['native_stop_state'] = 'SUBMITTING'
            publish(state, cfg, logger, pos)  # BEFORE sending the stop
            try:
                order = client.stop_loss_sell(symbol, fmt_dec(quantity), fmt_dec(D(stop)), pos['native_stop_client_id'])
            except BinanceAPIError as exc:
                if not order_definitely_rejected(exc):
                    raise
                pos['native_stop_client_id'] = ''
                fail_protection(client, market, state, cfg, logger, rules, pos, str(exc),
                    "ATR_STOP" if any(t in str(exc).lower() for t in ("immediately trigger", "trigger immediately")) else "PROTECTION_FAILURE")
                return
        status = order.get('status')
        prior = (pos.get('native_stop_order_id'), pos.get('native_stop_state'), pos.get('native_stop_error'))
        pos['native_stop_order_id'] = int(order['orderId'])
        pos['native_stop_state'] = status or 'UNKNOWN'
        pos['native_stop_error'] = ''
        if prior != (pos['native_stop_order_id'], pos['native_stop_state'], pos['native_stop_error']):
            publish(state, cfg, logger, pos)
        if status in TERMINAL:
            # A triggered/expired/canceled native order cannot be replaced blindly.
            # Account any partial fill, then flatten only the verified remainder.
            sig = exit_signal(pos)
            pos['exit_reason'] = 'ATR_STOP' if float(order.get('executedQty',0) or 0)>0 else 'PROTECTION_FAILURE'
            pos['pending_exit_signal'] = sig.to_dict()
            publish(state, cfg, logger, pos)
            close_position(sig, market, client, rules, state, cfg, logger)
    except Exception as exc:
        pos = state['open_positions'].get(symbol)
        if pos:
            pos.update(native_stop_state='UNKNOWN', native_stop_error=str(exc))
            publish(state, cfg, logger, pos)
        logger.emit('STOP_RECONCILIATION_PENDING', symbol=symbol, error=str(exc))


def recover_entries(client, market, state, cfg, logger, rules):
    for cid, intent in list(state.get('pending_entries', {}).items()):
        sig = Signal(**intent['signal'])
        try:
            if sig.symbol in state['open_positions']:
                # Entry and journal removal are saved atomically; disagreement is
                # not permission to overwrite an existing position.
                raise ValueError('Pending entry conflicts with a recorded position')
            order = client.order(sig.symbol, orig_client_order_id=cid)
            if order.get('status') not in TERMINAL:
                try:
                    client.cancel_order(sig.symbol, cid)
                except Exception:
                    pass
                order = client.order(sig.symbol, orig_client_order_id=cid)
            if order.get('status') not in TERMINAL:
                continue
            finish_entry(sig, order, cid, intent['stop_distance'], intent['live_ask'],
                         market, client, rules, state, cfg, logger)
        except Exception as exc:
            logger.emit('ENTRY_RECONCILIATION_PENDING', symbol=sig.symbol, error=str(exc))
