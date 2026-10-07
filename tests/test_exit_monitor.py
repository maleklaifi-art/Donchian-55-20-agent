from dataclasses import asdict
from decimal import Decimal
from unittest.mock import Mock

import pytest
from demo_trader import BotConfig, Position, SymbolRules, initial_state, sync_positions, load_state


class Exchange:
    def __init__(self):
        self.orders = {
            10: dict(orderId=10,status='NEW',executedQty='0',cummulativeQuoteQty='0',updateTime=1000),
            11: dict(orderId=11,status='NEW',executedQty='0',cummulativeQuoteQty='0',updateTime=1000)}
        self.client_orders = {}
        self.sells = []
        self.race = False
        self.cancel_fail = False
        self.timeout = False
        self.price = 119

    def order(self, symbol, order_id=None, orig_client_order_id=None):
        return dict(self.orders[order_id] if order_id is not None else self.client_orders[orig_client_order_id])

    def cancel_order_list(self, symbol, ident):
        if self.cancel_fail:
            raise RuntimeError('cancel timeout with still active orders')
        for o in self.orders.values():
            o['status'] = 'CANCELED'
        if self.race:
            self.orders[10].update(status='FILLED', executedQty='2',cummulativeQuoteQty='240',updateTime=4000)

    def market_sell_qty(self, symbol, quantity, client_id):
        self.sells.append((symbol, quantity, client_id))
        record = dict(orderId=12,status='FILLED',executedQty=quantity,cummulativeQuoteQty=str(float(quantity)*self.price),updateTime=5000)
        self.client_orders[client_id] = record
        if self.timeout:
            raise RuntimeError('lost response after exchange accepted order')
        return dict(record)


@pytest.fixture
def context(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path/'state.json'), kill_switch_file=str(tmp_path/'STOP'))
    # Legacy pre-cutover position that still carries OCO protective orders.
    pos = Position(symbol='DUSKUSDT', signal_key='DUSKUSDT:1', entry_order_id=1,
                   entry_client_order_id='entry', entry_price=100, quantity=2,
                   risk_usdt=4, opened_at='1970-01-01T00:00:01+00:00',
                   tp=120, sl=98, rr=10, oco_order_list_id=5,
                   oco_list_client_order_id='list', tp_order_id=10, sl_order_id=11)
    state = initial_state()
    state['open_positions'][pos.symbol] = asdict(pos)
    market = Mock()
    market.aggregate_trades.return_value = [dict(a=1,T=2000,p='121'),dict(a=2,T=3000,p='97')]
    rule = SymbolRules(pos.symbol,'DUSK','USDT',Decimal('.01'),Decimal('.001'),Decimal('.001'),Decimal('10000'),Decimal('5'),True)
    exchange, logger = Exchange(), Mock()
    def sync():
        sync_positions(exchange,state,logger,cfg,market,{pos.symbol:rule})
    return cfg,pos,state,market,exchange,logger,sync


def test_donchian_channel_position_is_left_to_the_daily_scan(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path/'state.json'), kill_switch_file=str(tmp_path/'STOP'))
    pos = Position(symbol='BTCUSDT', signal_key='BTCUSDT:1', entry_order_id=7,
                   entry_client_order_id='dce1', entry_price=100, quantity=0.5,
                   risk_usdt=2, opened_at='2026-10-01T00:00:00+00:00',
                   atr=1.0, exit_channel=90.0, entry_channel=120.0)
    state = initial_state()
    state['open_positions'][pos.symbol] = asdict(pos)
    exchange, logger, market = Mock(), Mock(), Mock()
    rule = SymbolRules(pos.symbol, 'BTC', 'USDT', Decimal('.01'), Decimal('.000001'),
                       Decimal('.000001'), Decimal('10000'), Decimal('5'), True)
    sync_positions(exchange, state, logger, cfg, market, {pos.symbol: rule})
    assert exchange.mock_calls == []
    assert pos.symbol in state['open_positions']
    assert state['realized'] == []
    logger.emit.assert_not_called()


