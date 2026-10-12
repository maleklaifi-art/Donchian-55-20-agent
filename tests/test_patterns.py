"""Pattern research: genuine fitting, chronological isolation and paper-only state."""
import copy
import json
import math
from dataclasses import asdict
from unittest.mock import Mock, patch

import pytest

from pattern_model import (FEATURES,SPEC,feature_rows,labelled_rows,fit_tree,predict,rules,
                           train_and_validate,paper_portfolio,net_return)
from pattern_engine import PatternEngine
from pattern_store import PatternStore
from runtime_settings import SettingsStore
from strategy import Candle,DAY_MS


def candles(n=850,phase=0):
    rows=[]
    for i in range(n):
        price=30*math.exp(.0005*i+.18*math.sin((i+phase)/17))
        close=price*(1+.005*math.sin(i))
        rows.append(Candle(i*DAY_MS,price,max(price,close)*1.012,min(price,close)*.988,
                           close,1000+300*math.sin((i+phase)/7),(i+1)*DAY_MS-1,30000))
    return rows


def config():
    return {'symbols':['BTCUSDT','ETHUSDT'],'capital':200,'risk_pct':1,
        'cost':{'fee':.0025,'slip':.0005,'min_notional':5},
        'rules':{'entry_period':55,'exit_period':20,'atr_period':20,'atr_multiplier':2,
                 'adx_period':14,'adx_threshold':25}}


def test_features_are_point_in_time_and_not_filtered_by_donchian():
    data={'BTCUSDT':candles(100),'ETHUSDT':candles(100,7)}
    rows=feature_rows(data,config()['symbols'])
    assert len(rows)==40*2 and len(rows[0]['x'])==len(FEATURES)
    assert len({r['feature_ms'] for r in rows})==40
    # Modifying only a future candle cannot alter any earlier feature.
    changed=copy.deepcopy(data)
    b=changed['ETHUSDT'][-1]
    changed['ETHUSDT'][-1]=Candle(b.open_time,100,110,90,105,1,b.close_time,100)
    assert feature_rows(changed,config()['symbols'])[:-2]==rows[:-2]
    assert any(r['x'][0]<0 for r in rows) and any(r['x'][0]>0 for r in rows)


def test_target_is_seven_days_from_future_entry_and_matures_only_when_available():
    data={'BTCUSDT':candles(100),'ETHUSDT':candles(100,7)}
    row=feature_rows(data,config()['symbols'])[0] # feature closes at day 61 boundary
    assert row['feature_ms']==61*DAY_MS
    assert row['entry_ms']==62*DAY_MS and row['exit_ms']==69*DAY_MS
    assert labelled_rows([row],data,config()['cost'],69*DAY_MS)==[]
    labelled=labelled_rows([row],data,config()['cost'],70*DAY_MS)[0]
    assert labelled['label_available_ms']==70*DAY_MS
    assert labelled['y']==pytest.approx(net_return(data[row['symbol']][62].open,data[row['symbol']][69].open,config()['cost']))
    assert labelled['y']<net_return(data[row['symbol']][62].open,data[row['symbol']][69].open,{'fee':0,'slip':0})


def test_tree_learns_data_dependent_patterns_and_json_reproduces_predictions():
    rows=[]
    for i in range(1000):
        value=(i%100)/10-5
        rows.append({'x':[value]+[0]*10,'y':4 if value>0 else -3,'feature_ms':i*DAY_MS})
    tree=fit_tree(rows,depth=2,min_leaf=40,min_days=20)
    assert tree['feature']==0
    assert predict(tree,[4]+[0]*10)['prediction_pct']>3
    assert predict(tree,[-4]+[0]*10)['prediction_pct']<0
    restored=json.loads(json.dumps(tree,allow_nan=False))
    assert predict(restored,[4]+[0]*10)==predict(tree,[4]+[0]*10)
    assert all(r['conditions'] and r['days']>=20 for r in rules(tree))
    # Opposite training outcomes reverse the learned forecast; not hardcoded rules.
    for r in rows:r['y']=-r['y']
    reverse=fit_tree(rows,depth=2,min_leaf=40,min_days=20)
    assert predict(reverse,[4]+[0]*10)['prediction_pct']<0


