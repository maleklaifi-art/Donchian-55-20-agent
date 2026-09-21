from decimal import Decimal
from unittest.mock import patch
from database import TradeDatabase
from demo_trader import (
    BotConfig,
    EventLogger,
    TestnetExecutionClient as BinanceTestnetClient,
    allowed_to_trade,
    floor_step,
    floor_tick,
    ceil_tick,
    deterministic_id,
    scan_market_detailed,
)
from runtime_settings import SettingsStore
import strategy
from strategy import Candle, Evaluation, PhaseOneSignal, Signal, StrategyConfig, evaluate_latest, previous_completed_daily, reward_risk_is_accepted


def test_rounding():
    assert floor_step(1.23456, Decimal("0.001")) == Decimal("1.234")
    assert floor_tick(10.129, Decimal("0.01")) == Decimal("10.12")
    assert ceil_tick(10.121, Decimal("0.01")) == Decimal("10.13")


def test_ids_are_deterministic():
    assert deterministic_id("nr7e", "ABC") == deterministic_id("nr7e", "ABC")
    assert deterministic_id("nr7e", "ABC") != deterministic_id("nr7e", "ABD")


def test_reward_risk_boundaries_are_inclusive():
    assert not reward_risk_is_accepted(1.9999999)
    assert reward_risk_is_accepted(2.0)
    assert reward_risk_is_accepted(3.0)
    assert reward_risk_is_accepted(4.0)
    assert not reward_risk_is_accepted(4.0000001)


def test_display_diagnostics_preserve_app_and_indicator_values():
    phase_one = PhaseOneSignal("TESTUSDT", 1, "2026-09-21T00:59:59.999Z", 10, 9, 9.5, 8, 2.1)
    evaluation = Evaluation(
        "TESTUSDT", 10, phase_one, None, "Indicator Volume", "1.2x < 1.5x",
        indicator_volume_surge=1.2,
    )
    row = evaluation.filtered_dict()
    assert row["phase_one_passed"] is True
    assert row["app_volume_surge"] == 2.1
    assert row["indicator_volume_surge"] == 1.2


def test_display_categories_are_filtered_confirmed_and_passed(tmp_path):
    class FakeMarket:
        def symbols(self, quote_asset, maximum):
            return ["FILTEREDUSDT", "CONFIRMEDUSDT", "PASSEDUSDT"]

        def klines(self, symbol, interval, limit):
            return []

    confirmed = PhaseOneSignal("CONFIRMEDUSDT", 1, "2026-09-21T00:59:59.999Z", 10, 9, 9.5, 8, 2.1)
    passed_phase = PhaseOneSignal("PASSEDUSDT", 2, "2026-09-21T00:59:59.999Z", 10, 9, 9.5, 8, 2.2)
    passed_signal = Signal(
        "PASSEDUSDT", 2, "2026-09-21T00:59:59.999Z", 10, 12, 9, 11,
        13, 2, 9.5, 9, 2.2, 1.8, 0.8, 75, "MEDIUM",
    )
    results = {
        "FILTEREDUSDT": Evaluation("FILTEREDUSDT", 1.0, rejection_stage="NR7", rejection_reason="No completed NR7 box"),
        "CONFIRMEDUSDT": Evaluation("CONFIRMEDUSDT", 10, confirmed, rejection_stage="Strong High", rejection_reason="Not active"),
        "PASSEDUSDT": Evaluation("PASSEDUSDT", 10, passed_phase, passed_signal),
    }

    def evaluate(symbol, candles, config, now_ms):
        return results[symbol]

    cfg = BotConfig(scan_workers=1, signal_log=str(tmp_path / "signals.json"), event_log=str(tmp_path / "events.jsonl"))
    with patch("demo_trader.evaluate_latest_detailed", side_effect=evaluate):
        snapshot = scan_market_detailed(FakeMarket(), cfg, StrategyConfig(), EventLogger(cfg.event_log))

    assert snapshot["coins_scanned"] == 3
    assert snapshot["valid_count"] == 1
    assert snapshot["app_confirmed_count"] == 2
    assert snapshot["indicator_rejected_count"] == 1
    assert [row["symbol"] for row in snapshot["filtered"]] == ["FILTEREDUSDT"]
    assert snapshot["filtered"][0]["stage"] == "NR7"
    assert {row["symbol"] for row in snapshot["phase_one"]} == {"CONFIRMEDUSDT", "PASSEDUSDT"}
    assert snapshot["phase_one"][0]["status"] in {"PASSED", "INDICATOR_NOT_PASSED"}


