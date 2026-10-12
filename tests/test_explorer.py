"""Explorer mechanics, isolation and preregistration. Fixtures are synthetic, not profit evidence."""
import copy
import json
import math
import sqlite3
from dataclasses import replace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from explorer_data import ExplorerStore,HOUR_MS as H,FRAMES,resample,validate_bars,refresh_history
from explorer_strategies import SPEC,catalogue,prepare,model_rows,signals,simulate,digest
from explorer_engine import ExplorerEngine,paired_bound,data_digest
from runtime_settings import RuntimeSettings,SettingsStore
from strategy import Candle,DAY_MS as D


def candles(days=430,phase=0):
    rows=[]
    for i in range(days*24):
        o=50*math.exp(.00001*i+.16*math.sin((i+phase)/90))
        c=o*(1+.003*math.sin(i/3))
        rows.append(Candle(i*H,o,max(o,c)*1.005,min(o,c)*.995,c,100+80*math.sin(i/8)**2,(i+1)*H-1,10000))
    return rows


def config(symbols=None):
    return {'symbols':symbols or ['BTCUSDT','ETHUSDT'],'capital':200,'risk_pct':1,
        'cost':{'fee':.0025,'slip':.0005,'min_notional':5},
        'rules':{'entry_period':55,'exit_period':20,'atr_period':20,'atr_multiplier':2,'adx_period':14,'adx_threshold':25}}


@pytest.fixture(scope='module')
def market():
    return {'BTCUSDT':candles(),'ETHUSDT':candles(phase=27)}


def item(key):
    return next(c for c in catalogue() if c['id']==key)


def test_catalogue_is_finite_deterministic_and_controlled_ancestry_is_single_factor():
    items=catalogue();lookup={c['id']:c for c in items}
    assert len(lookup)==len(items)==SPEC['max_candidates']==363
    assert items==catalogue()
    assert [c['frame'] for c in items[:3]]==['1h','4h','1d']
    assert all(c['family']=='learned_tree' for c in items[:3])
    assert all(c['hypothesis'] for c in items)
    for c in items:
        if c['parent_id']:
            p=lookup[c['parent_id']]
            assert c['kind']=='CONTROLLED_CHANGE' and c['change']['before']!=c['change']['after']
            assert c['frame']==p['frame']
            assert sum(c[k]!=p[k] for k in ['variant','family','filter'])==1
            assert items.index(p)<items.index(c)
        else:assert c['kind']=='DISCOVERY' and c['change'] is None
    assert SPEC['family_alpha']/(363*3*2)*SPEC['bootstrap_samples']>=10


def test_resampling_uses_only_completed_candles_and_monday_weeks():
    bars=candles(15)
    assert len(resample(bars,4*H,7*H))==1
    assert resample(bars,4*H,7*H)[0].close==bars[3].close
    assert resample(bars,D,D-1)==[]
    weekly=resample(bars,7*D,15*D)
    assert weekly[0].open_time==4*D # 1970-01-05, Monday
    assert weekly[0].close_time==11*D-1
    assert resample(bars,H,2*H)[0] is bars[0]
    with pytest.raises(ValueError,match='Gap'):resample(bars[:8]+bars[9:],4*H,15*D)
    with pytest.raises(ValueError,match='alignment'):validate_bars([replace(bars[0],close_time=H)],H)
    with pytest.raises(ValueError,match='Non-finite'):validate_bars([replace(bars[0],close=float('nan'))],H)


