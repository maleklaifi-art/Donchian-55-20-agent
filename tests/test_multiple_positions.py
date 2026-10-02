from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from demo_trader import (BotConfig, SymbolRules, allowed_to_trade, initial_state,
                         load_config, load_state, place_signal, today_utc)
from strategy import Signal

NOW = int(datetime(2026, 9, 28, 11, tzinfo=timezone.utc).timestamp() * 1000)


class Exchange:
    def __init__(self, free=200.0, fill_price=90.0, fail_oco=False):
        self.free, self.fill_price, self.fail_oco = free, fill_price, fail_oco
        self.buys, self.protections, self.sells = [], [], []

    def account(self):
        return {'canTrade': True, 'balances': [{'asset': 'USDT', 'free': str(self.free)}]}

    def market_buy_quote(self, symbol, quote, client_id):
        cost = float(quote)
        assert 0 < cost <= self.free
        self.free -= cost
        self.buys.append((symbol, cost))
        return {'orderId': len(self.buys), 'executedQty': str(cost / self.fill_price),
                'cummulativeQuoteQty': str(cost), 'fills': []}

    def sell_oco_market_triggers(self, *args):
        if self.fail_oco:
            raise RuntimeError('test protection failure')
        self.protections.append(args)
        return {'orderListId': len(self.protections), 'orderReports': [
            {'type': 'TAKE_PROFIT', 'orderId': 100}, {'type': 'STOP_LOSS', 'orderId': 101}]}

    def market_sell_qty(self, *args):
        self.sells.append(args)


def signal(symbol='AAAUSDT', sl=88.0):
    return Signal(symbol, NOW - 1, '2026-09-28T10:59:59.999Z',
                  90.0, 90.0 + 3 * (90.0 - sl), sl, 92.0, 100.0, 3.0,
                  89.0, sl, 2.0, 2.0, 0.8, 80, 'HIGH')


def rule(symbol):
    return SymbolRules(symbol, symbol[:-4], 'USDT', Decimal('0.01'),
                       Decimal('0.000001'), Decimal('0.000001'),
                       Decimal('1000000'), Decimal('5'), True)


@pytest.fixture
def setup(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / 'state.json'), kill_switch_file=str(tmp_path / 'STOP'))
    market = Mock()
    market.best_ask.return_value = 90.0
    return cfg, market, initial_state(), Mock()


def enter(sig, exchange, setup):
    cfg, market, state, logger = setup
    with patch('demo_trader.time.time', return_value=NOW / 1000):
        place_signal(sig, market, exchange, {sig.symbol: rule(sig.symbol)}, state, cfg, logger)


def test_multiple_20_usdt_positions_have_independent_risk_and_cash_caps(setup):
    exchange = Exchange()
    enter(signal('AAAUSDT'), exchange, setup)
    enter(signal('BBBUSDT'), exchange, setup)
    cfg, _, state, _ = setup
    assert exchange.buys == [('AAAUSDT', 20.0), ('BBBUSDT', 20.0)]
    assert exchange.free == 160.0
    assert len(state['open_positions']) == len(exchange.protections) == 2
    assert [p['risk_usdt'] for p in state['open_positions'].values()] == pytest.approx([0.444444, 0.444444])
    assert len(state['executed_entries']) == 2
    assert load_state(cfg.state_file) == state
    enter(signal('CCCUSDT'), exchange, setup)
    assert exchange.buys[-1][1] == pytest.approx(20.0)
    assert sum(cost for _, cost in exchange.buys) < 200
    assert all(0 < p['risk_usdt'] <= 2 for p in state['open_positions'].values())


@pytest.mark.parametrize('free,open_cost', [(10000, 190), (7, 90)])
def test_new_order_uses_free_balance_and_remaining_demo_bankroll(setup, free, open_cost):
    setup[2]['open_positions']['OLDUSDT'] = {'entry_price': open_cost, 'quantity': 1, 'risk_usdt': 2}
    exchange = Exchange(free)
    enter(signal(), exchange, setup)
    assert exchange.buys[0][1] == pytest.approx(min(free, 200 - open_cost) * 0.995)


@pytest.mark.parametrize('free,open_cost', [(0, 90), (10000, 200), (4, 0), (10000, 199)])
def test_no_order_when_balance_cannot_fund_exchange_minimum(setup, free, open_cost):
    setup[2]['open_positions']['OLDUSDT'] = {'entry_price': open_cost, 'quantity': 1, 'risk_usdt': 2}
    exchange = Exchange(free)
    enter(signal(), exchange, setup)
    assert exchange.buys == []


def test_duplicate_signal_and_existing_symbol_still_blocked(setup):
    exchange = Exchange()
    enter(signal(), exchange, setup)
    enter(signal(), exchange, setup)
    setup[2]['seen_signals'] = []
    enter(signal(), exchange, setup)
    assert len(exchange.buys) == 1


@pytest.mark.parametrize('fill_price,fail_oco', [(96.0, False), (90.0, True)])
def test_fill_risk_and_protection_failures_still_flatten(setup, fill_price, fail_oco):
    exchange = Exchange(fill_price=fill_price, fail_oco=fail_oco)
    enter(signal(), exchange, setup)
    assert len(exchange.buys) == len(exchange.sells) == 1
    assert setup[2]['open_positions'] == {}


def test_legacy_config_cannot_reinstate_removed_caps_or_raise_absolute_risk(setup, tmp_path):
    path = tmp_path / 'legacy.json'
    path.write_text('{"risk_per_trade_usdt":20,"max_open_positions":1,"max_trades_per_utc_day":1,"max_total_open_risk_usdt":2}')
    cfg = load_config(str(path))
    cfg = replace(cfg, state_file=setup[0].state_file, kill_switch_file=setup[0].kill_switch_file)
    context = (cfg, *setup[1:])
    exchange = Exchange()
    enter(signal('AAAUSDT'), exchange, context)
    enter(signal('BBBUSDT'), exchange, context)
    assert len(context[2]['open_positions']) == 2
    assert all(p['risk_usdt'] <= 2 for p in context[2]['open_positions'].values())


def test_kill_switch_and_existing_daily_loss_stop_still_apply(setup):
    cfg, _, state, logger = setup
    state['realized'] = [{'day': today_utc(), 'r': -3}]
    assert not allowed_to_trade(state, cfg, logger)
    state['realized'] = []
    Path(cfg.kill_switch_file).touch()
    assert not allowed_to_trade(state, cfg, logger)