def test_chronological_validation_purges_all_coins_and_reports_negative_results():
    rows=[]
    for day in range(800):
        for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            x=math.sin(day/17)
            rows.append({'symbol':symbol,'x':[x]+[0]*10,'y':(-1 if day<600 else 1)*x,
                'feature_ms':day*DAY_MS,'label_available_ms':(day+9)*DAY_MS})
    actual_fit=fit_tree
    training_sets=[]
    def spy(training,*args):
        training_sets.append(training)
        return actual_fit(training,*args)
    with patch('pattern_model.fit_tree',side_effect=spy):
        result,predictions=train_and_validate(rows,config())
    assert len(training_sets)==4 and len(predictions)==270*3
    for training,fold in zip(training_sets,result['validation']['folds']):
        assert all(r['label_available_ms']<fold['test_start_ms'] for r in training)
        assert fold['test_days']==90 and fold['test_rows']==270
        assert {r['symbol'] for r in training}=={'BTCUSDT','ETHUSDT','SOLUSDT'}
    assert 'not a fresh holdout' in result['validation']['label']
    assert any(not f['beats_mean_baseline'] for f in result['validation']['folds'])


def test_paper_portfolio_obeys_dates_costs_cash_and_fixed_exits():
    cfg=config();cfg['symbols']=['BTCUSDT','ETHUSDT','SOLUSDT','ADAUSDT']
    data={s:candles(50,i*3) for i,s in enumerate(cfg['symbols'])}
    forecasts=[{'symbol':s,'issued_ms':29*DAY_MS,'entry_ms':30*DAY_MS,'exit_ms':37*DAY_MS,
                'prediction_pct':5-i*.1,'atr':2,'model_id':1} for i,s in enumerate(cfg['symbols'])]
    before=paper_portfolio(data,forecasts,cfg,30*DAY_MS,37*DAY_MS)
    assert before['metrics']['closed_trades']==0 and before['metrics']['open_positions']==3
    after=paper_portfolio(data,forecasts,cfg,30*DAY_MS,38*DAY_MS)
    assert after['metrics']['closed_trades']==3 and after['metrics']['open_positions']==0
    assert after['metrics']['skipped_entries']==1
    assert after['metrics']['cash']>=0 and all(v>=0 for _,v in after['equity'])
    for t in after['trades']:
        assert t['entry_time_ms']==30*DAY_MS and t['exit_time_ms']==37*DAY_MS
        s=t['symbol'];cost=cfg['cost']
        expected=t['quantity']*data[s][37].open*(1-cost['slip'])*(1-cost['fee'])-t['cost']
        assert t['pnl']==pytest.approx(expected)
    late=copy.deepcopy(forecasts);late[0]['issued_ms']=late[0]['entry_ms']
    with pytest.raises(ValueError,match='before its entry'):
        paper_portfolio(data,late,cfg,30*DAY_MS,38*DAY_MS)
    del data['BTCUSDT'][31]
    with pytest.raises(ValueError,match='Missing paper'):
        paper_portfolio(data,forecasts,cfg,30*DAY_MS,38*DAY_MS)


