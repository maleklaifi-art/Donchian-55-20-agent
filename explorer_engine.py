"""Budgeted multi-timeframe explorer. Public data and paper replay only.

Candidate catalogue, search order, budget and validation protocol are frozen before
results. This worker cannot place orders or modify any trading strategy/settings.
"""
import copy
import json
import hashlib
import threading
import time
from dataclasses import astuple

from explorer_data import ExplorerStore,FRAMES,HOUR_MS,resample,refresh_history,fetch_page
from explorer_strategies import SPEC,FAMILIES,FILTERS,ML_FEATURES,catalogue,digest,prepare,model_rows,signals,simulate
from pattern_model import fit_tree,rules,FEATURES
from research_backtest import simulate as donchian_simulate
from strategy import DEFAULT_UNIVERSE_SYMBOLS,DAY_MS


def data_digest(hourly):
    h=hashlib.sha256()
    for symbol,bars in sorted(hourly.items()):
        h.update(symbol.encode())
        for bar in bars:h.update(json.dumps(astuple(bar),allow_nan=False).encode())
    return h.hexdigest()


def paired_bound(control,candidate,alpha,seed,samples):
    """Seeded circular 7-day block bootstrap; chunked to bound peak memory."""
    import numpy as np
    if len(control)!=len(candidate) or len(control)<30 or alpha*samples<10:return None
    delta=np.asarray(candidate)-np.asarray(control)
    if not np.isfinite(delta).all():raise ValueError('Invalid paired daily returns')
    n=len(delta);block=7;full,tail=divmod(n,block);rng=np.random.default_rng(seed)
    doubled=np.r_[delta,delta[:block]];prefix=np.r_[0.,np.cumsum(doubled)]
    block_sums=prefix[np.arange(n)+block]-prefix[np.arange(n)]
    means=np.empty(samples)
    for lo in range(0,samples,2000):
        hi=min(samples,lo+2000);ix=rng.integers(0,n,size=(hi-lo,full))
        total=block_sums[ix].sum(axis=1)
        if tail:
            j=rng.integers(0,n,size=hi-lo);total+=prefix[j+tail]-prefix[j]
        means[lo:hi]=total/n
    return float(np.quantile(means,alpha))