def test_download_resumes_round_robin_and_never_overwrites_or_invents_prices(tmp_path):
    path=tmp_path/'explorer.sqlite3';store=ExplorerStore(path);bars=candles(1);calls=[]
    def page(s,start,end):
        calls.append((s,start,end));return bars[start//H:min(start//H+6,end//H)]
    first=refresh_history(store,['BTCUSDT','ETHUSDT'],0,D,page,max_pages=2)
    assert first['available']==0 and [r['bars'] for r in first['coins']]==[6,6]
    store=ExplorerStore(path)
    final=refresh_history(store,['BTCUSDT','ETHUSDT'],0,D,page,max_pages=10)
    assert final['available']==2 and final['pages_this_batch']==6
    assert [c[0] for c in calls[:4]]==['BTCUSDT','ETHUSDT']*2
    store.append('BTCUSDT',[replace(bars[0],volume=999)])
    assert store.load('BTCUSDT',D)[0].volume==bars[0].volume
    assert refresh_history(store,['BTCUSDT'],0,D,page)['pages_this_batch']==0
    bad=refresh_history(store,['NEW'],0,D,lambda *a:bars[1:])
    assert 'Missing first' in bad['errors']['NEW'] and store.bounds('NEW')[2]==0
    assert data_digest({'BTCUSDT':bars})!=data_digest({'BTCUSDT':[replace(bars[0],volume=999)]+bars[1:]})


def test_rate_limit_stops_batch_and_stop_callback_makes_no_request(tmp_path):
    store=ExplorerStore(tmp_path/'e.sqlite3');get=Mock(side_effect=RuntimeError('rate limit'))
    status=refresh_history(store,['BTCUSDT','ETHUSDT'],0,D,get)
    assert get.call_count==1 and status['available']==0
    get.reset_mock();refresh_history(store,['BTCUSDT'],0,D,get,lambda:True)
    get.assert_not_called()


@pytest.mark.parametrize('frame',['1h','4h','1d'])
def test_features_and_signals_do_not_look_at_future_candles(market,frame):
    p=prepare(market,frame,430*D);prefix=prepare(market,frame,420*D)
    for s in p:
        n=len(prefix[s]['time'])
        np.testing.assert_allclose(p[s]['x'][:n],prefix[s]['x'],equal_nan=True)
        np.testing.assert_array_equal(p[s]['higher'][:n],prefix[s]['higher'])
    for c in (c for c in catalogue() if c['frame']==frame and c['family']!='learned_tree'):
        a,b=signals(p,c),signals(prefix,c)
        for s in a:
            n=len(b[s]['buy'])
            np.testing.assert_array_equal(a[s]['buy'][:n],b[s]['buy'])
            np.testing.assert_array_equal(a[s]['sell'][:n],b[s]['sell'])


def test_single_factor_variants_preserve_exit_rules_and_combination_is_one_filter(market):
    p=prepare(market,'1h',430*D)
    for c in catalogue():
        if c['frame']!='1h' or not c['parent_id']:continue
        parent=item(c['parent_id']);a=signals(p,c);b=signals(p,parent)
        for s in p:
            if c['family'] not in ('ema_trend','ema_and_momentum','momentum') or c['filter']!='none':
                np.testing.assert_array_equal(a[s]['sell'],b[s]['sell'])
            if c['change']['factor']=='entry condition':
                assert np.all(~a[s]['buy'] | b[s]['buy'])
    # EMA fast length and momentum lookback are shared entry/exit factors.
    assert item('1h:ema_trend:v2:none')['change']['factor']=='fast_ema'


def test_next_open_execution_costs_fixed_hold_and_daily_pairing(market):
    p=prepare(market,'1h',430*D);start=420*D;end=423*D
    sig={s:{'buy':np.zeros(len(v['time']),dtype=bool),'sell':np.zeros(len(v['time']),dtype=bool),'hold_bars':7} for s,v in p.items()}
    sig['BTCUSDT']['buy'][420*24]=True
    out=simulate(p,sig,'1h',config(),start,end)
    assert len(out['returns'])==3 and out['metrics']['closed_trades']==1
    t=out['trades'][0]
    assert t['entry_ms']==start+H and t['exit_ms']==start+8*H
    no_cost=config();no_cost['cost']={'fee':0,'slip':0,'min_notional':5}
    assert out['metrics']['net_profit']<simulate(p,sig,'1h',no_cost,start,end)['metrics']['net_profit']
    assert math.prod(1+r for r in out['returns'])==pytest.approx(1+out['metrics']['return_pct']/100)
    assert out['metrics']['ending_equity']>=0


def test_model_labels_are_purged_at_cutoff_and_sampling_is_shared_by_all_coins(market):
    p=prepare(market,'1h',430*D);rows=model_rows(p,'1h',config(),420*D)
    assert max(r['label_available_ms'] for r in rows)<420*D
    counts={}
    for r in rows:counts[r['feature_ms']]=counts.get(r['feature_ms'],0)+1
    assert all(n==2 for n in counts.values())
    assert len(rows)<430*2
    for r in rows[:20]:
        i=(r['feature_ms']-H)//H;cost=config()['cost']
        expected=100*(p[r['symbol']]['open'][i+8]*(1-cost['slip'])*(1-cost['fee'])/(p[r['symbol']]['open'][i+1]*(1+cost['slip'])*(1+cost['fee']))-1)
        assert r['y']==pytest.approx(expected)


def test_historical_compares_parent_on_identical_dates_and_records_reject(tmp_path,market):
    engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:RuntimeSettings())
    c=item('1h:rsi_reversion:v2:none');parent=item(c['parent_id'])
    cfg=config();daily={s:resample(b,D,430*D) for s,b in market.items()}
    trial=engine.store.new_trial(c,430*D,{'parent':parent,'config_hash':digest(cfg)})
    engine._historical(trial,market,daily,cfg,430*D,430*D)
    h=engine.store.trial(trial['id'])['payload']['historical']
    assert h['candidate']['start_ms']==h['parent']['start_ms']==h['baseline']['start_ms']
    assert len(h['folds'])==3 and all(f['parent'] for f in h['folds'])
    assert 'parent_net_return' in h['gates'] and trial['status']=='REJECTED' # two coins cannot pass breadth=5
    with pytest.raises(sqlite3.IntegrityError):engine.store.new_trial(c,431*D,{})
    assert len(ExplorerStore(tmp_path/'e.sqlite3').trials())==1


