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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
