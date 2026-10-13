from datetime import datetime, timezone
from decimal import Decimal

import pytest
from database import TradeDatabase
from demo_trader import (
    BotConfig,
    EventLogger,
    TestnetExecutionClient as BinanceTestnetClient,
    allowed_to_trade,
    deterministic_id,
    floor_step,
    floor_tick,
    ceil_tick,
)
from runtime_settings import SettingsStore
from strategy import (
    DAY_MS,
    DEFAULT_UNIVERSE_SYMBOLS,
    Candle,
    DonchianConfig,
    evaluate_donchian,
    is_current_signal_close,
    wilders_atr,
    within_entry_grace,
)


def daily(specs, start_day=0):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        open_time = (start_day + i) * DAY_MS
        rows.append(Candle(open_time, o, h, l, c, 10.0, open_time + DAY_MS - 1, 1000.0))
    return rows


FLAT = [(100, 101, 99, 100)] * 60
N_DAYS = 61
BOUNDARY = N_DAYS * DAY_MS
NOW_MS = BOUNDARY + 30_000
CFG = DonchianConfig(adx_threshold=-1)


def test_rounding():
    assert floor_step(1.23456, Decimal("0.001")) == Decimal("1.234")
    assert floor_tick(10.129, Decimal("0.01")) == Decimal("10.12")
    assert ceil_tick(10.121, Decimal("0.01")) == Decimal("10.13")


def test_ids_are_deterministic():
    assert deterministic_id("dce", "ABC") == deterministic_id("dce", "ABC")
    assert deterministic_id("dce", "ABC") != deterministic_id("dce", "ABD")


def test_signal_close_is_exactly_the_daily_boundary():
    for extra in [0, 30_000, 3_600_000 - 1]:
        now = BOUNDARY + extra
        assert is_current_signal_close(BOUNDARY - 1, now)
        for wrong in [BOUNDARY - 1 - DAY_MS, BOUNDARY - 1 + DAY_MS, BOUNDARY - 2, BOUNDARY]:
            assert not is_current_signal_close(wrong, now)


def test_entry_grace_window_is_bounded_by_minutes():
    assert within_entry_grace(BOUNDARY, 15)
    assert within_entry_grace(BOUNDARY + 15 * 60_000, 15)
    assert not within_entry_grace(BOUNDARY + 15 * 60_000 + 1, 15)
    assert not within_entry_grace(BOUNDARY - 1, 15)


def test_wilders_atr_uses_sma_seed_then_wilder_smoothing():
    candles = daily([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (108, 115, 105, 110)])
    # True ranges: 2, 2, 2, then max(10, |115-100|, |105-100|) = 15
    # SMA seed of first 3 = 2.0, then (2*2 + 15)/3 = 19/3
    assert wilders_atr(candles, 3) == pytest.approx(19 / 3)
    assert wilders_atr(candles, 10) is None


def test_breakout_entry_is_long_with_channels_from_prior_days_only():
    rows = daily(FLAT + [(108, 115, 105, 110)])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS, holding=False, universe="Core 10")
    assert result.rejection_stage is None and result.signal is not None
    sig = result.signal
    assert sig.direction == "LONG"
    assert sig.entry == 110.0
    # No lookahead: the signal day's own high of 115 never enters the channel.
    assert sig.entry_channel == 101.0
    assert sig.exit_channel == 99.0
    assert sig.signal_close_ms == BOUNDARY - 1
    assert sig.stop_distance == pytest.approx(2.0 * sig.atr)
    assert sig.atr > 0
    assert sig.universe == "Core 10"
    assert sig.strategy_version == "donchian_55_20_adx25_atr_stop_v2"


def test_close_equal_to_channel_high_does_not_break_out():
    rows = daily(FLAT + [(100, 101, 99, 100)])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS)
    assert result.signal is None
    assert result.rejection_stage == "Entry"
    assert "did not exceed" in result.rejection_reason


def test_breakout_outside_the_grace_window_is_rejected():
    rows = daily(FLAT + [(108, 115, 105, 110)])
    late = BOUNDARY + 16 * 60_000
    result = evaluate_donchian("TESTUSDT", rows, CFG, late)
    assert result.signal is None
    assert result.rejection_stage == "Entry"
    assert "entry window" in result.rejection_reason


def test_held_position_exits_when_close_breaks_the_20_day_low():
    rows = daily(FLAT + [(97, 98, 94, 95)])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS, holding=True)
    assert result.signal is not None
    assert result.signal.direction == "EXIT"
    assert result.signal.exit_channel == 99.0
    assert result.signal.entry == 95.0
    assert result.signal.signal_close_ms == BOUNDARY - 1


