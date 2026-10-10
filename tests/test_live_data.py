from unittest.mock import Mock, patch

import pytest
import demo_trader
from demo_trader import (
    BotConfig,
    DonchianConfig,
    EventLogger,
    PublicMarketClient,
    place_signal,
    scan_market_detailed,
)
from strategy import DAY_MS, Candle, evaluate_donchian, is_current_signal_close

N_DAYS = 61
BOUNDARY = N_DAYS * DAY_MS
NOW = BOUNDARY + 30_000
FLAT = [(100, 101, 99, 100)] * 60


def daily(specs, start_day=0):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        open_time = (start_day + i) * DAY_MS
        rows.append(Candle(open_time, o, h, l, c, 10.0, open_time + DAY_MS - 1, 1000.0))
    return rows


def test_exact_previous_daily_close_only():
    for extra in [0, 60_000, DAY_MS - 1]:
        now = BOUNDARY + extra
        expected = now // DAY_MS * DAY_MS - 1
        assert is_current_signal_close(expected, now)
        for wrong in [expected - DAY_MS, expected + DAY_MS, expected - 1, expected + 1]:
            assert not is_current_signal_close(wrong, now)


def test_old_data_rejected_before_any_strategy_calculation(monkeypatch):
    import strategy
    called = []
    monkeypatch.setattr(strategy, "wilders_atr", lambda *a, **k: called.append(1))
    stale_now = NOW + 10 * DAY_MS
    result = evaluate_donchian("EOSUSDT", daily(FLAT), DonchianConfig(), stale_now)
    assert result.signal is None
    assert result.rejection_stage == "Data"
    assert "Stale daily candles" in result.rejection_reason
    assert called == []


def test_current_day_reaches_the_evaluator_and_ignores_the_open_candle(monkeypatch):
    import strategy
    rows = daily(FLAT + [(108, 115, 105, 110)])
    rows.append(Candle(N_DAYS * DAY_MS, 100, 900, 1, 800, 9999.0, (N_DAYS + 1) * DAY_MS - 1, 9999.0))
    seen = []
    original = strategy.wilders_atr
    monkeypatch.setattr(strategy, "wilders_atr", lambda bars, period: (seen.append(len(bars)), original(bars, period))[1])
    result = evaluate_donchian("TESTUSDT", rows, DonchianConfig(), NOW + 20_000)
    assert result.signal is not None and result.signal.direction == "LONG"
    assert seen == [61]


def test_gap_between_daily_candles_cannot_stand_in_for_a_missing_day():
    rows = daily(FLAT + [(108, 115, 105, 110)])
    del rows[-2]
    rows.insert(0, daily([(100, 101, 99, 100)], start_day=-1)[0])
    result = evaluate_donchian("TESTUSDT", rows, DonchianConfig(), NOW)
    assert result.signal is None and result.rejection_stage == "Data"
    assert "consecutive" in result.rejection_reason


def active(symbol="ACTIVEUSDT", **over):
    row = dict(symbol=symbol, status="TRADING", isSpotTradingAllowed=True, quoteAsset="USDT")
    row.update(over)
    return row


def test_universe_resolution_filters_exchange_state_and_keeps_order():
    missing_flag = dict(symbol="UNKNOWNUSDT", status="TRADING", quoteAsset="USDT")
    items = [
        active(),
        active("SMALLUSDT"),
        active("EOSUSDT", status="BREAK"),
        active("HALTUSDT", status="HALT"),
        active("FUTUREUSDT", isSpotTradingAllowed=False),
        active("WRONGUSDT", quoteAsset="BTC"),
        missing_flag,
        active("FDUSDUSDT"),  # stablecoin, excluded by name
    ]
    client = PublicMarketClient(1)
    requested = ["SMALLUSDT", "ACTIVEUSDT", "HALTUSDT", "EOSUSDT", "FUTUREUSDT",
                 "WRONGUSDT", "UNKNOWNUSDT", "FDUSDUSDT", "REMOVEDUSDT"]
    with patch.object(client, "get", return_value={"symbols": items}) as get:
        assert client.resolve_universe(requested, "USDT") == ["SMALLUSDT", "ACTIVEUSDT"]
    assert get.call_count == 1
    assert get.call_args.args[0] == "/api/v3/exchangeInfo"


def test_no_ticker_fallback_when_exchange_info_fails():
    client = PublicMarketClient(1)
    with patch.object(client, "get", side_effect=RuntimeError("HTTP 500")) as get:
        with pytest.raises(RuntimeError):
            client.resolve_universe(["BTCUSDT"], "USDT")
    assert get.call_count == 1


def test_stablecoin_exclusions_apply_to_the_requested_universe():
    symbols = sorted(demo_trader.EXCLUDED_SYMBOLS) + ["BTCUSDT", "ETHUSDT"]
    info = {"symbols": [active(s) for s in symbols]}
    client = PublicMarketClient(1)
    with patch.object(client, "get", return_value=info):
        assert client.resolve_universe(symbols, "USDT") == ["BTCUSDT", "ETHUSDT"]


class FakeMarket:
    def __init__(self, data):
        self.data = data
        self.kline_calls = []

    def resolve_universe(self, requested, quote_asset):
        return [s for s in requested if s in self.data]

    def klines(self, symbol, interval, limit):
        self.kline_calls.append((symbol, interval, limit))
        return self.data[symbol]