def test_forward_preregistration_fifo_and_frozen_parent(tmp_path):
    engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:RuntimeSettings());cfg=config()
    for c in catalogue()[3:8]:
        t=engine.store.new_trial(c,10*D,{'config_hash':digest(cfg),'parent':None})
        engine.store.stage(t,10*D,'WAITING_SLOT','ready')
    engine._activate(cfg,10*D+H)
    active=[t for t in engine.store.trials() if t['status']=='OBSERVING']
    assert sorted(t['id'] for t in active)==[1,2,3]
    assert all(t['payload']['forward_start_ms']==11*D for t in active)
    t=active[0];t['payload']['candidate']['variant']=999
    engine._forward(t,{}, {},cfg,12*D,12*D)
    assert engine.store.trial(t['id'])['status']=='ERROR'


def test_forward_progress_cannot_accept_before_declared_checkpoint(tmp_path,market):
    engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:RuntimeSettings());cfg=config()
    c=item('1h:rsi_reversion:v2:none');parent=item(c['parent_id'])
    t=engine.store.new_trial(c,418*D,{'config_hash':digest(cfg),'parent':parent})
    engine.store.stage(t,418*D,'WAITING_SLOT','ready');engine._activate(cfg,419*D)
    t=engine.store.trial(t['id']);daily={s:resample(b,D,430*D) for s,b in market.items()}
    with patch('explorer_engine.paired_bound') as bound:
        engine._forward(t,market,daily,cfg,430*D,430*D)
        bound.assert_not_called()
    p=engine.store.trial(t['id'])['payload']
    assert p['observed_days']==10 and p['next_evaluation_ms']==510*D
    assert 'gates' not in p['forward'] and p['forward']['parent']
    assert engine.store.trial(t['id'])['status']=='OBSERVING'


def test_run_budget_pause_and_interruption_preserve_attempts(tmp_path):
    settings=RuntimeSettings();engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:settings,get_page=Mock())
    cfg=engine._config(1100*D);engine.store.put('data',{'end_ms':1100*D,'available':19})
    for c in cfg['catalogue'][:12]:engine.store.new_trial(c,1099*D,{'config_hash':digest(cfg)})
    assert engine.run(1100*D) is False
    assert len(engine.store.trials())==12 and all(t['status']=='ERROR' for t in engine.store.trials())
    engine.get_page.assert_not_called()
    settings.explorer_enabled=False;assert engine.run(1101*D) is False
    engine.get_page.assert_not_called()
    with patch('explorer_engine.time.time',return_value=1100*D/1000):snap=engine.snapshot()
    assert len(snap['trials'])==12 and all('payload' not in t for t in snap['trials'])
    assert snap['recent_attempts']==12