def test_held_position_keeps_it_when_close_stays_above_the_channel():
    rows = daily(FLAT + [(100, 101, 99, 100)])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS, holding=True)
    assert result.signal is None
    assert result.rejection_stage == "Held"
    assert result.holding is True


def test_held_position_exit_never_requires_the_entry_grace_window():
    rows = daily(FLAT + [(97, 98, 94, 95)])
    late = BOUNDARY + 8 * 3_600_000
    result = evaluate_donchian("TESTUSDT", rows, CFG, late, holding=True)
    assert result.signal is not None and result.signal.direction == "EXIT"


def test_too_little_history_is_a_data_rejection():
    rows = daily(FLAT[:30])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS)
    assert result.signal is None
    assert result.rejection_stage == "Data"
    assert "completed daily candles" in result.rejection_reason


def test_stale_daily_candles_rejected_before_any_calculation(monkeypatch):
    rows = daily(FLAT + [(108, 115, 105, 110)])
    stale_now = NOW_MS + 5 * DAY_MS
    import strategy
    called = []
    monkeypatch.setattr(strategy, "wilders_atr", lambda *a, **k: called.append(1))
    result = evaluate_donchian("TESTUSDT", rows, CFG, stale_now)
    assert result.signal is None and result.rejection_stage == "Data"
    assert "Stale daily candles" in result.rejection_reason
    assert called == []


def test_open_candle_is_ignored_by_the_evaluation():
    rows = daily(FLAT + [(108, 115, 105, 110)])
    rows.append(Candle(N_DAYS * DAY_MS, 100, 900, 1, 800, 9999.0, (N_DAYS + 1) * DAY_MS - 1, 9999.0))
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS + 20_000)
    assert result.signal is not None and result.signal.direction == "LONG"


def test_gap_between_daily_candles_is_a_data_rejection():
    rows = daily(FLAT + [(108, 115, 105, 110)])
    del rows[-2]
    rows.insert(0, daily([(100, 101, 99, 100)], start_day=-1)[0])
    result = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS)
    assert result.signal is None and result.rejection_stage == "Data"
    assert "consecutive" in result.rejection_reason


def test_filtered_rows_expose_channels_and_reason():
    rows = daily(FLAT + [(100, 101, 99, 100)])
    row = evaluate_donchian("TESTUSDT", rows, CFG, NOW_MS).filtered_dict()
    assert row["symbol"] == "TESTUSDT"
    assert row["stage"] == "Entry"
    assert row["entry_channel"] == 101.0
    assert row["exit_channel"] == 99.0
    assert row["atr"] > 0
    assert row["holding"] is False


def test_default_settings_schedule_and_universe(tmp_path):
    settings = SettingsStore(str(tmp_path / "missing.json")).get()
    assert settings.settings_revision == 7
    assert settings.starting_capital_usdt == 200.0
    assert settings.risk_per_trade_pct == 1.0
    assert (settings.entry_period, settings.exit_period, settings.atr_period, settings.atr_multiplier) == (55, 20, 20, 2.0)
    assert settings.scan_workers == 100
    assert settings.entry_grace_minutes == 15
    assert settings.active_universe == "Core 19"
    public = settings.public_dict()
    assert public["schedule"] == "Daily at 00:00 UTC"
    assert public["strategy_version"] == "donchian_55_20_adx25_atr_stop_v2"
    assert "frozen ATR stop distance" in public["sizing_rule"]
    assert public["active_universe_symbols"] == DEFAULT_UNIVERSE_SYMBOLS
    assert public["telegram_bot_token"] == "" and public["telegram_token_configured"] is False
    for legacy in ["top_n", "app_minimum_surge", "minimum_reward_risk", "supertrend_period", "hourly_history_bars"]:
        assert legacy not in public


def test_legacy_worker_settings_migrate_to_100_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"scan_workers": 8}', encoding="utf-8")
    migrated = SettingsStore(str(path)).get()
    assert migrated.scan_workers == 100 and migrated.settings_revision == 7

    store = SettingsStore(str(path))
    store.update({"scan_workers": 8})
    assert SettingsStore(str(path)).get().scan_workers == 8

    path.write_text('{"settings_revision": 2, "scan_workers": 50}', encoding="utf-8")
    migrated = SettingsStore(str(path)).get()
    assert migrated.scan_workers == 100 and migrated.settings_revision == 7

    store = SettingsStore(str(path))
    store.update({"scan_workers": 50})
    assert SettingsStore(str(path)).get().scan_workers == 50


def test_old_settings_file_clamps_capital_into_the_documented_range(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"settings_revision": 4, "starting_capital_usdt": 5, "top_n": 777}', encoding="utf-8")
    loaded = SettingsStore(str(path)).get()
    assert loaded.starting_capital_usdt == 10.0
    assert loaded.settings_revision == 7
    assert "top_n" not in loaded.public_dict()