class ExplorerEngine:
    def __init__(self,path,get_settings,scan_busy=lambda:False,get_page=fetch_page):
        self.store=ExplorerStore(path);self.get_settings=get_settings;self.scan_busy=scan_busy;self.get_page=get_page
        self.lock=threading.Lock();self.wakeup=threading.Event();self.busy=False;self.error=None
        self.retry_after_ms=0

    def start(self,stop_event):
        def loop():
            while not stop_event.is_set():
                self.wakeup.wait(60);self.wakeup.clear()
                if stop_event.is_set():break
                try:self.run(stop_event=stop_event)
                except Exception as exc:
                    self.error=str(exc)[:500]
                    print(json.dumps({'event':'EXPLORER_ERROR','error':self.error}),flush=True)
        threading.Thread(target=loop,name='strategy-explorer',daemon=True).start()

    def _config(self,now_ms):
        config=self.store.meta('config')
        if config is None:
            s=self.get_settings();items=catalogue()
            config={'created_ms':now_ms,'history_start_ms':now_ms//DAY_MS*DAY_MS-SPEC['history_days']*DAY_MS,
                'symbols':list(DEFAULT_UNIVERSE_SYMBOLS),'capital':s.starting_capital_usdt,'risk_pct':s.risk_per_trade_pct,
                'cost':{'fee':s.research_fee_bps/10000,'slip':s.research_slippage_bps/10000,'min_notional':s.research_min_notional},
                'rules':{'entry_period':55,'exit_period':20,'adx_period':14,'adx_threshold':25,'atr_period':20,'atr_multiplier':2},
                'spec':copy.deepcopy(SPEC),'catalogue':items,'catalogue_hash':digest(items)}
            self.store.put('config',config)
            self.store.event(now_ms,'REGISTERED',f"Registered {len(items)} hypotheses across 1H/4H/1D, with a fixed search order and {SPEC['weekly_budget']} tests per seven days.")
        return config

    def run(self,now_ms=None,stop_event=None):
        provided=now_ms is not None;now_ms=now_ms or int(time.time()*1000)
        if now_ms<self.retry_after_ms or self.scan_busy() or not self.lock.acquire(blocking=False):return False
        self.busy=True;trial=None
        try:
            if not self.get_settings().explorer_enabled:return False
            config=self._config(now_ms)
            if config['spec']!=SPEC or config['catalogue_hash']!=digest(catalogue()):
                self.store.event(now_ms,'PAUSED','Explorer specification changed. Preserve existing trials; explicitly register a new experiment before changing the search.')
                return False
            should_stop=lambda:self.scan_busy() or (stop_event is not None and stop_event.is_set())
            last=self.store.meta('data',{})
            hour=now_ms//HOUR_MS*HOUR_MS
            if last.get('end_ms')!=hour or last.get('available')!=len(config['symbols']):
                status=refresh_history(self.store,config['symbols'],config['history_start_ms'],now_ms,self.get_page,should_stop)
            else:status=last
            if status['available']!=len(config['symbols']):
                self.store.put('status',{'stage':'DOWNLOADING','message':f"Hourly history ready for {status['available']}/{status['required']} coins. Each downloaded page is saved; no gap is filled synthetically.",'updated_ms':now_ms})
                self.retry_after_ms=now_ms+(15*60000 if status['errors'] else 60000)
                return False
            if should_stop():return False
            # Recover interrupted trials without erasing them or reusing their trial numbers.
            all_trials=self.store.trials()
            for old in all_trials:
                if old['status']=='RUNNING':self.store.stage(old,now_ms,'ERROR','Interrupted run retained as an attempted hypothesis; no silent retry.')
            self._activate(config,now_ms)
            all_trials=self.store.trials();day=now_ms//DAY_MS*DAY_MS
            due=[t for t in all_trials if t['status']=='OBSERVING' and t['payload'].get('last_forward_day')!=day]
            tested={t['candidate_id'] for t in all_trials}
            attempts=sum(t['registered_ms']>now_ms-7*DAY_MS for t in all_trials)
            next_candidate=next((c for c in config['catalogue'] if c['id'] not in tested),None)
            if not due and (attempts>=SPEC['weekly_budget'] or next_candidate is None):
                self.store.put('status',{'stage':'OBSERVING','message':'Search budget used or catalogue exhausted. Existing forward experiments continue; no repeated mining of the same history.','updated_ms':now_ms})
                return False
            hourly={s:self.store.load(s,day) for s in config['symbols']}
            if not all(b and b[-1].close_time==day-1 for b in hourly.values()):raise ValueError('Full current UTC day is not available for every coin')
            daily={s:resample(b,DAY_MS,day) for s,b in hourly.items()}
            if due:
                # One unit of expensive work per tick; keep execution responsive.
                self._forward(due[-1],hourly,daily,config,day,now_ms)
                return True
            trial=self.store.new_trial(next_candidate,now_ms,{'config_hash':digest(config),'history_end_ms':day,
                'policy':copy.deepcopy(SPEC),'catalogue_hash':config['catalogue_hash'],
                'parent':copy.deepcopy(next((c for c in config['catalogue'] if c['id']==next_candidate['parent_id']),None)),
                'data_hash':data_digest(hourly)})
            self.store.stage(trial,now_ms,'RUNNING',f"Testing #{trial['id']} — {next_candidate['frame']} / {next_candidate['label']}")
            self._historical(trial,hourly,daily,config,day,now_ms)
            finished=now_ms if provided else int(time.time()*1000)
            self._activate(config,finished)
            self.error=None
            return True
        except Exception as exc:
            self.error=str(exc)[:500];self.retry_after_ms=now_ms+15*60000
            if trial and trial['status']=='RUNNING':self.store.stage(trial,now_ms,'ERROR',self.error)
            else:self.store.event(now_ms,'ERROR',self.error)
            return False
        finally:
            self.busy=False;self.lock.release()

    def _historical(self,trial,hourly,daily,config,end,now_ms):
        import numpy as np
        c=trial['payload']['candidate'];frame=c['frame'];interval=FRAMES[frame]
        prepared=prepare(hourly,frame,end);start=end-SPEC['fold_days']*SPEC['folds']*DAY_MS
        folds=[];whole_signals=None;frozen_tree=None
        parent=trial['payload'].get('parent')
        parent_signals=signals(prepared,parent) if parent else None
        for fold in range(SPEC['folds']):
            lo=start+fold*SPEC['fold_days']*DAY_MS;hi=lo+SPEC['fold_days']*DAY_MS
            training=None;tree=None
            if c['family']=='learned_tree':
                training=model_rows(prepared,frame,config,lo)
                tree=fit_tree(training,depth=3,min_leaf=200,min_days=30)
            sig=signals(prepared,c,tree)
            if whole_signals is None:
                whole_signals={s:{'buy':np.zeros(len(p['time']),dtype=bool),'sell':np.zeros(len(p['time']),dtype=bool),'hold_bars':sig[s]['hold_bars']} for s,p in prepared.items()}
            for s,p in prepared.items():
                mask=(p['time']>=lo)&(p['time']<hi)
                whole_signals[s]['buy'][mask]=sig[s]['buy'][mask];whole_signals[s]['sell'][mask]=sig[s]['sell'][mask]
            result=simulate(prepared,sig,frame,config,lo,hi)
            baseline=donchian_simulate(daily,config,lo,hi)
            folds.append({'start_ms':lo,'end_ms':hi,'candidate':result['metrics'],'baseline':baseline['metrics'],
                'parent':simulate(prepared,parent_signals,frame,config,lo,hi)['metrics'] if parent else None,
                'last_training_label_ms':max(r['label_available_ms'] for r in training) if training else None})
        candidate=simulate(prepared,whole_signals,frame,config,start,end)
        baseline=donchian_simulate(daily,config,start,end)
        stress=copy.deepcopy(config);stress['cost']['fee']*=2;stress['cost']['slip']*=2
        stressed=simulate(prepared,whole_signals,frame,stress,start,end)
        a,b=baseline['metrics'],candidate['metrics']
        gates={'net_return':b['return_pct']>max(0,a['return_pct']),
            'drawdown':b['max_drawdown_pct']<=a['max_drawdown_pct'],
            'trade_count':b['closed_trades']>=SPEC['min_closed_trades'],'coin_breadth':b['symbols_traded']>=SPEC['min_coins'],
            'period_stability':sum(f['candidate']['return_pct']>0 for f in folds)>=2,
            'double_costs':stressed['metrics']['return_pct']>0}
        parent_result=simulate(prepared,parent_signals,frame,config,start,end) if parent else None
        if parent_result:
            pm=parent_result['metrics']
            gates.update(parent_net_return=b['return_pct']>pm['return_pct'],parent_drawdown=b['max_drawdown_pct']<=pm['max_drawdown_pct'])
        trial['payload']['historical']={'candidate':b,'baseline':a,'folds':folds,'double_costs':stressed['metrics'],
            'parent':parent_result['metrics'] if parent_result else None,
            'gates':gates,'label':'Known-history diagnostics; every attempted candidate remains counted. Not an untouched holdout.'}
        if c['family']=='learned_tree':
            training=model_rows(prepared,frame,config,end)
            frozen_tree=fit_tree(training,depth=3,min_leaf=200,min_days=30)
            trial['payload']['tree']=frozen_tree
            trial['payload']['model']={'features':ML_FEATURES,'training_rows':len(training),
                'last_training_label_ms':max(r['label_available_ms'] for r in training),
                'rules':[{**r,'conditions':[{**cond,'feature':ML_FEATURES[FEATURES.index(cond['feature'])]} for cond in r['conditions']]} for r in rules(frozen_tree)]}
        if all(gates.values()):
            self.store.stage(trial,now_ms,'WAITING_SLOT','Historical diagnostics passed. Queued FIFO for a new forward paper period; this is not evidence of a proven edge.')
        else:self.store.stage(trial,now_ms,'REJECTED','Historical screen failed: '+', '.join(k for k,v in gates.items() if not v))

    def _activate(self,config,now_ms):
        trials=self.store.trials();free=SPEC['forward_slots']-sum(t['status']=='OBSERVING' for t in trials)
        for trial in sorted((t for t in trials if t['status']=='WAITING_SLOT'),key=lambda t:t['id'])[:max(0,free)]:
            start=(now_ms//DAY_MS+1)*DAY_MS
            trial['payload'].update(forward_start_ms=start,look_index=0,
                next_evaluation_ms=start+SPEC['forward_looks_days'][0]*DAY_MS,
                frozen_hash=self.recipe_hash(trial['payload']))
            self.store.stage(trial,now_ms,'OBSERVING','Frozen before forward start; replay starts on the next full UTC day. No Testnet orders.')

    def _forward(self,trial,hourly,daily,config,end,now_ms):
        p=trial['payload'];p['last_forward_day']=end
        expected=self.recipe_hash(p)
        if expected!=p['frozen_hash'] or p['config_hash']!=digest(config):
            self.store.stage(trial,now_ms,'ERROR','Forward recipe changed; comparison invalidated.')
            return
        start=p['forward_start_ms'];p['observed_days']=max(0,(end-start)//DAY_MS)
        target=start+SPEC['forward_looks_days'][p['look_index']]*DAY_MS;p['next_evaluation_ms']=target
        if end<=start:self.store.save_trial(trial);return
        # Show descriptive progress; only declared checkpoints may change the verdict.
        stop=min(end,target);p['observed_days']=(stop-start)//DAY_MS
        c=p['candidate'];prepared=prepare(hourly,c['frame'],stop)
        result=simulate(prepared,signals(prepared,c,p.get('tree')),c['frame'],config,start,stop)
        baseline=donchian_simulate({s:[b for b in bars if b.close_time<stop] for s,bars in daily.items()},config,start,stop)
        parent=simulate(prepared,signals(prepared,p['parent']),c['frame'],config,start,stop) if p.get('parent') else None
        p['forward']={'candidate':result['metrics'],'baseline':baseline['metrics'],
            'parent':parent['metrics'] if parent else None,
            'label':'Prospectively registered rule replay on new candles; hypothetical fills, not actual Testnet execution.',
            'recent_trades':result['trades'][-30:]}
        if end<target:self.store.save_trial(trial);return
        a,b=baseline['metrics'],result['metrics']
        # Fixed catalogue, three looks and two comparisons per hypothesis.
        alpha=SPEC['family_alpha']/(SPEC['max_candidates']*len(SPEC['forward_looks_days'])*2)
        resolution=alpha*SPEC['bootstrap_samples']>=10
        lower=paired_bound(baseline['returns'],result['returns'],alpha,trial['id'],SPEC['bootstrap_samples']) if resolution else None
        gates={'sample':b['closed_trades']>=SPEC['min_closed_trades'],
            'breadth':b['symbols_traded']>=SPEC['min_coins'],
            'net_return':b['return_pct']>max(0,a['return_pct']),
            'drawdown':b['max_drawdown_pct']<=a['max_drawdown_pct'],
            'uncertainty_resolution':resolution,'paired_lower_bound':lower is not None and lower>0}
        if parent:
            parent_lower=paired_bound(parent['returns'],result['returns'],alpha,trial['id'],SPEC['bootstrap_samples'])
            gates.update(parent_net_return=b['return_pct']>parent['metrics']['return_pct'],
                parent_drawdown=b['max_drawdown_pct']<=parent['metrics']['max_drawdown_pct'],
                parent_paired_lower_bound=parent_lower is not None and parent_lower>0)
            p['forward']['parent_paired_lower_bound']=parent_lower
        p['forward'].update(gates=gates,adjusted_alpha=alpha,paired_lower_bound=lower,
            statistical_note='Conservative operational bootstrap screen; dependence, selection and model assumptions prevent any guarantee against overfitting.')
        p.setdefault('checkpoints',[]).append(copy.deepcopy(p['forward']))
        if all(gates.values()):
            self.store.stage(trial,now_ms,'PROMISING_PAPER_ONLY','Passed recorded forward screens. Still paper research; no execution adoption pathway.')
        elif (not gates['sample'] or not gates['breadth']) and p['look_index']+1<len(SPEC['forward_looks_days']):
            p['look_index']+=1;p['next_evaluation_ms']=start+SPEC['forward_looks_days'][p['look_index']]*DAY_MS
            self.store.stage(trial,now_ms,'OBSERVING','Insufficient sample; extend to the next date declared before the test began.')
        else:self.store.stage(trial,now_ms,'REJECTED','Forward screen failed or remained inconclusive: '+', '.join(k for k,v in gates.items() if not v))

    @staticmethod
    def recipe_hash(p):
        return digest({k:p.get(k) for k in ('candidate','parent','tree','config_hash')})

    def snapshot(self):
        try:
            config=self.store.meta('config');trials=self.store.trials();now_ms=int(time.time()*1000)
            # Compact polling response; complete evidence is fetched by trial ID.
            for t in trials:
                p=t.pop('payload');t.update(candidate=p['candidate'],last_event=p['timeline'][-1] if p['timeline'] else None,
                    next_evaluation_ms=p.get('next_evaluation_ms'),observed_days=p.get('observed_days',0))
            counts={}
            for t in trials:counts[t['status']]=counts.get(t['status'],0)+1
            return {'enabled':self.get_settings().explorer_enabled,'busy':self.busy,'spec':SPEC,'error':self.error,
                'config':{k:v for k,v in config.items() if k!='catalogue'} if config else None,
                'catalogue_size':len(catalogue()),'families':FAMILIES+['learned_tree'],'filters':FILTERS,
                'attempted':len(trials),'recent_attempts':sum(t['registered_ms']>now_ms-7*DAY_MS for t in trials),
                'counts':counts,'trials':trials,'data':self.store.meta('data'),
                'status':self.store.meta('status',{'stage':'WAITING','message':'Waiting for hourly data download to start.'})}
        except Exception as exc:return {'enabled':False,'status':{'stage':'ERROR','message':str(exc)[:500]}}
