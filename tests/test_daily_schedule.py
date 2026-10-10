"""Offline boundary and delayed-candle regression checks; no exchange orders."""
import importlib
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import demo_trader
from strategy import Candle, DAY_MS, DonchianConfig


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('START_ENGINE', 'false')
    monkeypatch.setenv('AUTO_START_NEW_TRADES', 'false')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('BINANCE_TESTNET_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_TESTNET_API_SECRET', raising=False)
    was_loaded = 'cloud_app' in sys.modules
    try:
        yield importlib.import_module('cloud_app')
    finally:
        # Do not leave this fixture's temporary DATA_DIR in the HTTP tests.
        if not was_loaded:
            sys.modules.pop('cloud_app', None)


@pytest.mark.parametrize('offset', [0, 30, 86390, 86399.75])
def test_cloud_and_cli_target_exact_boundary(app, monkeypatch, offset):
    day = 61 * 86400
    now = day + offset
    expected = day + 86400
    assert app.next_day_boundary(now) == expected
    monkeypatch.setattr(demo_trader.time, 'time', lambda: now)
    sleep = Mock()
    monkeypatch.setattr(demo_trader.time, 'sleep', sleep)
    demo_trader.sleep_to_next_day()
    sleep.assert_called_once_with(expected - now)

    stop = threading.Event()
    wake = Mock()
    wake.wait.side_effect = lambda **kw: stop.set()
    engine = SimpleNamespace(stop_event=stop, scan_wakeup=wake,
                             meta_lock=threading.RLock(), _run_scan=Mock())
    app.CloudDemoEngine._scanner_loop(engine)
    engine._run_scan.assert_called_once_with()
    wake.wait.assert_called_once_with(timeout=expected - now)
    assert engine.next_scan_at_ms == expected * 1000


def candles():
    values = [(100, 101, 99, 100)] * 60 + [(108, 115, 105, 110)]
    return [Candle(i * DAY_MS, o, h, l, c, 10, (i + 1) * DAY_MS - 1, 1000)
            for i, (o, h, l, c) in enumerate(values)]


@pytest.mark.parametrize('mode,calls,signals', [('fresh', 1, 1), ('delayed', 2, 1), ('stale', 3, 0), ('outside_window', 1, 0)])
def test_retry_only_when_latest_closed_candle_is_missing(tmp_path, monkeypatch, mode, calls, signals):
    rows = candles()
    now = 61 * DAY_MS + (16 * 60000 if mode == 'outside_window' else 0)
    market = Mock()
    market.resolve_universe.return_value = ['TESTUSDT']
    # Append the open day's huge breakout: it must never drive the signal.
    open_bar = Candle(61 * DAY_MS, 110, 1000, 1, 900, 10, 62 * DAY_MS - 1, 1000)
    fresh = rows + [open_bar]
    market.klines.side_effect = ([fresh] if mode == 'fresh' else
                                [rows[:-1], fresh] if mode == 'delayed' else [rows[:-1]] * 3)
    monkeypatch.setattr(demo_trader.time, 'time', lambda: now / 1000)
    sleep = Mock()
    monkeypatch.setattr(demo_trader.time, 'sleep', sleep)
    cfg = demo_trader.BotConfig(scan_workers=1, signal_log=str(tmp_path / 'signals.json'))
    result = demo_trader.scan_market_detailed(market, cfg, DonchianConfig(), Mock())
    assert market.klines.call_count == calls
    assert sleep.call_count == calls - 1
    assert len(result['signals']) == signals
    if signals:
        assert result['signals'][0]['entry'] == 110
        assert result['signals'][0]['signal_close_ms'] == 61 * DAY_MS - 1


def test_same_scan_exits_update_balance_before_entry_sizing(app, monkeypatch, tmp_path):
    db = Mock()
    db.stats.return_value = {"realized_pnl_usdt": -5}
    state = {"open_positions": {}, "realized": []}
    monkeypatch.setattr(app, "load_state", lambda path: state)
    monkeypatch.setattr(app, "Signal", lambda **row: SimpleNamespace(**row))
    def close(*args):
        db.stats.return_value = {"realized_pnl_usdt": 10}
    monkeypatch.setattr(app, "close_position", close)
    submit = Mock()
    monkeypatch.setattr(app, "place_signal", submit)
    engine = SimpleNamespace(state_lock=threading.RLock(), db=db,
                             cfg=SimpleNamespace(state_file=str(tmp_path / "state.json")),
                             market=Mock(), exec_client=Mock(), rules={}, logger=Mock(),
                             storage_error=None)
    app.CloudDemoEngine._execute_snapshot(engine, {
        "exit_signals": [{"symbol": "OLDUSDT", "signal_close_ms": 1}],
        "signals": [{"symbol": "NEWUSDT", "signal_close_ms": 1}],
    })
    submit.assert_called_once()
    assert submit.call_args.kwargs["realized_pnl_usdt"] == 10
