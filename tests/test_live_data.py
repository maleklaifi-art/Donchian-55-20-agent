from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import demo_trader
from demo_trader import BotConfig, PublicMarketClient, place_signal, scan_market_detailed
from strategy import Candle, StrategyConfig, evaluate_latest_detailed, is_current_signal_close

HOUR = 3_600_000
NOW = int(datetime(2026, 9, 27, 19, tzinfo=timezone.utc).timestamp() * 1000)


def history(ending_at):
    return [
        Candle(ending_at - (240 - i) * HOUR, 100, 102, 98, 100, 10,
               ending_at - (239 - i) * HOUR - 1, 1000)
        for i in range(240)
    ]


@pytest.mark.parametrize("now", [NOW, NOW + 120_000, NOW + HOUR - 1,
                                 int(datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp() * 1000)])
def test_exact_previous_hour_only(now):
    expected = now // HOUR * HOUR - 1
    assert is_current_signal_close(expected, now)
    for wrong in [expected - HOUR, expected + HOUR, expected - 1, expected + 1]:
        assert not is_current_signal_close(wrong, now)


@pytest.mark.parametrize("end", [NOW - HOUR, int(datetime(2025, 5, 26, 4, tzinfo=timezone.utc).timestamp() * 1000)])
def test_old_data_rejected_before_any_strategy_calculation(end):
    with patch("strategy.latest_prior_nr7_box") as nr7:
        result = evaluate_latest_detailed("EOSUSDT", history(end), StrategyConfig(), NOW)
    assert result.signal is None and result.phase_one is None
    assert result.rejection_stage == "Data"
    assert "Stale candles" in result.rejection_reason
    nr7.assert_not_called()


def test_current_hour_reaches_original_strategy_and_ignores_open_candle():
    rows = history(NOW)
    rows.append(Candle(NOW, 100, 900, 1, 800, 9999, NOW + HOUR - 1, 9999))
    with patch("strategy.latest_prior_nr7_box", return_value=None) as nr7:
        result = evaluate_latest_detailed("TESTUSDT", rows, StrategyConfig(), NOW + 20_000)
    assert result.rejection_stage == "NR7"
    assert len(nr7.call_args.args[0]) == 240


def test_gap_before_breakout_cannot_act_as_previous_candle():
    rows = history(NOW)
    del rows[-2]
    rows.insert(0, Candle(rows[0].open_time - HOUR, 100, 102, 98, 100, 10, rows[0].open_time - 1, 1000))
    result = evaluate_latest_detailed("TESTUSDT", rows, StrategyConfig(), NOW)
    assert result.rejection_stage == "Data"
    assert "consecutive" in result.rejection_reason


def test_universe_filters_status_before_top_n_and_refreshes():
    active = dict(symbol="ACTIVEUSDT", status="TRADING", isSpotTradingAllowed=True, quoteAsset="USDT")
    small = dict(active, symbol="SMALLUSDT")
    stopped = dict(active, symbol="EOSUSDT", status="BREAK")
    halted = dict(active, symbol="HALTUSDT", status="HALT")
    futures = dict(active, symbol="FUTUREUSDT", isSpotTradingAllowed=False)
    wrong_quote = dict(active, symbol="WRONGUSDT", quoteAsset="BTC")
    missing_flag = dict(active, symbol="UNKNOWNUSDT")
    del missing_flag["isSpotTradingAllowed"]
    items = [active, small, stopped, halted, futures, wrong_quote, missing_flag]
    tickers = [dict(symbol=x["symbol"], quoteVolume=10000) for x in items[2:]]
    tickers += [dict(symbol="REMOVEDUSDT", quoteVolume=50000),
                dict(symbol="ACTIVEUSDT", quoteVolume=200),
                dict(symbol="SMALLUSDT", quoteVolume=100)]
    client = PublicMarketClient(1)
    with patch.object(client, "get", side_effect=[{"symbols": items}, tickers,
                                                {"symbols": [small]}, tickers]) as get:
        assert client.symbols("USDT", 1) == ["ACTIVEUSDT"]
        assert client.symbols("USDT", 1000) == ["SMALLUSDT"]
    assert [x.args[0] for x in get.call_args_list].count("/api/v3/exchangeInfo") == 2


def test_no_ticker_fallback_when_exchange_info_fails():
    client = PublicMarketClient(1)
    with patch.object(client, "get", side_effect=RuntimeError("HTTP 500")) as get:
        with pytest.raises(RuntimeError):
            client.symbols("USDT", 1000)
    assert get.call_count == 1


def test_stale_scan_retained_as_data_failure_without_alert_or_daily_fallback(tmp_path):
    market = Mock()
    market.symbols.return_value = ["EOSUSDT"]
    market.klines.return_value = history(NOW - 1000 * HOUR)
    cfg = BotConfig(signal_log=str(tmp_path / "signals.json"))
    callback = Mock()
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        result = scan_market_detailed(market, cfg, StrategyConfig(), Mock(), callback)
    assert result["coins_scanned"] == 1
    assert result["valid_count"] == result["app_confirmed_count"] == 0
    assert result["filtered"][0]["stage"] == "Data"
    market.klines.assert_called_once()
    callback.assert_not_called()


def test_stale_entry_blocked_before_state_write_or_exchange_call(tmp_path):
    signal = Mock(symbol="EOSUSDT", signal_close_ms=NOW - HOUR - 1)
    client, market, logger = Mock(), Mock(), Mock()
    cfg = BotConfig(state_file=str(tmp_path / "state.json"))
    state = {"seen_signals": []}
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        place_signal(signal, market, client, {}, state, cfg, logger)
    assert state == {"seen_signals": []}
    assert not (tmp_path / "state.json").exists()
    assert not client.mock_calls and not market.mock_calls
    assert logger.emit.call_args.kwargs["reason"] == "stale signal candle"


def test_user_lowered_filters_still_work_for_fresh_signal():
    rows = history(NOW)
    rows[-1] = Candle(NOW - HOUR, 106, 118, 105, 110, 20, NOW - 1, 2000)
    cfg = StrategyConfig(app_minimum_surge=0.1, minimum_close_position=0.3)
    with (
        patch("strategy.latest_prior_nr7_box", return_value=(105.0, 105.0)),
        patch("strategy.app_volume_surge", return_value=0.15),
        patch("strategy.is_stablecoin_at_signal", return_value=False),
        patch("strategy.strong_pivot_state", return_value=[(True, 140.0)] * len(rows)),
        patch("strategy.previous_completed_daily", return_value=Candle(0, 105, 115, 105, 110, 1, 0, 1)),
    ):
        result = evaluate_latest_detailed("TESTUSDT", rows, cfg, NOW)
    assert result.signal is not None
    assert result.signal.app_volume_surge == 0.15
    assert 0.38 < result.signal.close_position < 0.39
    assert cfg.app_minimum_surge == 0.1 and cfg.minimum_close_position == 0.3