def test_forecast_audit_is_immutable_and_restart_safe(tmp_path):
    path=tmp_path/'patterns.sqlite3';store=PatternStore(path)
    row={'symbol':'BTCUSDT','feature_ms':DAY_MS,'entry_ms':2*DAY_MS,'exit_ms':9*DAY_MS,
         'issued_ms':DAY_MS+10,'prediction_pct':2,'leaf_id':'rootR','model_id':1,'atr':1,'x':[0]*11}
    store.record_forecasts([row]);store.record_forecasts([{**row,'prediction_pct':99,'model_id':2}])
    saved=PatternStore(path).forecasts()['rows'][0]
    assert saved['prediction_pct']==2 and saved['model_id']==1
    store.outcome(saved['id'],{'net_return_pct':-3,'squared_error':25})
    store.outcome(saved['id'],{'net_return_pct':99,'squared_error':0})
    assert store.forecasts()['rows'][0]['outcome']['net_return_pct']==-3
    assert store.scorecard()[0]['rmse_pct']==5
    with pytest.raises(ValueError,match='precede entry'):
        store.record_forecasts([{**row,'symbol':'ETHUSDT','issued_ms':2*DAY_MS}])
    assert store.forecasts()['total']==1


def write_cache(root,data):
    for symbol,bars in data.items():
        (root/(symbol+'.json')).write_text(json.dumps({'version':1,'symbol':symbol,'candles':[asdict(b) for b in bars]}))


def test_engine_runs_real_training_then_forecasts_and_matures_without_touching_trades(tmp_path):
    # Two symbols keeps this integration fixture small; production universe is fixed19.
    settings=SettingsStore(str(tmp_path/'settings.json'))
    state=tmp_path/'state.json';state.write_text('{"open_positions":{"KEEP":{"quantity":2,"exit_period":20}}}')
    original=state.read_bytes()
    data={'BTCUSDT':candles(810),'ETHUSDT':candles(810,7)};write_cache(tmp_path,data)
    with patch('pattern_engine.DEFAULT_UNIVERSE_SYMBOLS',['BTCUSDT','ETHUSDT']):
        engine=PatternEngine(tmp_path/'patterns.sqlite3',tmp_path,settings.get)
        assert engine.run(now_ms=810*DAY_MS+1000)
    snap=engine.snapshot();assert len(snap['models'])==1 and snap['forecasts']['total']==2
    assert snap['paper']['start_ms']==811*DAY_MS
    assert not engine.run(now_ms=810*DAY_MS+2000) # No duplicate fitting or forecasts.
    for b in snap['forecasts']['rows']:
        assert b['issued_ms']<b['entry_ms'] and b['outcome'] is None
    # Restart and jump forward: score old forecasts; do not fill missed forecast days.
    data={'BTCUSDT':candles(820),'ETHUSDT':candles(820,7)};write_cache(tmp_path,data)
    engine=PatternEngine(tmp_path/'patterns.sqlite3',tmp_path,settings.get)
    assert engine.run(now_ms=820*DAY_MS+1000)
    snap=engine.snapshot();assert len(snap['models'])==1 and snap['forecasts']['total']==4
    assert sum(r['outcome'] is not None for r in snap['forecasts']['rows'])==2
    assert snap['paper']['pattern']['days']==9
    assert snap['paper']['pattern']['start_ms']==snap['paper']['donchian']['start_ms']
    assert state.read_bytes()==original and not (tmp_path/'settings.json').exists()
    assert not hasattr(engine,'exec_client') and not hasattr(engine,'promote')
    frozen=copy.deepcopy(snap['config'])
    settings.update({'starting_capital_usdt':777,'risk_per_trade_pct':2})
    assert engine._config(settings.get(),820*DAY_MS)==frozen
    settings.update({'pattern_enabled':False})
    assert not engine.run(now_ms=821*DAY_MS)


def test_pattern_worker_waits_for_scan_and_missing_data(tmp_path):
    settings=SettingsStore(str(tmp_path/'settings.json'))
    engine=PatternEngine(tmp_path/'patterns.sqlite3',tmp_path,settings.get,lambda:True)
    assert not engine.run(now_ms=100*DAY_MS)
    engine.scan_busy=lambda:False
    assert not engine.run(now_ms=100*DAY_MS)
    assert engine.snapshot()['status']['stage']=='WAITING_DATA'
    assert engine.snapshot()['data']['available']==0 and engine.snapshot()['data']['required']==19
    assert engine.snapshot()['models']==[]
