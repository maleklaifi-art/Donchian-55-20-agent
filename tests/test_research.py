"""Research regressions: strict gates, timing, immutable records and trade safety."""
import copy
import importlib
import json
import sys
from dataclasses import asdict
from unittest.mock import Mock, patch

import pytest
from database import TradeDatabase
from runtime_settings import RuntimeSettings, SettingsStore
from research_store import ResearchStore
from research_backtest import features, simulate, paired_lower_bound
from research_engine import ResearchEngine, config_for, validation_policy, load_history, choices, select_proposal
from strategy import Candle, DonchianConfig, DAY_MS, evaluate_donchian, wilders_adx_series


def bars(n=180, phase=0):
    import math
    out=[]
    for i in range(n):
        # Repeated rising and falling trends, with a known next-open gap.
        price=100+20*math.sin((i+phase)/16)+i*.04
        out.append(Candle(i*DAY_MS,price,price+.8,price-.8,price+.2,100,(i+1)*DAY_MS-1,10000))
    return out


def small_config():
    s=RuntimeSettings(universes=[{'name':'test','symbols':['AAAUSDT','BBBUSDT']}],active_universe='test')
    return config_for(s)


def test_adx_wilder_seed_zero_and_trend():
    flat=[Candle(i*DAY_MS,100,101,99,100,1,(i+1)*DAY_MS-1) for i in range(60)]
    x=wilders_adx_series(flat)
    assert all(v is None for v in x[:27])
    assert x[27:]==[0.0]*33
    trend=[Candle(i*DAY_MS,100+i,102+i,99+i,101+i,1,(i+1)*DAY_MS-1) for i in range(60)]
    assert wilders_adx_series(trend)[27:]==[100.0]*33


@pytest.mark.parametrize('adx,expected',[(24.9,False),(25,True),(25.0001,True)])
def test_adx25_is_inclusive_and_never_blocks_exits(adx,expected):
    x=[Candle(i*DAY_MS,100,101,99,100,1,(i+1)*DAY_MS-1) for i in range(60)]
    x.append(Candle(60*DAY_MS,108,115,105,110,1,61*DAY_MS-1))
    with patch('strategy.wilders_adx',return_value=adx):
        result=evaluate_donchian('AAAUSDT',x,DonchianConfig(),61*DAY_MS)
        assert bool(result.signal)==expected
        assert result.adx==adx
        if not expected: assert result.rejection_stage=='ADX'
        x[-1]=Candle(60*DAY_MS,97,98,94,95,1,61*DAY_MS-1)
        assert evaluate_donchian('AAAUSDT',x,DonchianConfig(),61*DAY_MS,holding=True).signal.direction=='EXIT'


def test_simulator_and_scanner_use_identical_signals_without_future_leakage():
    data=bars();rules=small_config()['rules'];cfg=DonchianConfig(**rules)
    f=features(data,rules)
    for i in range(56,len(data)):
        result=evaluate_donchian('AAAUSDT',data[:i+1],cfg,(i+1)*DAY_MS)
        assert bool(result.signal)==f[i]['buy']
        assert result.adx==pytest.approx(f[i]['adx'])
        assert result.atr==pytest.approx(f[i]['atr'])
        held=evaluate_donchian('AAAUSDT',data[:i+1],cfg,(i+1)*DAY_MS,holding=True)
        assert bool(held.signal)==f[i]['sell']
    mutated=copy.deepcopy(data)
    mutated[-1]=Candle(179*DAY_MS,1,99999,1,99999,1,180*DAY_MS-1)
    assert features(mutated,rules)[:-1]==f[:-1]


