"""Read-only consumer of candles; trains models and maintains a paper ledger.

No exchange client, state file, Donchian promotion callback, or settings writer is
available to this engine. Its only write target is patterns.sqlite3.
"""
import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict

from pattern_model import (FEATURES, SPEC, feature_rows, labelled_rows, net_return,
                           train_and_validate, predict, paper_portfolio)
from pattern_store import PatternStore
from research_engine import load_history
from research_backtest import simulate
from strategy import DEFAULT_UNIVERSE_SYMBOLS, DAY_MS


class PatternEngine:
    def __init__(self,path,history_dir,get_settings,scan_busy=lambda:False):
        self.store=PatternStore(path);self.history_dir=history_dir
        self.get_settings=get_settings;self.scan_busy=scan_busy
        self.lock=threading.Lock();self.wakeup=threading.Event();self.busy=False
        self.last_error=None;self.last_error_ms=0

    def start(self,stop_event):
        def loop():
            while not stop_event.is_set():
                self.wakeup.wait(300);self.wakeup.clear()
                if stop_event.is_set():break
                try:self.run()
                except Exception as exc:
                    # Isolate even storage failures from both execution and this loop.
                    self.last_error=str(exc)[:500]
                    print(json.dumps({'event':'PATTERN_WORKER_ERROR','error':self.last_error}),flush=True)
        threading.Thread(target=loop,name='pattern-research',daemon=True).start()

    def _config(self,settings,now_ms):
        config=self.store.meta('config')
        if config is None:
            config={'created_ms':now_ms,'symbols':list(DEFAULT_UNIVERSE_SYMBOLS),
                'capital':settings.starting_capital_usdt,'risk_pct':settings.risk_per_trade_pct,
                'cost':{'fee':settings.research_fee_bps/10000,'slip':settings.research_slippage_bps/10000,
                        'min_notional':settings.research_min_notional},
                'rules':{'entry_period':55,'exit_period':20,'atr_period':20,'atr_multiplier':2,
                         'adx_period':14,'adx_threshold':25},'spec':copy.deepcopy(SPEC)}
            self.store.put('config',config)
        return config

    def run(self,now_ms=None):
        if self.scan_busy() or not self.lock.acquire(blocking=False):return False
        self.busy=True;provided=now_ms is not None
        now_ms=int(time.time()*1000) if now_ms is None else now_ms
        day=now_ms//DAY_MS*DAY_MS
        try:
            if not self.get_settings().pattern_enabled:return False
            if self.store.meta('last_day')==day:return False
            if self.last_error and now_ms-self.last_error_ms<3600000:return False
            config=self._config(self.get_settings(),now_ms)
            data,errors=load_history(self.history_dir,config['symbols'],now_ms)
            self.store.put('data',{'updated_ms':now_ms,'available':len(data),'required':len(config['symbols']),'errors':errors})
            if errors:
                message='Waiting for complete daily history: '+', '.join(errors)
                old=self.store.meta('status',{})
                if old.get('message')!=message:self.store.event(now_ms,'WAITING_DATA',message)
                return False
            if config['spec']!=SPEC:
                self.store.event(now_ms,'PAUSED','Model specification changed. Existing experiment retained; an explicit new experiment is required.')
                return False
            self._mature(data,config,now_ms)
            recent=self.store.models(1,full=True)
            model=recent[0] if recent else None
            if model is None or now_ms>=self.store.meta('next_training_ms',0):
                self.store.event(now_ms,'TRAIN','Building past-only features and matured seven-day labels for all 19 coins.')
                rows=feature_rows(data,config['symbols'])
                labelled=labelled_rows(rows,data,config['cost'],day)
                self.store.event(now_ms,'VALIDATE','Training a shallow regression tree with three chronological, purged validation folds.')
                model,validation_predictions=train_and_validate(labelled,config)
                historical_forecasts=[{**r,'issued_ms':r['feature_ms']+1} for r in validation_predictions]
                historical_start=min(r['entry_ms'] for r in historical_forecasts)
                historical_pattern=paper_portfolio(data,historical_forecasts,config,historical_start,day)
                historical_donchian=simulate(data,config,historical_start,day)
                model['historical']={'pattern':historical_pattern['metrics'],
                    'donchian':historical_donchian['metrics'],
                    'label':'Retrospective paper replay using predictions from chronological validation folds. Not a fresh holdout.'}
                model.update(spec=copy.deepcopy(SPEC),features=FEATURES,
                    data_hash=hashlib.sha256(json.dumps({s:[asdict(b) for b in data[s]] for s in config['symbols']},sort_keys=True).encode()).hexdigest(),
                    verdict='DIAGNOSTIC_ONLY — forecasts and paper trading; never promoted to Testnet',
                    config=config)
                model['id']=self.store.add_model(now_ms,model);model['created_ms']=now_ms
                self.store.put('next_training_ms',now_ms+SPEC['retrain_days']*DAY_MS)
                self.store.event(now_ms,'TRAINED',f"Model #{model['id']} saved. Validation beats mean-return predictor: {model['validation']['beats_mean_baseline']}. Paper only.")
            else:
                rows=feature_rows(data,config['symbols'])
            issued_ms=now_ms if provided else int(time.time()*1000)
            # A slow run crossing midnight cannot register yesterday's forecasts late.
            if issued_ms//DAY_MS!=now_ms//DAY_MS:
                self.store.event(issued_ms,'WAITING_DATA','Day changed during training; wait for the new completed candles.')
                return False
            latest=[r for r in rows if r['feature_ms']==day]
            if len(latest)!=len(config['symbols']):raise ValueError('Latest feature day is incomplete')
            forecasts=[]
            for row in latest:
                leaf=predict(model['tree'],row['x'])
                forecasts.append({**row,'model_id':model['id'],'issued_ms':issued_ms,
                                  'prediction_pct':leaf['prediction_pct'],'leaf_id':leaf['id']})
            self.store.record_forecasts(forecasts)
            self._paper(data,config,day)
            self.store.put('last_day',day)
            self.store.event(issued_ms,'OBSERVING',f"Recorded {len(forecasts)} forecasts before tomorrow's open. Outcomes mature after seven holding days; no exchange orders.")
            self.last_error=None
            return True
        except Exception as exc:
            self.last_error=str(exc)[:500];self.last_error_ms=now_ms
            self.store.event(now_ms,'ERROR',self.last_error)
            return False
        finally:
            self.busy=False;self.lock.release()

    def _mature(self,data,config,now_ms):
        lookup={s:{b.open_time:b for b in bars} for s,bars in data.items()}
        for row in self.store.forecasts(pending_only=True,all_rows=True)['rows']:
            entry=lookup[row['symbol']].get(row['entry_ms']);exit_bar=lookup[row['symbol']].get(row['exit_ms'])
            if not entry or not exit_bar or exit_bar.close_time>=now_ms:continue
            actual=net_return(entry.open,exit_bar.open,config['cost'])
            self.store.outcome(row['id'],{'net_return_pct':actual,'prediction_error_pct':row['prediction_pct']-actual,
                'squared_error':(row['prediction_pct']-actual)**2,'entry_open':entry.open,'exit_open':exit_bar.open,
                'available_ms':exit_bar.close_time+1})

    def _paper(self,data,config,end_ms):
        forecasts=self.store.forecasts(all_rows=True)['rows']
        if not forecasts:return
        start=min(r['entry_ms'] for r in forecasts)
        result=paper_portfolio(data,forecasts,config,start,end_ms)
        if result is None:
            self.store.put('paper',{'start_ms':start,'notice':'Waiting for completed candles after the first planned entry.'})
            return
        baseline=simulate(data,config,start,end_ms)
        # Daily display: full ledger kept reproducibly by immutable forecasts.
        self.store.put('paper',{'start_ms':start,'end_ms':end_ms,'pattern':result['metrics'],
            'donchian':baseline['metrics'],'trades':result['trades'][-100:],'positions':result['positions'],
            'skips':result['skips'][-30:],'equity':result['equity'][-730:],
            'label':'Fresh forward paper portfolios, same dates/capital/costs. Different entry and exit rules.',
            'notice':'Marks use completed daily candles. No lot/depth/spread model. Pattern: max three positions, seven-day exit; Donchian: 55/20 + ADX25.'})

    def snapshot(self):
        try:
            return {**self.store.snapshot(),'enabled':self.get_settings().pattern_enabled,
                    'busy':self.busy,'spec':SPEC,'features':FEATURES,'error':self.last_error}
        except Exception as exc:
            return {'enabled':False,'busy':False,'status':{'stage':'ERROR','message':str(exc)[:500]}}
