from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from demo_trader import (
    BotConfig,
    EXCLUDED_SYMBOLS,
    SymbolRules,
    TestnetExecutionClient as BinanceTestnetClient,
    allowed_to_trade,
    close_position,
    initial_state,
    load_config,
    load_state,
    place_signal,
    today_utc,
)
from strategy import DAY_MS, Signal

BOUNDARY = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp() * 1000
NOW = int(BOUNDARY) + 60_000  # 00:01 UTC, inside the 15 minute entry grace window


def signal(symbol="AAAUSDT", atr=1.0, direction="LONG", close_ms=None, price=90.0):
    return Signal(
        symbol=symbol,
        signal_close_ms=int(close_ms if close_ms is not None else BOUNDARY - 1),
        close_time_utc="2026-09-27T23:59:59.999Z",
        direction=direction,
        entry=price,
        entry_channel=85.0,
        exit_channel=80.0,
        atr=atr,
        stop_distance=2.0 * atr,
        universe="Test",
        strategy_version="donchian_55_20_v1",
    )


def rule(symbol):
    return SymbolRules(symbol, symbol[:-4], "USDT", Decimal("0.01"),
                       Decimal("0.000001"), Decimal("0.000001"),
                       Decimal("1000000"), Decimal("5"), True)


class Exchange:
    def __init__(self, free=200.0, fill_price=90.0):
        self.free, self.fill_price = free, fill_price
        self.buys, self.sells = [], []

    def account(self):
        return {"canTrade": True, "balances": [{"asset": "USDT", "free": str(self.free)}]}

    def market_buy_quote(self, symbol, quote, client_id):
        cost = float(quote)
        assert 0 < cost <= self.free
        self.free -= cost
        self.buys.append((symbol, cost))
        return {"orderId": len(self.buys), "executedQty": str(cost / self.fill_price),
                "cummulativeQuoteQty": str(cost), "fills": []}

    def market_sell_qty(self, symbol, quantity, client_id):
        self.sells.append((symbol, quantity, client_id))
        return {"orderId": 900 + len(self.sells), "status": "FILLED",
                "executedQty": quantity,
                "cummulativeQuoteQty": str(float(quantity) * self.fill_price)}