def test_simulator_costs_cash_and_next_open_execution():
    cfg=small_config();data={'AAAUSDT':bars(450),'BBBUSDT':bars(450,7)}
    out=simulate(data,cfg,70*DAY_MS,450*DAY_MS)
    assert out['metrics']['closed_trades']>0
    assert all(v>=0 for _,v in out['equity'])
    for trade in out['trades']:
        entry_idx=trade['entry_time_ms']//DAY_MS
        assert entry_idx>70
        assert features(data[trade['symbol']],cfg['rules'])[entry_idx-1]['buy']
    no_cost=copy.deepcopy(cfg);no_cost['cost'].update(fee=0,slip=0)
    free=simulate(data,no_cost,70*DAY_MS,450*DAY_MS)
    assert free['metrics']['net_profit']>out['metrics']['net_profit']
    assert out['metrics']['fee_bps']==25 and out['metrics']['slippage_bps']==5
    with pytest.raises(ValueError,match='missing days'):
        simulate({'AAAUSDT':data['AAAUSDT'][:-1],'BBBUSDT':data['BBBUSDT']},cfg,70*DAY_MS,450*DAY_MS)


def test_decisions_immutable_retries_execution_and_matured_outcomes(tmp_path):
    db=TradeDatabase(str(tmp_path/'trades.sqlite3'));store=ResearchStore(db);cfg=small_config()
    row={'symbol':'AAAUSDT','signal_close_ms':70*DAY_MS-1,'strategy_version':'v1',
         'action':'LONG','stage':'No signal','price':100,'adx':30,'reason':'','adx_threshold':25}
    snap={'decisions':[row]}
    store.record_decisions(snap,cfg,True,True)
    store.execution_event('POSITION_OPENED',{'signal_key':f'AAAUSDT:{70*DAY_MS-1}'})
    store.record_decisions({'decisions':[{**row,'action':'NO_ACTION','stage':'Held'}]},cfg,True,True)
    rec=store.decisions()['rows'][0]
    assert rec['action']=='LONG' and rec['execution']=='FILLED'
    store.execution_event('TRADE_REJECTED',{'signal_key':f'AAAUSDT:{70*DAY_MS-1}','reason':'seen'})
    assert store.decisions()['rows'][0]['execution']=='FILLED'
    # A missing-data row is repairable, unlike a genuine observed decision.
    bad={**row,'symbol':'BBBUSDT','action':'NO_ACTION','stage':'Data'}
    store.record_decisions({'decisions':[bad]},cfg,True,True)
    store.record_decisions({'decisions':[{**row,'symbol':'BBBUSDT'}]},cfg,True,True)
    assert store.decisions()['rows'][0]['action']=='LONG'
    store.update_outcomes({'AAAUSDT':bars(100),'BBBUSDT':bars(100)},77*DAY_MS)
    rec=store.decisions(symbol='AAAUSDT')['rows'][0]
    assert '7' in rec['outcomes'] and '30' not in rec['outcomes']
    assert rec['outcomes']['7']['return_pct']==pytest.approx((bars(100)[76].close/bars(100)[70].open-1)*100)
    assert store.analysis_summary()['groups'][0]['matured_7d']>=1
    assert ResearchStore(TradeDatabase(db.path)).decisions()['total']==2


def test_cache_refuses_wrong_ticker_gaps_and_stale_days(tmp_path):
    path=tmp_path/'AAAUSDT.json';x=bars(90)
    def save(values,symbol='AAAUSDT'):
        path.write_text(json.dumps({'version':1,'symbol':symbol,'candles':[asdict(b) for b in values]}))
    save(x);assert not load_history(tmp_path,['AAAUSDT'],90*DAY_MS)[1]
    save(x,'SPY');assert 'AAAUSDT' in load_history(tmp_path,['AAAUSDT'],90*DAY_MS)[1]
    save(x[:50]+x[51:]);assert 'AAAUSDT' in load_history(tmp_path,['AAAUSDT'],90*DAY_MS)[1]
    save(x[:-1]);assert 'AAAUSDT' in load_history(tmp_path,['AAAUSDT'],90*DAY_MS)[1]


def forward_cycle(store,settings):
    base=config_for(settings);candidate=copy.deepcopy(base);candidate['rules']['adx_threshold']=20
    c=store.new_cycle({'base':base,'base_version':settings.strategy_version,'candidate':candidate,
        'candidate_version':'research_1','trial':1,'change':{'key':'adx_threshold','before':25,'after':20},
        'forward_start_ms':600*DAY_MS,'look_index':0,'policy':validation_policy(settings)})
    store.stage(c,'decide','Candidate observing','OBSERVING')
    return c


