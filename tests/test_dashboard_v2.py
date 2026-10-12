"""Local HTTP integration; disables engine startup and never uses exchange keys."""
import importlib
import json
import re
import threading
from datetime import datetime, timezone
from http.client import HTTPConnection


def test_dashboard_settings_api_universes_capital_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('START_ENGINE', 'false')
    monkeypatch.setenv('AUTO_START_NEW_TRADES', 'false')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('BINANCE_TESTNET_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_TESTNET_API_SECRET', raising=False)
    app = importlib.import_module('cloud_app')
    server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        content = response.read().decode()
        status = response.status
        conn.close()
        return status, content

    try:
        assert request('GET', '/healthz')[0] == 200
        assert request('GET', '/api/status')[0] == 401
        assert request('GET', '/api/research/decisions')[0] == 401
        assert request('GET', '/api/research/cycle?id=1')[0] == 404
        assert request('GET', '/api/patterns/forecasts')[0] == 404
        assert request('GET', '/api/explorer/trial?id=1')[0] == 404
        sid, rec = app.make_session()
        old_cookie = {'Cookie': 'nr7_session=' + sid}
        assert request('GET', '/api/status', headers=old_cookie)[0] == 401
        headers = {'Cookie': 'donchian_session=' + sid, 'Content-Type': 'application/json'}
        for url in ['/api/explorer/trial?id=1', '/api/patterns/forecasts', '/api/research/cycle?id=1']:
            assert request('GET', url, headers=headers)[0] == 404
        code, text = request('GET', '/api/status', headers=headers)
        status = json.loads(text)
        assert code == 200 and 'tracking' in status
        assert not {'research', 'patterns', 'explorer'}.intersection(status)
        assert not app.REMOVED_JOB_SETTINGS.intersection(status['settings'])
        assert request('GET', '/api/research/decisions?offset=bad', headers=headers)[0] == 400
        code, text = request('GET', '/api/research/decisions?symbol=BTCUSDT&skipped=1', headers=headers)
        assert code == 200 and json.loads(text) == {'total': 0, 'rows': []}
        # Strategy settings: Donchian parameters round trip through the API.
        body = json.dumps(dict(risk_per_trade_pct=0.5, entry_period=60, exit_period=25,
                               atr_period=14, atr_multiplier=1.5, daily_history_bars=200,
                               entry_grace_minutes=10, scan_workers=50))
        assert request('POST', '/api/config', body, headers)[0] == 403
        headers['X-CSRF-Token'] = rec['csrf']
        version = app.settings_store.get().strategy_version
        for key in app.REMOVED_JOB_SETTINGS:
            assert request('POST', '/api/config', json.dumps({key: True}), headers)[0] == 400
        assert app.settings_store.get().strategy_version == version
        code, text = request('POST', '/api/config', body, headers)
        assert code == 200
        value = json.loads(text)['settings']
        assert value['risk_per_trade_pct'] == 0.5
        assert (value['entry_period'], value['exit_period']) == (60, 25)
        assert (value['atr_period'], value['atr_multiplier']) == (14, 1.5)
        assert value['daily_history_bars'] == 200 and value['entry_grace_minutes'] == 10
        assert value['strategy_version'].startswith('manual_')
        assert value['schedule'] == 'Daily at 00:00 UTC'
        for legacy in ['top_n', 'app_minimum_surge', 'minimum_reward_risk', 'hourly_history_bars']:
            assert legacy not in value
        saved = app.SettingsStore(str(tmp_path / 'settings.json'))
        assert saved.get().risk_per_trade_pct == 0.5
        cfg = app.engine._apply_settings(saved.get())
        assert cfg.entry_period == 60 and cfg.atr_multiplier == 1.5 and cfg.risk_pct == 0.5

        code, html = request('GET', '/', headers=headers)
        assert code == 200
        assert 'Donchian 55/20 Demo Agent' in html
        assert 'NR7' not in html and 'Supertrend' not in html
        assert 'id="setRiskPct"' in html and 'id="setUniverseName"' in html
        assert 'id="setUniverseSelect"' not in html and 'id="setUniverseActivate"' not in html
        assert 'Daily at 00:00 UTC' in html
        ids = set(re.findall(r'id="([^"]+)"', html))
        refs = set(re.findall(r"\$\('([^']+)'\)", html))
        assert refs <= ids, sorted(refs - ids)
        assert request('POST', '/api/config', '{"risk_per_trade_pct": -1}', headers)[0] == 400
        assert request('POST', '/api/start', headers=headers)[0] == 409  # no credentials
        assert app.engine.exec_client is None

        # Universe editor: named universes round trip and the active one is applied.
        body = json.dumps(dict(universes=[{'name': 'Test', 'symbols': ['BTCUSDT', 'ETHUSDT']}],
                               active_universe='Test'))
        code, text = request('POST', '/api/config', body, headers)
        assert code == 200
        value = json.loads(text)['settings']
        assert value['active_universe'] == 'Test'
        assert value['active_universe_symbols'] == ['BTCUSDT', 'ETHUSDT']
        assert request('POST', '/api/config', '{"active_universe": "Missing"}', headers)[0] == 400
        assert request('POST', '/api/config', '{"universes": []}', headers)[0] == 400
        app.engine._apply_settings(app.settings_store.get())
        assert app.engine.cfg.universe_name == 'Test'
        assert app.engine.cfg.universe_symbols == ['BTCUSDT', 'ETHUSDT']
        status, text = request('GET', '/api/status', headers=headers)
        assert json.loads(text)['settings']['active_universe'] == 'Test'
        assert app.engine.cfg.universe_name == 'Test'

        # Capital changes preserve strategy settings, Closed P&L and positions.
        app.db.open_trade(dict(signal_key='CAPITAL:1', symbol='CAPITALUSDT',
                               entry_price=10, quantity=10, tp=12, sl=9, rr=2, risk_usdt=10))
        app.db.close_trade(dict(signal_key='CAPITAL:1', outcome='CHANNEL_EXIT', exit_price=11,
                                pnl_usdt=10, pnl_pct=10, realized_r=1,
                                closed_at='2026-10-07T00:00:00Z'))
        app.db.open_trade(dict(signal_key='OPEN:1', symbol='OPENUSDT',
                               entry_price=10, quantity=5, tp=12, sl=9, rr=2, risk_usdt=5))
        app.engine.live_prices['OPENUSDT'] = 12
        before_closed = app.db.closed_trades()
        before_open = app.db.open_trades()
        code, text = request('POST', '/api/config', '{"starting_capital_usdt": 500}', headers)
        assert code == 200
        status, text = request('GET', '/api/status', headers=headers)
        value = json.loads(text)
        assert status == 200 and value['stats']['demo_balance_usdt'] == 510
        assert value['stats']['estimated_demo_equity_usdt'] == 520
        assert value['stats']['available_demo_cash_usdt'] == 460
        assert value['settings']['risk_per_trade_pct'] == 0.5
        assert value['settings']['entry_period'] == 60
        assert value['risk']['risk_per_trade_pct'] == 0.5
        assert value['risk']['sizing_rule'] == 'risk % of equity / (2 x ATR)'
        assert value['risk']['max_position_notional_usdt'] is None
        assert value['risk']['cash_reserve_pct'] == 0.0
        assert 'open P&L' in value['risk']['equity_basis']
        assert value['risk']['daily_loss_limit_r'] is None
        assert app.db.closed_trades() == before_closed and app.db.open_trades() == before_open
        saved = app.SettingsStore(str(tmp_path / 'settings.json'))
        assert saved.get().starting_capital_usdt == 500
        restarted = app.CloudDemoEngine(app.cfg, app.db, saved)
        assert restarted.snapshot()['stats']['demo_balance_usdt'] == 510
        for invalid in [0, -1, True, 'NaN', 'Infinity']:
            body = json.dumps({'starting_capital_usdt': invalid})
            assert request('POST', '/api/config', body, headers)[0] == 400
        assert app.engine.cfg.demo_equity_cap_usdt == 500
        # A negative close reduces balance; replay/restart cannot double-count it.
        app.db.close_trade(dict(signal_key='OPEN:1', outcome='CHANNEL_EXIT', exit_price=9,
                                pnl_usdt=-5, pnl_pct=-10, realized_r=-1,
                                closed_at='2026-10-07T01:00:00Z'))
        assert restarted.snapshot()['stats']['demo_balance_usdt'] == 505
        assert app.db.stats()['closed_count'] == 2
        open_trade = restarted.snapshot()['open_trades']
        assert open_trade == []  # OPEN:1 was just closed

        # The daily scheduler targets exactly 00:00 UTC without a settle delay.
        day = int(datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())
        assert app.next_day_boundary(day + 30) == day + 86400
        assert app.next_day_boundary(day + 86_300) == day + 86400

        # Railway refuses new entries without durable storage; exit readiness
        # is independent and remains available for positions already tracked.
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_ID', 'test-production')
        unmounted = app.CloudDemoEngine(app.cfg, app.db, saved)
        assert unmounted.storage_error and not unmounted.new_entries_enabled()
        import pytest
        with pytest.raises(ValueError, match='Persistent trade storage'):
            unmounted.set_new_entries_enabled(True)
        monkeypatch.setattr(app.os.path, 'ismount', lambda path: True)
        mounted = app.CloudDemoEngine(app.cfg, app.db, saved)
        assert mounted.history_persistent and not mounted.storage_error
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
