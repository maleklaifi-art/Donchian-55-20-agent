"""Written-rule regressions: continuous ATR, actual fees, and durable exits."""
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from daily_history import DailyHistory
from demo_trader import (allowed_to_trade, close_position, load_config, load_state,
                         sync_positions)
from execution_accounting import commissions, position_cost
from strategy import DAY_MS, wilders_atr
from test_multiple_positions import (Exchange, NOW, enter, rule, setup, signal)


def rows(count):
    # Early high volatility makes a rolling-window ATR reset detectable.
    return [[i * DAY_MS, 100, 150 if i < 30 else 102, 50 if i < 30 else 98,
             100, 10, (i + 1) * DAY_MS - 1, 1000] for i in range(count)]


def api(source):
    def get(path, params):
        assert path == '/api/v3/klines'
        return [r for r in source if params['startTime'] <= r[0]
                and r[6] <= params['endTime']][:params['limit']]
    return Mock(side_effect=get)


def test_continuous_atr_pagination_restart_and_only_new_closed_bars(tmp_path):
    source = rows(182)  # one still-open candle
    get = api(source)
    cache = DailyHistory(tmp_path)
    bars = cache.fetch(get, 'BTCUSDT', 181 * DAY_MS + 10, 60)
    assert len(bars) == 181 and get.call_count == 4
    assert get.call_args_list[0].args[1]['startTime'] == 0
    assert wilders_atr(bars, 20) > wilders_atr(bars[-120:], 20)
    get.reset_mock()
    same = DailyHistory(tmp_path).fetch(get, 'BTCUSDT', 181 * DAY_MS + 100, 1000)
    assert same == bars and get.call_count == 0
    updated = DailyHistory(tmp_path).fetch(get, 'BTCUSDT', 182 * DAY_MS, 1000)
    assert len(updated) == 182 and get.call_count == 1
    assert get.call_args.args[1]['startTime'] == 181 * DAY_MS
    other = DailyHistory(tmp_path / 'other').fetch(api(source), 'BTCUSDT', 182 * DAY_MS, 1000)
    assert wilders_atr(updated, 20) == wilders_atr(other, 20)


def test_history_gap_cannot_poison_cache_and_corrupt_cache_rebuilds(tmp_path):
    cache = DailyHistory(tmp_path)
    cache.fetch(api(rows(61)), 'BTCUSDT', 61 * DAY_MS)
    before = (tmp_path / 'BTCUSDT.json').read_bytes()
    bad = rows(63)[62:]  # missing day61
    with pytest.raises(ValueError, match='gap'):
        cache.fetch(api(bad), 'BTCUSDT', 63 * DAY_MS)
    assert (tmp_path / 'BTCUSDT.json').read_bytes() == before
    (tmp_path / 'BTCUSDT.json').write_text('broken JSON')
    get = api(rows(63))
    assert len(cache.fetch(get, 'BTCUSDT', 63 * DAY_MS)) == 63
    assert get.call_args_list[0].args[1]['startTime'] == 0


def test_legacy_daily_cutoff_cannot_return_from_config(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'daily_loss_limit_r': 3}))
    assert not hasattr(load_config(str(path)), 'daily_loss_limit_r')


class FeeExchange(Exchange):
    def __init__(self, asset='AAA', commission=.0025, sell_fraction=1):
        super().__init__()
        self.asset, self.commission = asset, commission
        self.held = 0.0
        self.sell_fraction = sell_fraction

    def account(self):
        return {'canTrade': True, 'balances': [
            {'asset': 'USDT', 'free': str(self.free)},
            {'asset': 'AAA', 'free': str(self.held)}]}

    def market_buy_quote(self, symbol, quote, client_id):
        result = super().market_buy_quote(symbol, quote, client_id)
        result['fills'][0].update(commission=str(self.commission), commissionAsset=self.asset)
        self.held = float(result['executedQty']) - (self.commission if self.asset == 'AAA' else 0)
        if self.asset == 'USDT':
            self.free -= self.commission
        return result

    def market_sell_qty(self, symbol, quantity, client_id):
        result = super().market_sell_qty(symbol, str(float(quantity) * self.sell_fraction), client_id)
        result['status'] = 'EXPIRED' if self.sell_fraction < 1 else 'FILLED'
        gross = float(result['cummulativeQuoteQty'])
        result['fills'][0]['commission'] = str(gross * .0025)
        self.free += gross * .9975
        self.held -= float(result['executedQty'])
        return result


def exit_trade(exchange, context, now=NOW):
    cfg, market, state, logger = context
    with patch('demo_trader.time.time', return_value=now / 1000):
        close_position(signal(direction='EXIT'), market, exchange,
                       {'AAAUSDT': rule('AAAUSDT')}, state, cfg, logger)


@pytest.mark.parametrize('asset,fee,expected_qty,expected_cost', [
    ('AAA', .0025, .9975, 90), ('USDT', .225, 1, 90.225), ('BNB', .001, 1, 90.3)])