def test_bootstrap_paired_uncertainty_resolution_and_determinism():
    alpha=SPEC['family_alpha']/(363*3*2)
    assert paired_bound([0]*90,[.01]*90,alpha,1,1000) is None
    assert paired_bound([0]*90,[.01]*90,alpha,1,SPEC['bootstrap_samples'])==pytest.approx(.01)
    assert paired_bound([.01]*90,[0]*90,alpha,1,SPEC['bootstrap_samples'])==pytest.approx(-.01)
    a=[.01*math.sin(i) for i in range(90)];b=[.01*math.cos(i) for i in range(90)]
    assert paired_bound(a,b,.05,2,1000)==paired_bound(a,b,.05,2,1000)


def test_research_settings_cannot_mutate_execution_or_position_files(tmp_path):
    settings=SettingsStore(str(tmp_path/'settings.json'))
    before=settings.get();state=tmp_path/'state.json';state.write_text('{"open_positions":{"BTCUSDT":{"quantity":0.1,"exit_period":20}}}')
    content=state.read_bytes();engine=ExplorerEngine(tmp_path/'e.sqlite3',settings.get)
    assert not hasattr(engine,'exec_client') and not hasattr(engine,'promote')
    settings.update({'explorer_enabled':False})
    assert settings.get().strategy_version==before.strategy_version and state.read_bytes()==content
    assert engine.run() is False
    with pytest.raises(ValueError):settings.update({'explorer_enabled':'yes'})


@pytest.mark.parametrize('frame',['1h','4h','1d'])
def test_actual_model_fit_across_timeframes_purges_training_and_freezes_rules(tmp_path,frame):
    data={'BTCUSDT':candles(1000)};cfg=config(['BTCUSDT'])
    engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:RuntimeSettings())
    c=item(f'{frame}:learned_tree:v1:none')
    trial=engine.store.new_trial(c,1000*D,{'config_hash':digest(cfg),'parent':None})
    engine._historical(trial,data,{'BTCUSDT':resample(data['BTCUSDT'],D,1000*D)},cfg,1000*D,1000*D)
    saved=engine.store.trial(trial['id']);p=saved['payload']
    assert p['tree'] and p['model']['training_rows']>365
    assert all(f['last_training_label_ms']<f['start_ms'] for f in p['historical']['folds'])
    assert p['model']['last_training_label_ms']<1000*D
    assert all('bar' in c['feature'] or c['feature'] in ('range_pct','close_in_range') for r in p['model']['rules'] for c in r['conditions'])
    assert saved['status']=='REJECTED' # deliberately insufficient breadth, not selected profit evidence
    json.dumps(saved,allow_nan=False)


@pytest.mark.parametrize('parent_lower,expected',[(.001,'PROMISING_PAPER_ONLY'),(-.001,'REJECTED')])
def test_checkpoint_requires_fresh_parent_evidence_and_never_promotes(tmp_path,parent_lower,expected):
    cfg=config();engine=ExplorerEngine(tmp_path/'e.sqlite3',lambda:RuntimeSettings())
    c=item('1h:rsi_reversion:v2:none')
    t=engine.store.new_trial(c,10*D,{'config_hash':digest(cfg),'parent':item(c['parent_id'])})
    engine.store.stage(t,10*D,'WAITING_SLOT','ready');engine._activate(cfg,10*D)
    t=engine.store.trial(t['id'])
    metrics={'return_pct':5.,'max_drawdown_pct':2.,'closed_trades':25,'symbols_traded':8,'start_ms':11*D,'end_ms':101*D,'days':90}
    base={'metrics':metrics,'returns':[0.]*90,'trades':[]}
    candidate={'metrics':{**metrics,'return_pct':8.,'max_drawdown_pct':1.},'returns':[.001]*90,'trades':[]}
    with patch('explorer_engine.prepare',return_value={}),patch('explorer_engine.signals',return_value={}),patch('explorer_engine.simulate',side_effect=[candidate,base]),patch('explorer_engine.donchian_simulate',return_value=base),patch('explorer_engine.paired_bound',side_effect=[.001,parent_lower]) as bound:
        engine._forward(t,{}, {},cfg,101*D,101*D)
    saved=engine.store.trial(t['id'])
    assert saved['status']==expected and len(saved['payload']['checkpoints'])==1
    assert bound.call_count==2 and saved['payload']['forward']['gates']['parent_paired_lower_bound']==(parent_lower>0)
    assert saved['payload']['forward']['adjusted_alpha']==SPEC['family_alpha']/(363*3*2)
    assert not hasattr(engine,'promote')
