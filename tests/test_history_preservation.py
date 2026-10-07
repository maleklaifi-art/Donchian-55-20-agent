from copy import deepcopy

import pytest

from database import TradeDatabase


def seed_trade(db):
    db.upsert_signal(dict(symbol='DJTBUSDT', signal_close_ms=1, entry=9.17,
                          tp=9.44, sl=9.08, rr=3.0))
    trade = dict(signal_key='DJTBUSDT:1', symbol='DJTBUSDT', entry_price=9.15,
                 quantity=2, tp=9.44, sl=9.08, rr=(9.44-9.15)/(9.15-9.08),
                 risk_usdt=0.14, opened_at='2026-10-01T00:00:00Z')
    db.open_trade(trade)
    return trade


def test_signal_rr_matches_passed_without_changing_fill_accounting(tmp_path):
    db = TradeDatabase(str(tmp_path/'trades.sqlite3'))
    seed_trade(db)
    row = db.open_trades()[0]
    assert row['signal_rr'] == 3.0
    assert row['signal_entry'] == 9.17
    assert row['rr'] == pytest.approx(4.1428571429)
    assert row['entry_price'] == 9.15 and row['risk_usdt'] == 0.14


def test_closed_history_survives_restart_and_stale_backfill_exactly(tmp_path):
    path = str(tmp_path/'trades.sqlite3')
    db = TradeDatabase(path)
    trade = seed_trade(db)
    closed = dict(trade, outcome='TP', exit_price=9.44, pnl_usdt=0.58,
                  pnl_pct=0.58/18.3*100, realized_r=0.58/0.14,
                  closed_at='2026-10-01T01:00:00Z')
    db.close_trade(closed)
    before = deepcopy(db.closed_trades())
    before_signals = deepcopy(db.signals())
    db = TradeDatabase(path)
    # Crash/restart can leave old open and partial realized records in state.
    db.backfill_state({'open_positions': {'DJTBUSDT': trade},
                       'realized': [{'signal_key': trade['signal_key'], 'pnl_usdt': -99}]})
    assert db.closed_trades() == before
    assert db.signals() == before_signals
    assert db.open_trades() == []


def test_legacy_open_trade_without_signal_is_still_readable(tmp_path):
    db = TradeDatabase(str(tmp_path/'trades.sqlite3'))
    db.open_trade(dict(signal_key='OLDUSDT:1', symbol='OLDUSDT', entry_price=10,
                       quantity=2, tp=13, sl=9, rr=3, risk_usdt=2))
    row = db.open_trades()[0]
    assert row['signal_rr'] is None and row['rr'] == 3