def test_fees_change_cash_equity_and_net_closed_pnl_once(setup, asset, fee, expected_qty, expected_cost):
    cfg, market, state, logger = setup
    market.best_ask.side_effect = lambda sym: 300 if sym == 'BNBUSDT' else 90
    exchange = FeeExchange(asset, fee)
    legacy = {'signal_key': 'old', 'pnl_usdt': 0, 'r': 0}
    state['realized'].append(deepcopy(legacy))
    enter(signal(), exchange, setup)
    pos = state['open_positions']['AAAUSDT']
    assert pos['quantity'] == pytest.approx(expected_qty)
    assert position_cost(pos) == pytest.approx(expected_cost)
    assert pos['fee_accounting_complete'] is True
    assert 200 - position_cost(pos) + pos['quantity'] * 90 == pytest.approx(200 - fee * (90 if asset == 'AAA' else 300 if asset == 'BNB' else 1))
    exchange.fill_price = 100
    exit_trade(exchange, setup)
    expected_pnl = expected_qty * 100 * .9975 - expected_cost
    assert not state['open_positions']
    assert state['realized'][-1]['pnl_usdt'] == pytest.approx(expected_pnl)
    assert state['realized'][-1]['accounting_basis'] == 'net_fees'
    assert state['realized'][0] == legacy
    assert load_state(cfg.state_file)['realized'] == state['realized']
    exit_trade(exchange, setup)
    assert len(exchange.sells) == 1 and len(state['realized']) == 2


def test_missing_commission_response_uses_order_trades_and_never_assumes_zero(setup):
    cfg, market, state, logger = setup
    exchange = FeeExchange()
    raw_buy = exchange.market_buy_quote
    def buy(*args):
        result = raw_buy(*args)
        result.pop('fills')
        return result
    exchange.market_buy_quote = buy
    exchange.order_trades = Mock(return_value=[{'qty': '1', 'commission': '.0025', 'commissionAsset': 'AAA'}])
    enter(signal(), exchange, setup)
    assert state['open_positions']['AAAUSDT']['quantity'] == .9975
    exchange.order_trades.assert_called_once_with('AAAUSDT', 1)


def test_unknown_fees_preserve_filled_position_but_block_new_entries(setup):
    cfg, market, state, logger = setup
    exchange = FeeExchange()
    exchange.asset = 'BNB'
    market.best_ask.side_effect = lambda sym: float('nan') if sym == 'BNBUSDT' else 90
    enter(signal(), exchange, setup)
    assert exchange.buys and 'AAAUSDT' in load_state(cfg.state_file)['open_positions']
    assert state['open_positions']['AAAUSDT']['fee_accounting_complete'] is False
    assert Path(cfg.kill_switch_file).exists()
    Path(cfg.kill_switch_file).unlink()
    assert not allowed_to_trade(state, cfg, logger)
    # An accounting issue must never prevent a confirmed channel exit.
    exchange.fill_price = 100
    exit_trade(exchange, setup)
    assert not state['open_positions'] and state['realized'][-1]['fee_accounting_complete'] is False


def test_partial_sell_is_retained_and_completed_after_restart_without_double_accounting(setup):
    cfg, market, state, logger = setup
    exchange = FeeExchange(asset='USDT', commission=.225, sell_fraction=.5)
    enter(signal(), exchange, setup)
    exchange.fill_price = 100
    exit_trade(exchange, setup)
    assert state['realized'] == []
    pos = state['open_positions']['AAAUSDT']
    assert pos['quantity'] == .5
    assert position_cost(pos) == pytest.approx(90.225 - 49.875)
    assert pos['pending_exit_signal']['direction'] == 'EXIT'
    # Retry the already-confirmed exit even after its original daily signal is stale.
    state = load_state(cfg.state_file)
    exchange.sell_fraction = 1
    with patch('demo_trader.time.time', return_value=(NOW + DAY_MS) / 1000):
        sync_positions(exchange, state, logger, cfg, market, {'AAAUSDT': rule('AAAUSDT')})
    assert not state['open_positions'] and len(state['realized']) == 1
    assert state['realized'][0]['pnl_usdt'] == pytest.approx(99.75 - 90.225)
    assert state['realized'][0]['quantity'] == 1
    assert exchange.sells[0][2] != exchange.sells[1][2]


def test_scan_entry_priority_uses_list_order_not_thread_completion(tmp_path):
    from test_live_data import FakeMarket, FLAT, daily, scan
    bars = daily(FLAT + [(108, 115, 105, 110)])
    market = FakeMarket({'ZZZUSDT': bars, 'AAAUSDT': bars, 'MMMUSDT': bars})
    # Reverse completed futures to exercise the previously nondeterministic ordering.
    import demo_trader
    with patch.object(demo_trader, 'as_completed', side_effect=lambda fs: list(fs)[::-1]):
        result = scan(market, tmp_path, holding=())
    assert [s['symbol'] for s in result['signals']] == list(market.data)


def test_lost_sell_response_reconciles_after_restart_without_second_order(setup):
    cfg, market, state, logger = setup
    exchange = FeeExchange(asset='USDT', commission=.225)
    enter(signal(), exchange, setup)
    exchange.fill_price = 100
    original_sell = exchange.market_sell_qty
    completed = []
    def lost_response(*args):
        completed.append(original_sell(*args))
        raise TimeoutError('response lost after exchange fill')
    exchange.market_sell_qty = lost_response
    with pytest.raises(TimeoutError):
        exit_trade(exchange, setup)
    assert exchange.held == 0
    state = load_state(cfg.state_file)
    assert state['open_positions']['AAAUSDT']['pending_exit_client_id']
    exchange.order = Mock(return_value=completed[0])
    with patch('demo_trader.time.time', return_value=(NOW + DAY_MS) / 1000):
        sync_positions(exchange, state, logger, cfg, market, {'AAAUSDT': rule('AAAUSDT')})
    assert not state['open_positions'] and len(exchange.sells) == 1
    assert state['realized'][0]['pnl_usdt'] == pytest.approx(9.525)