def test_fast_default_and_exact_hour_schedule_label():
    settings = SettingsStore("/path/that/does/not/exist/settings.json").get()
    assert settings.scan_workers == 100
    assert settings.public_dict()["schedule"] == "Hourly at HH:00 UTC"


def test_existing_default_worker_setting_migrates_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"scan_workers": 8}', encoding="utf-8")
    migrated = SettingsStore(str(path)).get()
    assert migrated.scan_workers == 100
    assert migrated.settings_revision == 3

    store = SettingsStore(str(path))
    store.update({"scan_workers": 8})
    assert SettingsStore(str(path)).get().scan_workers == 8


def test_previous_fast_worker_default_migrates_to_100_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"settings_revision": 2, "scan_workers": 50}', encoding="utf-8")
    migrated = SettingsStore(str(path)).get()
    assert migrated.scan_workers == 100
    assert migrated.settings_revision == 3

    store = SettingsStore(str(path))
    store.update({"scan_workers": 50})
    assert SettingsStore(str(path)).get().scan_workers == 50


def test_previous_completed_daily_uses_immediately_prior_utc_day():
    hour = 3_600_000
    day = 86_400_000
    candles = []
    for d in range(3):
        for h in range(24):
            open_time = d * day + h * hour
            candles.append(Candle(open_time, 100 + d, 110 + d, 90 + d, 105 + d, 1, open_time + hour - 1, 10))
    signal_index = len(candles) - 1
    previous = previous_completed_daily(candles, signal_index)
    assert previous is not None
    assert previous.open_time == day
    assert previous.high == 111
    assert previous.low == 91
    assert previous.close == 106


def test_evaluate_latest_uses_pivot_r2_tp_and_accepts_both_rr_boundaries():
    hour = 3_600_000
    candles = []
    for i in range(240):
        open_time = i * hour
        candles.append(Candle(open_time, 100, 101, 99, 100, 1, open_time + hour - 1, 10))
    open_time = 240 * hour
    candles.append(Candle(open_time, 106, 111, 105, 110, 2, open_time + hour - 1, 20))
    now_ms = candles[-1].close_time + 1

    for previous_daily, expected_tp, expected_rr in [
        (Candle(0, 105, 115, 105, 110, 1, 0, 1), 120.0, 2.0),
        (Candle(0, 110, 120, 100, 110, 1, 0, 1), 130.0, 4.0),
    ]:
        with (
            patch.object(strategy, "latest_prior_nr7_box", return_value=(105.0, 105.0)),
            patch.object(strategy, "app_volume_surge", return_value=2.0),
            patch.object(strategy, "is_stablecoin_at_signal", return_value=False),
            patch.object(strategy, "strong_pivot_state", return_value=[(True, 140.0)] * len(candles)),
            patch.object(strategy, "previous_completed_daily", return_value=previous_daily),
        ):
            signal = evaluate_latest("TESTUSDT", candles, StrategyConfig(), now_ms)
        assert signal is not None
        assert signal.tp == expected_tp
        assert signal.rr == expected_rr
        assert signal.strong_high_filter == 140.0


def test_closed_signal_values_are_frozen_during_following_hour():
    hour = 3_600_000
    candles = []
    for i in range(240):
        open_time = i * hour
        candles.append(Candle(open_time, 100, 101, 99, 100, 1, open_time + hour - 1, 10))
    breakout_open = 240 * hour
    breakout = Candle(breakout_open, 106, 111, 105, 110, 2, breakout_open + hour - 1, 20)
    live = Candle(241 * hour, 110, 999, 1, 500, 999, 242 * hour - 1, 9999)
    candles.extend([breakout, live])
    previous_daily = Candle(0, 105, 115, 105, 110, 1, 0, 1)
    with (
        patch.object(strategy, "latest_prior_nr7_box", return_value=(105.0, 105.0)),
        patch.object(strategy, "app_volume_surge", return_value=2.0),
        patch.object(strategy, "is_stablecoin_at_signal", return_value=False),
        patch.object(strategy, "strong_pivot_state", return_value=[(True, 140.0)] * len(candles)),
        patch.object(strategy, "previous_completed_daily", return_value=previous_daily),
    ):
        at_0100 = evaluate_latest("TESTUSDT", candles, StrategyConfig(), breakout.close_time + 1)
        at_0102 = evaluate_latest("TESTUSDT", candles, StrategyConfig(), breakout.close_time + 120_001)
    assert at_0100 is not None
    assert at_0102 is not None
    assert at_0100.to_dict() == at_0102.to_dict()