def test_no_early_adoption_and_reject_changed_base(tmp_path):
    settings=SettingsStore(str(tmp_path/'settings.json'));store=ResearchStore(TradeDatabase(str(tmp_path/'db')))
    promote=Mock(return_value=True);engine=ResearchEngine(store,tmp_path,settings,promote)
    c=forward_cycle(store,settings.get())
    with patch('research_engine.simulate') as sim:
        engine._forward(c,{},settings.get(),650*DAY_MS)
        sim.assert_not_called();promote.assert_not_called()
    changed=settings.get();changed.starting_capital_usdt=999
    engine._forward(c,{},changed,690*DAY_MS)
    assert store.cycle(c['id'])['status']=='REJECTED';promote.assert_not_called()


def test_gates_reject_or_observe_and_restart_preserves_adoption(tmp_path):
    settings=SettingsStore(str(tmp_path/'settings.json'));store=ResearchStore(TradeDatabase(str(tmp_path/'db')))
    promote=Mock(return_value=True);engine=ResearchEngine(store,tmp_path,settings,promote)
    c=forward_cycle(store,settings.get())
    a={'metrics':{'closed_trades':22,'symbols_traded':6,'return_pct':5,'max_drawdown_pct':10},'returns':[0]*90}
    b={'metrics':{'closed_trades':25,'symbols_traded':7,'return_pct':7,'max_drawdown_pct':9},'returns':[.001]*90}
    with patch('research_engine.simulate',side_effect=[a,b]):
        engine._forward(c,{},settings.get(),690*DAY_MS)
    assert store.cycle(c['id'])['status']=='ADOPTED';promote.assert_called_once()
    assert all(store.cycle(c['id'])['payload']['forward']['gates'].values())
    c=forward_cycle(store,settings.get());b['metrics']['closed_trades']=2
    with patch('research_engine.simulate',side_effect=[a,b]):
        engine._forward(c,{},settings.get(),690*DAY_MS)
    saved=store.cycle(c['id']);assert saved['status']=='OBSERVING' and saved['payload']['look_index']==1
    # A restart sees the same predeclared second checkpoint; it does not retest daily.
    with patch('research_engine.simulate') as sim:
        engine._forward(saved,{},settings.get(),700*DAY_MS)
        sim.assert_not_called()
    assert promote.call_count==1


def test_candidate_scope_and_paired_uncertainty():
    base=small_config()
    for option in choices(base):
        assert option['key'] in ('entry_period','adx_threshold')
        changed=[k for k in base['rules'] if base['rules'][k]!=option['config']['rules'][k]]
        assert changed==[option['key']]
        assert option['config']['risk_pct']==base['risk_pct']
        assert option['config']['symbols']==base['symbols']
    assert paired_lower_bound([0]*90,[0]*90,.01,1)==0
    assert paired_lower_bound([0]*90,[-.01]*90,.01,1)<0


