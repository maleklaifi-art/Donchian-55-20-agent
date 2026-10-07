"""Local HTTP integration; disables engine startup and never uses exchange keys."""
import importlib
import json
import re
import threading
from http.client import HTTPConnection


def test_dashboard_settings_api_zero_auth_csrf_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('START_ENGINE','false')
    monkeypatch.setenv('AUTO_START_NEW_TRADES','false')
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.delenv('BINANCE_TESTNET_API_KEY',raising=False)
    monkeypatch.delenv('BINANCE_TESTNET_API_SECRET',raising=False)
    app = importlib.import_module('cloud_app')
    server = app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def request(method,path,body=None,headers=None):
        conn = HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        conn.request(method,path,body=body,headers=headers or {})
        response = conn.getresponse()
        content = response.read().decode()
        status = response.status
        conn.close()
        return status,content
    try:
        assert request('GET','/healthz')[0] == 200
        assert request('GET','/api/status')[0] == 401
        sid, rec = app.make_session()
        headers = {'Cookie':'nr7_session='+sid,'Content-Type':'application/json'}
        body = json.dumps(dict(app_minimum_surge=0,indicator_minimum_surge=0))
        assert request('POST','/api/config',body,headers)[0] == 403
        headers['X-CSRF-Token'] = rec['csrf']
        code, text = request('POST','/api/config',body,headers)
        assert code == 200
        value = json.loads(text)['settings']
        assert value['app_minimum_surge'] == value['indicator_minimum_surge'] == 0
        assert value['minimum_reward_risk'] == 1 and 'maximum_reward_risk' not in value
        assert value['supertrend_period'] == 10 and value['supertrend_multiplier'] == 3
        assert app.SettingsStore(str(tmp_path/'settings.json')).get().app_minimum_surge == 0
        cfg = app.engine._apply_settings(app.settings_store.get())
        assert cfg.app_minimum_surge == 0 and cfg.pine_minimum_reward_risk == 1
        code, html = request('GET','/',headers=headers)
        assert code == 200 and 'App + NR7/Supertrend Pine' in html
        assert 'id="setAppSurge" type="number" min="0"' in html
        ids = set(re.findall(r'id="([^"]+)"',html))
        refs = set(re.findall(r"\$\('([^']+)'\)",html))
        assert refs <= ids
        assert request('POST','/api/config','{"app_minimum_surge":-1}',headers)[0] == 400
        assert request('POST','/api/start',headers=headers)[0] == 409  # no credentials
        assert app.engine.exec_client is None

        # Capital changes preserve strategy settings, Closed P&L and positions.
        app.db.open_trade(dict(signal_key='CAPITAL:1',symbol='CAPITALUSDT',
            entry_price=10,quantity=10,tp=12,sl=9,rr=2,risk_usdt=10))
        app.db.close_trade(dict(signal_key='CAPITAL:1',outcome='TP',exit_price=11,
            pnl_usdt=10,pnl_pct=10,realized_r=1,closed_at='2026-10-07T00:00:00Z'))
        app.db.open_trade(dict(signal_key='OPEN:1',symbol='OPENUSDT',
            entry_price=10,quantity=5,tp=12,sl=9,rr=2,risk_usdt=5))
        app.engine.live_prices['OPENUSDT'] = 12
        before_closed = app.db.closed_trades()
        before_open = app.db.open_trades()
        code, text = request('POST','/api/config','{"starting_capital_usdt":500}',headers)
        assert code == 200
        status, text = request('GET','/api/status',headers=headers)
        value = json.loads(text)
        assert status == 200 and value['stats']['demo_balance_usdt'] == 510
        assert value['stats']['estimated_demo_equity_usdt'] == 520
        assert value['stats']['available_demo_cash_usdt'] == 460
        assert value['settings']['app_minimum_surge'] == 0
        assert value['risk']['risk_per_trade_usdt'] == 2
        assert value['risk']['max_position_notional_usdt'] == 100
        assert app.db.closed_trades() == before_closed and app.db.open_trades() == before_open
        saved = app.SettingsStore(str(tmp_path/'settings.json'))
        assert saved.get().starting_capital_usdt == 500
        restarted = app.CloudDemoEngine(app.cfg, app.db, saved)
        assert restarted.snapshot()['stats']['demo_balance_usdt'] == 510
        for invalid in [0, -1, True, 'NaN', 'Infinity']:
            body = json.dumps({'starting_capital_usdt': invalid})
            assert request('POST','/api/config',body,headers)[0] == 400
        assert app.engine.cfg.demo_equity_cap_usdt == 500
        # A negative close reduces balance; replay/restart cannot double-count it.
        app.db.close_trade(dict(signal_key='OPEN:1',outcome='SL',exit_price=9,
            pnl_usdt=-5,pnl_pct=-10,realized_r=-1,closed_at='2026-10-07T01:00:00Z'))
        assert restarted.snapshot()['stats']['demo_balance_usdt'] == 505
        assert app.db.stats()['closed_count'] == 2

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
