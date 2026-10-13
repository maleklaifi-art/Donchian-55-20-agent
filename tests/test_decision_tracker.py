"""Trading-only runtime: preserve data, stop research and retain outcome tracking."""
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from database import TradeDatabase
from decision_tracker import DecisionTracker, config_for, fingerprint
from research_store import ResearchStore
from runtime_settings import RuntimeSettings
from strategy import Candle, DAY_MS


def cache(directory, symbol, count):
    bars = [Candle(i*DAY_MS, 100, 112, 99, 110, 100,
                   (i+1)*DAY_MS-1, 10000) for i in range(count)]
    (directory / (symbol+'.json')).write_text(json.dumps({
        'version': 1, 'symbol': symbol, 'candles': [asdict(b) for b in bars]}))


def test_outcomes_continue_without_research_and_retry_missing_coins(tmp_path):
    settings = RuntimeSettings(research_enabled=False, research_auto_adopt=False,
        pattern_enabled=False, explorer_enabled=False,
        universes=[{'name':'test','symbols':['AAAUSDT','BBBUSDT']}], active_universe='test')
    store = ResearchStore(TradeDatabase(str(tmp_path/'trades.sqlite3')))
    rows = [{'symbol':symbol, 'signal_close_ms':40*DAY_MS-1, 'strategy_version':'v1',
             'action':'NO_ACTION', 'stage':'ADX', 'adx':24, 'adx_threshold':25,
             'reason':'ADX below threshold'} for symbol in ('AAAUSDT','BBBUSDT')]
    store.record_decisions({'decisions':rows}, config_for(settings), True, True)
    history = tmp_path/'daily_klines';history.mkdir()
    cache(history, 'AAAUSDT', 47)
    tracker = DecisionTracker(store, history, lambda:settings)
    with patch.object(store, 'update_outcomes', wraps=store.update_outcomes) as update:
        tracker.run_once(47*DAY_MS)
        tracker.run_once(47*DAY_MS)
        assert update.call_count==1
        tracker.run_once(47*DAY_MS, force=True)  # A retry can repair an existing decision.
        assert update.call_count==2
    a = store.decisions(symbol='AAAUSDT')['rows'][0]
    b = store.decisions(symbol='BBBUSDT')['rows'][0]
    assert a['outcomes']['7']['return_pct'] > 9.99
    assert '30' not in a['outcomes'] and not b['outcomes']
    assert a['action']=='NO_ACTION' and a['execution']=='SKIPPED'
    assert 'BBBUSDT' in tracker.snapshot()['data']['errors']
    cache(history, 'BBBUSDT', 47)
    tracker.run_once(47*DAY_MS)
    assert '7' in store.decisions(symbol='BBBUSDT')['rows'][0]['outcomes']
    assert tracker.snapshot()['data']['available']==2
    tracker.scan_busy=lambda:True
    with patch.object(store, 'update_outcomes') as update:
        tracker.run_once(48*DAY_MS)
        update.assert_not_called()


def test_recorded_configuration_identity_matches_existing_decisions():
    from research_engine import config_for as original_config, fingerprint as original_fingerprint
    settings = RuntimeSettings()
    assert {k:v for k,v in config_for(settings).items() if k!='execution'}==original_config(settings)
    assert fingerprint(config_for(settings))!=original_fingerprint(original_config(settings))


def test_startup_preserves_saved_state_and_starts_only_trading_workers(tmp_path):
    settings = asdict(RuntimeSettings(entry_period=60, exit_period=23, adx_threshold=27,
                                     strategy_version='saved_version'))
    held = {'symbol':'AAAUSDT','signal_key':'AAAUSDT:1','entry_price':100,'quantity':.5,
            'risk_usdt':2,'entry_cost_usdt':50.1,'exit_period':27,'strategy_version':'held_v1',
            'opened_at':'2026-10-01T00:00:00Z','pending_exit_signal':{'keep':'existing'}}
    state = {'open_positions':{'AAAUSDT':held}, 'seen_signals':['keep'],
             'realized':[{'symbol':'OLDUSDT','signal_key':'old','pnl_usdt':4}],
             'pending_orders':{'keep':'unchanged'}}
    originals = {'settings.json':json.dumps(settings).encode(), 'state.json':json.dumps(state).encode(),
                 'patterns.sqlite3':b'archived-pattern-data', 'explorer.sqlite3':b'archived-explorer-data'}
    for name, contents in originals.items():
        (tmp_path/name).write_bytes(contents)
    env = {k:v for k,v in os.environ.items() if not k.startswith('BINANCE_')}
    env.update(START_ENGINE='false', AUTO_START_NEW_TRADES='false', DATA_DIR=str(tmp_path))
    script = '''
import sys
from unittest.mock import patch
import cloud_app as app
assert not {'research_engine','pattern_engine','explorer_engine'}.intersection(sys.modules)
assert app.engine.strategy_config.entry_period == 60
assert app.engine.strategy_config.exit_period == 23
assert app.engine.strategy_config.adx_threshold == 27
assert not hasattr(app.engine, '_promote_research')
with patch('threading.Thread') as thread:
    app.engine.start()
    assert [call.kwargs['name'] for call in thread.call_args_list] == [
        'decision-outcomes', 'donchian-monitor', 'donchian-scanner']
app.engine.tracking.run_once()
'''
    result = subprocess.run([sys.executable, '-c', script], env=env,
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=20)
    assert result.returncode==0, result.stderr
    for name, contents in originals.items():
        assert (tmp_path/name).read_bytes()==contents