def test_settings_are_persisted_and_validate_rr_order(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(str(path))
    value = store.update({"top_n": 777, "minimum_reward_risk": 2.0, "maximum_reward_risk": 4.0})
    assert value.top_n == 777
    assert SettingsStore(str(path)).get().top_n == 777
    try:
        store.update({"minimum_reward_risk": 4.1, "maximum_reward_risk": 4.0})
        assert False, "invalid R:R order should fail"
    except ValueError:
        pass


def test_one_trade_per_utc_day_gate(tmp_path):
    cfg = BotConfig(kill_switch_file=str(tmp_path / "STOP"))
    logger = EventLogger(str(tmp_path / "events.jsonl"))
    state = {
        "open_positions": {},
        "realized": [{"opened_at": strategy.datetime.now(strategy.timezone.utc).isoformat(), "r": 2.0}],
    }
    assert not allowed_to_trade(state, cfg, logger)


def test_filled_emergency_entry_still_consumes_daily_trade_cap(tmp_path):
    cfg = BotConfig(kill_switch_file=str(tmp_path / "STOP"))
    logger = EventLogger(str(tmp_path / "events.jsonl"))
    state = {
        "executed_entries": [{
            "signal_key": "TESTUSDT:1",
            "opened_at": strategy.datetime.now(strategy.timezone.utc).isoformat(),
        }],
        "open_positions": {},
        "realized": [],
    }
    assert not allowed_to_trade(state, cfg, logger)


def test_current_binance_oco_request_shape():
    client = object.__new__(BinanceTestnetClient)
    with patch.object(BinanceTestnetClient, "_signed", return_value={"orderListId": 1}) as signed:
        response = client.sell_oco_market_triggers("TESTUSDT", "1", "120", "90", "nr7-list")
    assert response["orderListId"] == 1
    method, path, params = signed.call_args.args
    assert method == "POST"
    assert path == "/api/v3/orderList/oco"
    assert params["aboveType"] == "TAKE_PROFIT"
    assert params["aboveStopPrice"] == "120"
    assert params["belowType"] == "STOP_LOSS"
    assert params["belowStopPrice"] == "90"


def test_dashboard_database_open_close_lifecycle(tmp_path):
    db = TradeDatabase(str(tmp_path / "trades.sqlite3"))
    now = strategy.datetime.now(strategy.timezone.utc).isoformat()
    signal = {
        "symbol": "TESTUSDT", "signal_close_ms": 1, "close_time_utc": now,
        "entry": 100.0, "tp": 120.0, "sl": 90.0, "rr": 2.0,
        "box_high": 99.0, "box_low": 90.0, "classic_pivot": 110.0,
        "strong_high_filter": 125.0, "app_volume_surge": 2.0,
        "indicator_volume_surge": 2.0, "close_position": 0.8,
        "confidence_score": 75, "confidence_level": "MEDIUM",
    }
    db.upsert_signal(signal)
    db.open_trade({
        "signal_key": "TESTUSDT:1", "symbol": "TESTUSDT", "entry_price": 100.0,
        "quantity": 0.2, "tp": 120.0, "sl": 90.0, "rr": 2.0,
        "risk_usdt": 2.0, "opened_at": now,
    })
    assert db.stats()["open_count"] == 1
    db.close_trade({
        "signal_key": "TESTUSDT:1", "outcome": "TP", "exit_price": 120.0,
        "quantity": 0.2, "pnl_usdt": 4.0, "pnl_pct": 20.0,
        "realized_r": 2.0, "closed_at": now,
    })
    stats = db.stats()
    assert stats["open_count"] == 0
    assert stats["wins"] == 1
    assert stats["realized_pnl_usdt"] == 4.0
    assert stats["realized_r"] == 2.0