@pytest.fixture
def setup(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market = Mock()
    market.best_ask.return_value = 90.0
    return cfg, market, initial_state(), Mock()


def enter(sig, exchange, setup):
    cfg, market, state, logger = setup
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        place_signal(sig, market, exchange, {sig.symbol: rule(sig.symbol)}, state, cfg, logger)


def test_spec_sizing_formula_risk_budget_over_two_atr(setup):
    exchange = Exchange()
    enter(signal(atr=1.0), exchange, setup)
    # equity 200 x 1% = 2 USDT risk; stop distance 2 x ATR(1) = 2; qty = 1; notional = 90
    assert exchange.buys == [("AAAUSDT", 90.0)]
    assert setup[2]["open_positions"]["AAAUSDT"]["risk_usdt"] == pytest.approx(2.0)


def test_larger_atr_produces_a_smaller_position_with_the_same_risk(setup):
    exchange = Exchange()
    enter(signal(atr=10.0), exchange, setup)
    assert exchange.buys == [("AAAUSDT", 9.0)]
    assert setup[2]["open_positions"]["AAAUSDT"]["risk_usdt"] == pytest.approx(2.0)


@pytest.mark.parametrize("atr,cap,expected", [(1.0, 100, 90.0), (1.0, 25, 25.0),
                                              (10.0, 100, 9.0), (0.1, 100, 100.0)])
def test_absolute_notional_ceiling_caps_the_formula(setup, atr, cap, expected):
    setup[0].max_position_notional_usdt = cap
    exchange = Exchange()
    enter(signal(atr=atr), exchange, setup)
    assert exchange.buys == [("AAAUSDT", pytest.approx(expected))]
    pos = setup[2]["open_positions"]["AAAUSDT"]
    assert 0 < pos["risk_usdt"] <= 2.0


def test_multiple_positions_have_independent_risk_and_shared_cash_caps(setup):
    exchange = Exchange()
    enter(signal("AAAUSDT"), exchange, setup)
    enter(signal("BBBUSDT"), exchange, setup)
    cfg, _, state, _ = setup
    assert exchange.buys == [("AAAUSDT", 90.0), ("BBBUSDT", 90.0)]
    assert exchange.free == 20.0
    assert len(state["open_positions"]) == 2
    assert [p["risk_usdt"] for p in state["open_positions"].values()] == pytest.approx([2.0, 2.0])
    assert len(state["executed_entries"]) == 2
    assert load_state(cfg.state_file) == state
    enter(signal("CCCUSDT"), exchange, setup)
    assert exchange.buys[-1][1] == pytest.approx(19.9)
    assert sum(cost for _, cost in exchange.buys) < 200


@pytest.mark.parametrize("symbol", sorted(EXCLUDED_SYMBOLS))
def test_excluded_signal_cannot_submit_or_touch_state(setup, symbol):
    setup[2]["realized"] = [{"signal_key": "old", "pnl_usdt": 7}]
    before = deepcopy(setup[2])
    exchange = Exchange()
    enter(signal(symbol), exchange, setup)
    assert not exchange.buys and not exchange.sells
    setup[1].best_ask.assert_not_called()
    assert setup[2] == before


@pytest.mark.parametrize("free,open_cost,expected", [(10000, 190, 9.95), (7, 90, 6.965)])
def test_new_order_uses_free_balance_and_remaining_demo_bankroll(setup, free, open_cost, expected):
    setup[2]["open_positions"]["OLDUSDT"] = {"entry_price": open_cost, "quantity": 1, "risk_usdt": 2}
    exchange = Exchange(free)
    enter(signal(), exchange, setup)
    assert exchange.buys[0][1] == pytest.approx(expected)


@pytest.mark.parametrize("free,open_cost", [(0, 90), (10000, 200), (4, 0), (10000, 199)])
def test_no_order_when_balance_cannot_fund_exchange_minimum(setup, free, open_cost):
    setup[2]["open_positions"]["OLDUSDT"] = {"entry_price": open_cost, "quantity": 1, "risk_usdt": 2}
    exchange = Exchange(free)
    enter(signal(), exchange, setup)
    assert exchange.buys == []


def test_duplicate_signal_and_existing_symbol_still_blocked(setup):
    exchange = Exchange()
    enter(signal(), exchange, setup)
    enter(signal(), exchange, setup)
    setup[2]["seen_signals"] = []
    enter(signal(), exchange, setup)
    assert len(exchange.buys) == 1


def test_stale_signal_is_rejected_before_state_write_or_exchange_call(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market, client, logger = Mock(), Mock(), Mock()
    state = {"seen_signals": []}
    sig = signal(close_ms=int(BOUNDARY) - 1 - DAY_MS)
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        place_signal(sig, market, client, {}, state, cfg, logger)
    assert state == {"seen_signals": []}
    assert not (tmp_path / "state.json").exists()
    assert not client.mock_calls and not market.mock_calls
    assert logger.emit.call_args.kwargs["reason"] == "stale signal candle"


def test_entry_outside_the_grace_window_is_rejected(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market, client, logger = Mock(), Mock(), Mock()
    state = {"seen_signals": []}
    late = int(BOUNDARY) + 16 * 60_000
    with patch("demo_trader.time.time", return_value=late / 1000):
        place_signal(signal(), market, client, {}, state, cfg, logger)
    assert state == {"seen_signals": []}
    assert not client.mock_calls
    assert logger.emit.call_args.kwargs["reason"] == "entry grace window expired"


def test_exit_direction_is_never_submitted_as_an_entry(setup):
    exchange = Exchange()
    enter(signal(direction="EXIT"), exchange, setup)
    assert exchange.buys == []


def test_legacy_config_cannot_reinstate_removed_strategy_knobs(setup, tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('{"risk_per_trade_usdt":20,"max_open_positions":1,'
                    '"max_trades_per_utc_day":1,"max_entry_drift_pct":0.1,"top_n":5}')
    cfg = load_config(str(path))
    cfg.state_file, cfg.kill_switch_file = setup[0].state_file, setup[0].kill_switch_file
    context = (cfg, *setup[1:])
    exchange = Exchange()
    enter(signal("AAAUSDT"), exchange, context)
    enter(signal("BBBUSDT"), exchange, context)
    assert len(context[2]["open_positions"]) == 2
    assert all(p["risk_usdt"] <= 2 for p in context[2]["open_positions"].values())


def test_kill_switch_and_daily_loss_stop_still_apply(setup):
    cfg, _, state, logger = setup
    state["realized"] = [{"day": today_utc(), "r": -3}]
    assert not allowed_to_trade(state, cfg, logger)
    state["realized"] = []
    Path(cfg.kill_switch_file).touch()
    assert not allowed_to_trade(state, cfg, logger)


@pytest.mark.parametrize("pnl,open_cost,expected", [(10, 200, 9.95), (-10, 180, 9.95),
                                                    (-210, 0, None)])
def test_realized_pnl_changes_cash_available_for_next_entry(setup, pnl, open_cost, expected):
    setup[2]["realized"] = [{"pnl_usdt": pnl}]
    if open_cost:
        setup[2]["open_positions"]["OLDUSDT"] = {"entry_price": open_cost, "quantity": 1}
    exchange = Exchange(free=10000)
    enter(signal(), exchange, setup)
    if expected is None:
        assert exchange.buys == []
    else:
        assert exchange.buys[0][1] == pytest.approx(expected)


def test_lowering_capital_preserves_existing_position_and_blocks_unfunded_entry(setup):
    exchange = Exchange()
    enter(signal(), exchange, setup)
    original = deepcopy(setup[2]["open_positions"])
    setup[0].demo_equity_cap_usdt = 50
    enter(signal("BBBUSDT"), exchange, setup)
    assert len(exchange.buys) == 1
    assert setup[2]["open_positions"] == original


def test_database_realized_total_is_used_for_cloud_order_budget(setup):
    setup[2]["open_positions"]["OLDUSDT"] = {"entry_price": 200, "quantity": 1}
    cfg, market, state, logger = setup
    exchange = Exchange(free=10000)
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        place_signal(signal(), market, exchange, {"AAAUSDT": rule("AAAUSDT")}, state,
                     cfg, logger, realized_pnl_usdt=10)
    assert exchange.buys[0][1] == pytest.approx(9.95)


def position_state(symbol="AAAUSDT"):
    return {
        "symbol": symbol, "signal_key": f"{symbol}:1", "entry_order_id": 1,
        "entry_client_order_id": "dce1", "entry_price": 90.0, "quantity": 1.0,
        "risk_usdt": 2.0, "opened_at": "2026-09-27T00:00:00+00:00",
        "atr": 1.0, "exit_channel": 80.0, "entry_channel": 85.0,
    }


def test_channel_exit_flattens_once_and_records_realized_pnl(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market = Mock()
    market.best_ask.return_value = 79.0
    logger = Mock()
    state = initial_state()
    state["open_positions"]["AAAUSDT"] = position_state()
    exchange = Exchange(free=200.0)
    # The fake fills the sell at 79 with the base balance present.
    exchange.account = lambda: {"canTrade": True, "balances": [
        {"asset": "USDT", "free": "110"}, {"asset": "AAA", "free": "1.0"}]}
    exchange.fill_price = 79.0
    exit_sig = signal(direction="EXIT", price=79.0)
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        close_position(exit_sig, market, exchange, {"AAAUSDT": rule("AAAUSDT")}, state, cfg, logger)
    assert state["open_positions"] == {}
    assert len(exchange.sells) == 1
    record = state["realized"][0]
    assert record["outcome"] == "CHANNEL_EXIT"
    assert record["signal_key"] == "AAAUSDT:1"
    assert record["exit_signal_key"] == f"AAAUSDT:{int(BOUNDARY) - 1}"
    assert record["pnl_usdt"] == pytest.approx(-11.0)
    assert record["r"] == pytest.approx(-5.5)
    assert load_state(cfg.state_file)["realized"] == state["realized"]

    # A second exit signal for the same (now closed) position must do nothing.
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        close_position(exit_sig, market, exchange, {"AAAUSDT": rule("AAAUSDT")}, state, cfg, logger)
    assert len(exchange.sells) == 1
    assert len(state["realized"]) == 1
    assert logger.emit.call_args_list[-1].kwargs["reason"] == "no open position"


def test_exit_is_rejected_when_the_signal_candle_is_stale(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market, exchange, logger = Mock(), Exchange(), Mock()
    state = initial_state()
    state["open_positions"]["AAAUSDT"] = position_state()
    stale = signal(direction="EXIT", price=79.0, close_ms=int(BOUNDARY) - 1 - DAY_MS)
    with patch("demo_trader.time.time", return_value=NOW / 1000):
        close_position(stale, market, exchange, {"AAAUSDT": rule("AAAUSDT")}, state, cfg, logger)
    assert exchange.sells == []
    assert "AAAUSDT" in state["open_positions"]
    assert logger.emit.call_args.kwargs["reason"] == "stale exit signal"


def test_no_stop_or_protective_orders_are_ever_created(setup):
    exchange = Exchange()
    enter(signal(), exchange, setup)
    assert exchange.sells == []
    assert len(setup[2]["open_positions"]) == 1
    # The real execution client has no OCO/stop helper at all.
    assert not hasattr(BinanceTestnetClient, "sell_oco_market_triggers")