INVALID_SETTINGS = [
    {"risk_per_trade_pct": 0},
    {"risk_per_trade_pct": 11},
    {"entry_period": 9},
    {"exit_period": 4},
    {"starting_capital_usdt": 9},
    {"starting_capital_usdt": 2_000_000},
    {"entry_grace_minutes": 0},
    {"atr_multiplier": 0},
    {"active_universe": "Missing"},
    {"universes": []},
    {"universes": [{"name": "A", "symbols": ["BTC"]}]},
    {"universes": [{"name": "A", "symbols": ["BTCUSDT"]}, {"name": "a", "symbols": ["ETHUSDT"]}]},
]


@pytest.mark.parametrize("bad", INVALID_SETTINGS)
def test_invalid_settings_are_rejected(tmp_path, bad):
    store = SettingsStore(str(tmp_path / "settings.json"))
    with pytest.raises((ValueError, TypeError)):
        store.update(bad)
    assert SettingsStore(str(tmp_path / "settings.json")).get().settings_revision == 7


def test_universe_edits_round_trip_and_rename_symbols(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(str(path))
    value = store.update({
        "universes": [{"name": "Majors", "symbols": [" btcusdt ", "ethusdt", "BTCUSDT"]}],
        "active_universe": "majors",
    })
    assert value.universes == [{"name": "Majors", "symbols": ["BTCUSDT", "ETHUSDT"]}]
    assert value.active_universe == "majors"
    reloaded = SettingsStore(str(path)).get()
    assert reloaded.universes == value.universes
    assert reloaded.public_dict()["active_universe_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_only_manual_kill_switch_blocks_new_entries(tmp_path):
    cfg = BotConfig(kill_switch_file=str(tmp_path / "STOP"))
    logger = EventLogger(str(tmp_path / "events.jsonl"))
    state = {"open_positions": {}, "realized": [], "executed_entries": []}
    assert allowed_to_trade(state, cfg, logger)

    state["realized"] = [{"day": datetime.now(timezone.utc).date().isoformat(), "r": -3.0}]
    assert allowed_to_trade(state, cfg, logger)
    state["realized"] = [{"day": "2020-01-01", "r": -3.0}]
    assert allowed_to_trade(state, cfg, logger)

    state["realized"] = []
    (tmp_path / "STOP").touch()
    assert not allowed_to_trade(state, cfg, logger)


def test_previous_trade_and_execution_ledger_do_not_block_entries(tmp_path):
    cfg = BotConfig(kill_switch_file=str(tmp_path / "STOP"))
    logger = EventLogger(str(tmp_path / "events.jsonl"))
    today = datetime.now(timezone.utc).isoformat()
    state = {"open_positions": {}, "executed_entries": [{"signal_key": "AAAUSDT:1", "opened_at": today}],
             "realized": [{"opened_at": today, "r": 2.0, "pnl_usdt": 4.0}]}
    assert allowed_to_trade(state, cfg, logger)


def test_testnet_client_never_exposes_protective_order_methods():
    # The spec forbids stop/OCO orders: exits are channel closes only.
    assert not hasattr(BinanceTestnetClient, "sell_oco_market_triggers")
    assert not hasattr(BinanceTestnetClient, "oco_order")


def test_dashboard_database_open_close_lifecycle(tmp_path):
    db = TradeDatabase(str(tmp_path / "trades.sqlite3"))
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_signal({
        "symbol": "TESTUSDT", "signal_close_ms": 1, "close_time_utc": now,
        "entry": 100.0, "entry_channel": 95.0, "exit_channel": 90.0,
        "atr": 2.0, "stop_distance": 4.0, "direction": "LONG",
        "universe": "Core 10", "strategy_version": "donchian_55_20_v1",
    })
    assert db.signals()[0]["status"] == "VALID"
    db.open_trade({
        "signal_key": "TESTUSDT:1", "symbol": "TESTUSDT", "entry_price": 100.0,
        "quantity": 0.2, "risk_usdt": 2.0, "opened_at": now,
        "atr": 2.0, "entry_channel": 95.0, "exit_channel": 90.0,
    })
    assert db.stats()["open_count"] == 1
    assert db.signals()[0]["status"] == "TRADED"
    db.close_trade({
        "signal_key": "TESTUSDT:1", "outcome": "CHANNEL_EXIT", "exit_price": 90.0,
        "quantity": 0.2, "pnl_usdt": -2.0, "pnl_pct": -10.0,
        "realized_r": -1.0, "closed_at": now,
    })
    stats = db.stats()
    assert stats["open_count"] == 0
    assert stats["losses"] == 1
    assert stats["realized_pnl_usdt"] == -2.0
    assert stats["realized_r"] == -1.0
    assert db.closed_trades()[0]["outcome"] == "CHANNEL_EXIT"