def test_invalid_ai_proposal_cannot_escape_allowlist(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-only');monkeypatch.setenv('RESEARCH_ANTHROPIC_MODEL','test-model')
    response=Mock(ok=True);response.json.return_value={'content':[{'text':'{"option_index":99,"hypothesis":"increase risk"}'}]}
    with patch('requests.post',return_value=response),pytest.raises(ValueError,match='allowed'):
        select_proposal(choices(small_config()),{})


def test_upgrade_and_tracking_preserve_position_without_adoption(tmp_path,monkeypatch):
    monkeypatch.setenv('START_ENGINE','false');monkeypatch.setenv('AUTO_START_NEW_TRADES','false')
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.delenv('BINANCE_TESTNET_API_KEY',raising=False)
    monkeypatch.delenv('BINANCE_TESTNET_API_SECRET',raising=False)
    from demo_trader import BotConfig, save_state, load_state
    app=importlib.import_module('cloud_app')
    old={'symbol':'AAAUSDT','signal_key':'AAAUSDT:1','entry_order_id':42,'entry_client_order_id':'keep',
         'entry_price':100,'quantity':.5,'risk_usdt':2,'opened_at':'2026-10-01T00:00:00Z',
         'entry_cost_usdt':50.1,'exit_channel':85,'pending_exit_signal':None}
    state={'open_positions':{'AAAUSDT':old},'realized':[{'symbol':'OLDUSDT','signal_key':'old','pnl_usdt':4}], 'seen_signals':['keep']}
    path=tmp_path/'state2.json';save_state(str(path),state)
    settings_path=tmp_path/'settings2.json'
    settings_path.write_text(json.dumps({'settings_revision':5,'exit_period':27,'starting_capital_usdt':250}))
    settings=SettingsStore(str(settings_path))
    # A crash between settings migration and position migration must not lose 27.
    settings=SettingsStore(str(settings_path));db=TradeDatabase(str(tmp_path/'db2'))
    cfg=BotConfig(state_file=str(path),event_log=str(tmp_path/'events'),signal_log=str(tmp_path/'signals'),kill_switch_file=str(tmp_path/'STOP2'))
    engine=app.CloudDemoEngine(cfg,db,settings)
    held=load_state(str(path))['open_positions']['AAAUSDT']
    assert all(held[k]==v for k,v in old.items()) and held['exit_period']==27
    c=forward_cycle(engine.research_store,settings.get());c['payload']['forward']={'gates':{'all':True}}
    before=path.read_bytes()
    saved_settings=settings_path.read_bytes()
    pending=engine.research_store.cycle(c['id'])
    assert not hasattr(engine, '_promote_research')
    assert not any(hasattr(engine, name) for name in ('research', 'patterns', 'explorer'))
    engine.tracking.run_once(now_ms=80*DAY_MS)
    assert path.read_bytes()==before  # No order, quantity, price, or exit mutation.
    assert settings_path.read_bytes()==saved_settings
    assert engine.research_store.cycle(c['id'])==pending
    assert settings.get().adx_threshold==25
    # Held coin still uses 27-day exit, despite a new active 20-day exit.
    rows=bars(80);market=Mock();market.resolve_universe.return_value=['AAAUSDT'];market.klines.return_value=rows
    with patch('demo_trader.time.time',return_value=80*86400), patch('demo_trader.evaluate_donchian', wraps=evaluate_donchian) as evaluate:
        from demo_trader import scan_market_detailed
        scan_market_detailed(market,cfg,engine.strategy_config,Mock(),{'AAAUSDT'},{'AAAUSDT':held})
        assert evaluate.call_args.args[2].exit_period==27
    # Research failure cannot block the existing execution/exit pathway.
    engine.exec_client=Mock();engine.execution_ready=True
    with patch('cloud_app.scan_market_detailed',return_value={'decisions':[]}), \
         patch.object(engine.research_store,'record_decisions',side_effect=RuntimeError('audit unavailable')), \
         patch.object(engine,'_execute_snapshot') as execute:
        engine._run_scan()
        execute.assert_called_once()


def test_research_retests_only_after_new_data_and_uses_recorded_evidence(tmp_path):
    settings=SettingsStore(str(tmp_path/'settings.json'));store=ResearchStore(TradeDatabase(str(tmp_path/'db')))
    engine=ResearchEngine(store,tmp_path,settings,Mock())
    data={s:bars(900) for s in small_config()['symbols']}
    base=small_config();now=900*DAY_MS
    from research_engine import fingerprint
    store.put_meta('attempted_configs',{fingerprint(o['config']):now for o in choices(base)})
    with patch('research_engine.historical_compare',return_value={'passed':False}) as compare:
        engine._new_review(data,base,settings.get(),now+89*DAY_MS)
        compare.assert_not_called()
        engine._new_review(data,base,settings.get(),now+90*DAY_MS)
        compare.assert_called_once()
    evidence={'groups':[{'stage':'ADX','matured_7d':30,'avg_7d_net_pct':-2}]}
    with patch.dict('os.environ',{'ANTHROPIC_API_KEY':'','RESEARCH_ANTHROPIC_MODEL':''}):
        choice,_,source=select_proposal(choices(base),evidence)
    assert choice['after']==30 and source.startswith('Rule-based')