def universe_data():
    return {
        "BREAKOUTUSDT": daily(FLAT + [(108, 115, 105, 110)]),
        "EXITMEUSDT": daily(FLAT + [(97, 98, 94, 95)]),
        "HELDUSDT": daily(FLAT + [(100, 101, 99, 100)]),
        "FLATUSDT": daily(FLAT + [(100, 101, 99, 100)]),
    }


def scan(market, tmp_path, holding=("EXITMEUSDT", "HELDUSDT")):
    cfg = BotConfig(scan_workers=4, universe_symbols=list(market.data),
                    signal_log=str(tmp_path / "signals.json"),
                    event_log=str(tmp_path / "events.jsonl"))
    logger = EventLogger(cfg.event_log)
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        return scan_market_detailed(market, cfg, DonchianConfig(), logger, open_symbols=set(holding))


def test_scan_splits_entries_exits_and_no_action(tmp_path):
    market = FakeMarket(universe_data())
    snapshot = scan(market, tmp_path)
    assert snapshot["coins_scanned"] == 4
    assert snapshot["universe"] == "Core 10"
    assert snapshot["entry_count"] == snapshot["valid_count"] == 1
    assert snapshot["exit_count"] == 1
    assert snapshot["error_count"] == 0
    entry = snapshot["signals"][0]
    assert entry["symbol"] == "BREAKOUTUSDT" and entry["direction"] == "LONG"
    assert entry["entry_channel"] == 101.0 and entry["stop_distance"] > 0
    exit_row = snapshot["exit_signals"][0]
    assert exit_row["symbol"] == "EXITMEUSDT" and exit_row["direction"] == "EXIT"
    assert exit_row["exit_channel"] == 99.0
    by_symbol = {row["symbol"]: row for row in snapshot["filtered"]}
    assert set(by_symbol) == {"HELDUSDT", "FLATUSDT"}
    assert by_symbol["HELDUSDT"]["stage"] == "Held"
    assert by_symbol["FLATUSDT"]["stage"] == "Entry"
    assert market.kline_calls and all(call[1] == "1d" for call in market.kline_calls)
    assert snapshot["duration_seconds"] > 0


def test_stale_scan_is_retained_as_a_data_failure_without_signals(tmp_path):
    market = FakeMarket({"EOSUSDT": daily(FLAT)})
    snapshot = scan(market, tmp_path, holding=())
    assert snapshot["coins_scanned"] == 1
    assert snapshot["valid_count"] == 0 and snapshot["entry_count"] == 0
    assert snapshot["filtered"][0]["stage"] == "Data"
    assert "Stale daily candles" in snapshot["filtered"][0]["reason"]


def test_scan_without_holding_never_emits_exit_signals(tmp_path):
    market = FakeMarket({"EXITMEUSDT": daily(FLAT + [(97, 98, 94, 95)])})
    snapshot = scan(market, tmp_path, holding=())
    assert snapshot["exit_count"] == 0
    assert snapshot["entry_count"] == 0
    assert snapshot["filtered"][0]["stage"] == "Entry"


def test_stale_entry_blocked_before_state_write_or_exchange_call(tmp_path):
    cfg = BotConfig(state_file=str(tmp_path / "state.json"), kill_switch_file=str(tmp_path / "STOP"))
    market, client, logger = Mock(), Mock(), Mock()
    state = {"seen_signals": []}
    from strategy import Signal
    sig = Signal(symbol="EOSUSDT", signal_close_ms=BOUNDARY - 1 - DAY_MS,
                 close_time_utc="2026-01-01T00:00:00Z", direction="LONG", entry=90.0,
                 entry_channel=85.0, exit_channel=80.0, atr=1.0, stop_distance=2.0)
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        place_signal(sig, market, client, {}, state, cfg, logger)
    assert state == {"seen_signals": []}
    assert not (tmp_path / "state.json").exists()
    assert not client.mock_calls and not market.mock_calls
    assert logger.emit.call_args.kwargs["reason"] == "stale signal candle"


def test_removed_held_coins_still_receive_exit_checks(tmp_path):
    market = FakeMarket(universe_data())
    cfg = BotConfig(scan_workers=1, universe_symbols=["FLATUSDT"],
                    signal_log=str(tmp_path / "signals.json"))
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        snapshot = scan_market_detailed(market, cfg, DonchianConfig(), Mock(),
                                        open_symbols={"EXITMEUSDT", "BREAKOUTUSDT"})
    assert {s for s, _, _ in market.kline_calls} == {"FLATUSDT", "EXITMEUSDT", "BREAKOUTUSDT"}
    assert [s["symbol"] for s in snapshot["exit_signals"]] == ["EXITMEUSDT"]
    assert snapshot["signals"] == []  # held breakout cannot become a fresh entry
    market.kline_calls.clear()
    with patch.object(demo_trader.time, "time", return_value=NOW / 1000):
        scan_market_detailed(market, cfg, DonchianConfig(), Mock(), open_symbols=set())
    assert [s for s, _, _ in market.kline_calls] == ["FLATUSDT"]