def test_tp_touch_then_reversal_closes_once_at_actual_fill_even_when_entries_paused(context):
    cfg,pos,state,market,ex,log,sync = context
    from pathlib import Path
    Path(cfg.kill_switch_file).touch()
    sync()
    assert len(ex.sells) == 1
    assert state['open_positions'] == {}
    closed = state['realized'][0]
    assert closed['outcome'] == 'TP' and closed['exit_price'] == 119
    assert closed['pnl_usdt'] == 38 and closed['r'] == 9.5
    assert load_state(cfg.state_file) == state
    sync()
    assert len(ex.sells) == len(state['realized']) == 1


def test_stop_touch_before_tp_wins_and_gap_records_actual_lower_fill(context):
    cfg,pos,state,market,ex,log,sync = context
    market.aggregate_trades.return_value = [dict(a=1,T=2000,p='96'),dict(a=2,T=3000,p='121')]
    ex.price = 95
    sync()
    assert state['realized'][0]['outcome'] == 'SL'
    assert state['realized'][0]['exit_price'] == 95
    assert state['realized'][0]['r'] == -2.5


def test_native_fill_racing_cancel_does_not_double_sell(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.race = True
    sync()
    assert ex.sells == [] and state['open_positions'] == {}
    assert state['realized'][0]['exit_price'] == 120


def test_partial_native_tp_only_sells_remainder(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.orders[10].update(status='PARTIALLY_FILLED',executedQty='.5',cummulativeQuoteQty='60')
    sync()
    assert ex.sells[0][1] == '1.5'
    assert state['realized'][0]['quantity'] == 2
    assert state['realized'][0]['exit_price'] == 119.25


def test_uncertain_sell_reconciles_after_reload_without_resubmitting(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.timeout = True
    sync()
    assert len(ex.sells) == 1 and len(state['open_positions']) == 1
    reloaded = load_state(cfg.state_file)
    state.clear();state.update(reloaded)
    sync()
    assert len(ex.sells) == 1 and state['open_positions'] == {}


def test_cancel_failure_retains_protection_and_intent_then_retries(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.cancel_fail = True
    sync()
    assert ex.sells == [] and len(state['open_positions']) == 1
    assert state['exit_watch'][pos.signal_key]['reason'] == 'TP'
    ex.cancel_fail = False
    market.aggregate_trades.side_effect = AssertionError('latched trigger must not be recalculated')
    sync()
    assert state['realized'][0]['outcome'] == 'TP'


def test_native_tp_does_not_depend_on_production_data_availability(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.orders[10].update(status='FILLED',executedQty='2',cummulativeQuoteQty='240')
    ex.orders[11]['status'] = 'CANCELED'
    market.aggregate_trades.side_effect = RuntimeError('production unavailable')
    sync()
    market.aggregate_trades.assert_not_called()
    assert state['open_positions'] == {} and ex.sells == []


def test_no_price_touch_keeps_trade_and_persists_cursor(context):
    cfg,pos,state,market,ex,log,sync = context
    market.aggregate_trades.return_value = [dict(a=7,T=2000,p='110')]
    sync()
    assert state['exit_watch'][pos.signal_key]['last_id'] == 7 and not ex.sells
    market.aggregate_trades.return_value = [dict(a=8,T=3000,p='120')]
    sync()
    assert market.aggregate_trades.call_args.kwargs['from_id'] == 8
    assert state['realized'][0]['outcome'] == 'TP'


def test_no_closed_trade_from_missing_fill_values(context):
    cfg,pos,state,market,ex,log,sync = context
    ex.orders[10].update(status='FILLED')
    sync()
    assert len(state['open_positions']) == 1 and not state['realized']


def test_database_recovers_close_saved_before_dashboard_log(tmp_path):
    from database import TradeDatabase
    db = TradeDatabase(str(tmp_path/'db.sqlite'))
    record = dict(signal_key='A:1',symbol='A',entry_price=100,quantity=1,opened_at='2026-10-01T00:00:00Z',
                  outcome='TP',exit_price=99,pnl_usdt=-1,pnl_pct=-1,r=-.5,closed_at='2026-10-01T01:00:00Z')
    db.open_trade(record)
    assert db.stats()['open_count'] == 1
    db.backfill_state({'open_positions':{},'realized':[record]})
    assert db.stats()['open_count'] == 0
    assert db.stats()['wins'] == 0 and db.stats()['losses'] == 1
    db.backfill_state({'open_positions':{},'realized':[record]})
    assert db.stats()['closed_count'] == 1
