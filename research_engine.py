"""Scheduled bounded research; no arbitrary code execution and no exchange client.

Promotion is delegated to the existing engine under its order/state lock. Candidate
changes are limited to one entry parameter. Risk, exits, universe, credentials and
capital are never editable by the proposer.
"""
import copy
import hashlib
import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

from daily_history import validate
from research_backtest import simulate, paired_lower_bound
from strategy import Candle, DAY_MS

STAGES = ['analyze', 'propose', 'test', 'compare', 'decide']
TERMINAL = {'ADOPTED', 'REJECTED', 'ERROR'}


def config_for(settings):
    active = next(u for u in settings.universes if u['name'].lower()==settings.active_universe.lower())
    return {'rules': {k:getattr(settings,k) for k in ('entry_period','exit_period','atr_period',
                      'atr_multiplier','adx_period','adx_threshold')},
            'risk_pct':settings.risk_per_trade_pct, 'capital':settings.starting_capital_usdt,
            'symbols':list(active['symbols']),
            'cost':{'fee':settings.research_fee_bps/10000,'slip':settings.research_slippage_bps/10000,
                    'min_notional':settings.research_min_notional}}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def validation_policy(settings):
    return {'forward_days':settings.research_forward_days, 'min_trades':settings.research_min_trades,
            'min_symbols':settings.research_min_symbols,
            'looks':[settings.research_forward_days*k for k in (1,2,4)],
            'family_alpha':0.05,'bootstrap_samples':5000,'block_days':7}


def load_history(directory, symbols, now_ms):
    data={}; errors={}; target=(now_ms//DAY_MS)*DAY_MS-1
    for s in symbols:
        try:
            if not s.isalnum():
                raise ValueError('Invalid symbol')
            raw=json.loads((Path(directory)/(s+'.json')).read_text())
            if raw.get('version')!=1 or raw.get('symbol')!=s:
                raise ValueError('Invalid cache identity')
            bars=[Candle(**b) for b in raw['candles'] if b['close_time']<=target]
            validate(bars)
            if not bars or bars[-1].close_time!=target:
                raise ValueError('Latest completed daily candle is missing')
            data[s]=bars
        except (OSError,ValueError,KeyError,TypeError) as exc:
            errors[s]=str(exc)
    return data,errors


def historical_compare(data, base, candidate, end_ms):
    warm=max(500, base['rules']['entry_period']+1, candidate['rules']['entry_period']+1)
    common=max(data[s][0].open_time+warm*DAY_MS for s in base['symbols'])
    start=max(common,end_ms-3*365*DAY_MS)
    if end_ms-start < 365*DAY_MS:
        raise ValueError('Need at least one year of shared 19-coin history after indicator warmup')
    a,b=simulate(data,base,start,end_ms),simulate(data,candidate,start,end_ms)
    mid=start+((end_ms-start)//DAY_MS//2)*DAY_MS
    halves=[]
    for lo,hi in ((start,mid),(mid,end_ms)):
        x,y=simulate(data,base,lo,hi),simulate(data,candidate,lo,hi)
        halves.append({'active':x['metrics'],'candidate':y['metrics']})
    passed=(b['metrics']['return_pct']>a['metrics']['return_pct'] and
            b['metrics']['max_drawdown_pct']<=a['metrics']['max_drawdown_pct'] and
            all(h['candidate']['return_pct']>=h['active']['return_pct'] for h in halves))
    return {'label':'Previously available history — diagnostic, not an independent holdout',
            'active':a['metrics'],'candidate':b['metrics'],'halves':halves,'passed':passed}


def choices(base):
    rules=base['rules']; result=[]
    for key,delta,low,high in [('adx_threshold',-5,15,40),('adx_threshold',5,15,40),
                               ('entry_period',-5,40,80),('entry_period',5,40,80)]:
        value=rules[key]+delta
        if low<=value<=high:
            candidate=copy.deepcopy(base);candidate['rules'][key]=value
            result.append({'key':key,'before':rules[key],'after':value,'config':candidate})
    return result


def select_proposal(options, evidence):
    """Optional Claude interpretation; otherwise an explicitly named rule-based proposer.

    Only summaries (no credentials, account identifiers or raw account state) are
    sent to the configured model. The output selects an already-validated option.
    """
    key=os.getenv('ANTHROPIC_API_KEY');model=os.getenv('RESEARCH_ANTHROPIC_MODEL')
    if not key or not model:
        choice=options[0]
        # Descriptive evidence only ranks the next test; it cannot authorize a trade
        # or adoption. Include losses and wait for enough matured observations.
        groups={g['stage']:g for g in evidence.get('groups',[])}
        for stage,param in [('ADX','adx_threshold'),('Entry','entry_period')]:
            group=groups.get(stage,{})
            avg=group.get('avg_7d_net_pct')
            if group.get('matured_7d',0)>=20 and avg is not None:
                direction=-1 if avg>0 else 1
                matching=[o for o in options if o['key']==param and (o['after']-o['before'])*direction>0]
                if matching:
                    choice=matching[0]
                    break
        reason = ('Test whether the entry gate excludes useful breakouts' if choice['after']<choice['before']
                  else 'Test whether a stricter entry gate reduces weak breakouts')
        return choice,reason,'Rule-based bounded proposer (no LLM configured)'
    import requests
    public_options=[{k:o[k] for k in ('key','before','after')} for o in options]
    response=requests.post('https://api.anthropic.com/v1/messages',timeout=45,
        headers={'x-api-key':key,'anthropic-version':'2023-06-01','Content-Type':'application/json'},
        json={'model':model,'max_tokens':400,'system':
            'Choose one listed research hypothesis for a demo trading experiment. Return JSON only '
            'with option_index (integer) and hypothesis (string). Never claim future profitability. '
            'No new rule, risk change, code, or order is permitted.',
            'messages':[{'role':'user','content':json.dumps({'options':public_options,'evidence':evidence})}]})
    if not response.ok:
        raise RuntimeError(f'AI proposal service returned HTTP {response.status_code}')
    content=''.join(c.get('text','') for c in response.json().get('content',[]))
    answer=json.loads(content)
    index=answer.get('option_index');hypothesis=answer.get('hypothesis')
    if type(index) is not int or not 0<=index<len(options) or not isinstance(hypothesis,str) or not hypothesis.strip():
        raise ValueError('AI proposal was outside the allowed candidate schema')
    return options[index],hypothesis[:2000],f'Claude / {model}'


class ResearchEngine:
    def __init__(self, store, history_dir, settings_store, promote):
        self.store,self.history_dir,self.settings_store,self.promote=store,Path(history_dir),settings_store,promote
        self.lock=threading.Lock();self.wakeup=threading.Event();self.busy=False

    def start(self, stop_event):
        def loop():
            while not stop_event.is_set():
                self.wakeup.wait(60);self.wakeup.clear()
                if stop_event.is_set(): break
                try:
                    self.run()
                except Exception as exc:
                    # A storage outage can also prevent run()'s error record.
                    # Keep the optional worker alive for recovery on the next tick.
                    print(json.dumps({'event':'RESEARCH_WORKER_ERROR','error':str(exc)[:500]}),flush=True)
        threading.Thread(target=loop,name='donchian-research',daemon=True).start()

    def run(self, now_ms=None, force=False):
        if not self.lock.acquire(blocking=False): return False
        self.busy=True
        try:
            now_ms=now_ms or int(time.time()*1000)
            settings=self.settings_store.get()
            if not settings.research_enabled: return False
            base=config_for(settings)
            reference=self.store.meta('reference')
            if reference is None:
                # Start from a NEW full day; never call existing observations unseen.
                reference={'config':copy.deepcopy(base),'start_ms':(now_ms//DAY_MS+1)*DAY_MS,
                           'created_ms':now_ms,'version':settings.strategy_version}
                self.store.put_meta('reference',reference)
            symbols=list(dict.fromkeys(base['symbols']+reference['config']['symbols']))
            data,errors=load_history(self.history_dir,symbols,now_ms)
            self.store.put_meta('data_status',{'updated_ms':now_ms,'available':len(data),'required':len(symbols),'errors':errors})
            if errors:
                self.store.put_meta('notice','Waiting for complete daily history: '+', '.join(errors))
                return False
            # Update outcomes even while research is waiting for its next weekly slot.
            outcome_day=(now_ms//DAY_MS)*DAY_MS
            if self.store.meta('outcome_day')!=outcome_day:
                self.store.update_outcomes(data,now_ms)
                self.store.put_meta('outcome_day',outcome_day)
            end_ms=(now_ms//DAY_MS)*DAY_MS
            last_day=self.store.meta('reference_last_day')
            if last_day!=end_ms and end_ms>reference['start_ms']:
                ref=simulate(data,reference['config'],reference['start_ms'],end_ms)
                self.store.put_meta('reference_result',ref['metrics'])
                self.store.put_meta('reference_last_day',end_ms)
            pending=self.store.pending()
            if pending:
                if now_ms-self.store.meta('last_review_ms',0)>=settings.research_review_days*DAY_MS:
                    self.store.put_meta('last_review_ms',now_ms)
                    pending['payload']['latest_analysis']=self.store.analysis_summary()
                    self.store.stage(pending,'decide','Weekly monitoring review recorded; validation dates remain fixed',pending['status'])
                self._forward(pending,data,settings,now_ms)
                return True
            last=self.store.meta('last_review_ms',0)
            if not force and now_ms-last<settings.research_review_days*DAY_MS: return False
            self.store.put_meta('last_review_ms',now_ms)
            self._new_review(data,base,settings,now_ms)
            return True
        except Exception as exc:
            self.store.put_meta('notice','Research error: '+str(exc)[:500])
            self.store.put_meta('last_error',{'time_ms':now_ms,'error':str(exc)[:500]})
            # A persisted unfinished cycle is recovered rather than silently lost.
            pending=self.store.pending()
            if pending and pending['status']=='RUNNING':
                self.store.stage(pending,pending['stage'],'Interrupted research run: '+str(exc)[:500],'ERROR')
            return False
        finally:
            self.busy=False;self.lock.release()

    def _new_review(self,data,base,settings,now_ms):
        end_ms=(now_ms//DAY_MS)*DAY_MS
        evidence=self.store.analysis_summary()
        cycle=self.store.new_cycle({'base':base,'base_version':settings.strategy_version,
            'policy':validation_policy(settings),'analysis':evidence,
            'data_hash':fingerprint({s:[asdict(b) for b in data[s]] for s in base['symbols']}),
            'history_end_ms':end_ms})
        self.store.stage(cycle,'analyze','Recorded decisions and validated closed daily history inspected')
        tried=self.store.meta('attempted_configs',{})
        if isinstance(tried,list): tried={key:now_ms for key in tried}
        # Previously rejected rules may become worth retesting in new regimes.
        # Require 90 new days before repeating a configuration, with a fresh trial
        # number and a new prospective period. Never reuse a previous holdout.
        options=[o for o in choices(base) if now_ms-tried.get(fingerprint(o['config']),-90*DAY_MS)>=90*DAY_MS]
        if not options:
            self.store.stage(cycle,'decide','No eligible change: wait for 90 new days before retesting a previously examined configuration. Keep active strategy.','REJECTED')
            return
        option,reason,source=select_proposal(options,evidence)
        trial=self.store.meta('trial_count',0)+1
        tried[fingerprint(option['config'])]=now_ms
        self.store.put_meta('attempted_configs',tried);self.store.put_meta('trial_count',trial)
        cycle['payload'].update(candidate=option['config'],change={k:option[k] for k in ('key','before','after')},
                                hypothesis=reason,proposer=source,trial=trial)
        self.store.stage(cycle,'propose',reason)
        self.store.stage(cycle,'test','Running paired, cash-constrained historical simulations including fees and slippage')
        comparison=historical_compare(data,base,option['config'],end_ms)
        cycle['payload']['historical']=comparison
        self.store.stage(cycle,'compare','Historical diagnostics complete; known history cannot authorize adoption')
        if not comparison['passed']:
            self.store.stage(cycle,'decide','Rejected: historical return/drawdown or two-window stability did not improve','REJECTED')
            return
        start=(now_ms//DAY_MS+1)*DAY_MS
        cycle['payload'].update(forward_start_ms=start,look_index=0,
                                candidate_version=f'research_{cycle["id"]}')
        self.store.stage(cycle,'decide','Keep observing: candidate registered before its fresh forward period starts','OBSERVING',
                         forward_start_ms=start)
        self.store.put_meta('notice','Candidate is observing fresh data; the active demo strategy continues trading.')

    def _forward(self,cycle,data,settings,now_ms):
        p=cycle['payload'];base=config_for(settings)
        if settings.strategy_version==p.get('candidate_version'):
            self.store.stage(cycle,'decide','Promotion recovered after restart; version already active','ADOPTED')
            return
        if base!=p['base'] or settings.strategy_version!=p['base_version']:
            self.store.stage(cycle,'decide','Rejected: strategy, capital, costs or universe changed during the experiment','REJECTED')
            return
        if cycle['status']=='RUNNING':
            self.store.stage(cycle,'decide','Incomplete run recovered after restart; no settings were promoted','ERROR')
            return
        start=p['forward_start_ms'];end=(now_ms//DAY_MS)*DAY_MS
        policy=p['policy'];look=policy['looks'][p['look_index']];target=start+look*DAY_MS
        # No daily significance peeking: analyze only at predeclared dates. Show progress.
        progress=max(0,(end-start)//DAY_MS)
        if p.get('observed_days')!=progress:
            p['observed_days']=progress;p['next_evaluation_ms']=target
            self.store.save_cycle(cycle)
        if end<target: return
        if cycle['status']=='READY':
            self._promote(cycle,settings)
            return
        self.store.stage(cycle,'test','Evaluating the predeclared forward checkpoint', 'OBSERVING')
        a=simulate(data,p['base'],start,target);b=simulate(data,p['candidate'],start,target)
        # Sum 1/(n(n+1)) over all trials <=1; reserve equal alpha for three looks.
        alpha=policy['family_alpha']/(p['trial']*(p['trial']+1)*len(policy['looks']))
        lower=paired_lower_bound(a['returns'],b['returns'],alpha,cycle['id'],
                                 policy['bootstrap_samples'],policy['block_days'])
        am,bm=a['metrics'],b['metrics']
        gates={
            'sample':min(am['closed_trades'],bm['closed_trades'])>=policy['min_trades'],
            'breadth':min(am['symbols_traded'],bm['symbols_traded'])>=policy['min_symbols'],
            'net_return':bm['return_pct']>max(0,am['return_pct']),
            'drawdown':bm['max_drawdown_pct']<=am['max_drawdown_pct'],
            'paired_return_lower_bound':lower>0,
        }
        p['forward']={'active':am,'candidate':bm,'gates':gates,'paired_lower_bound':lower,'alpha':alpha,
            'label':'Fresh forward paper comparison; same capital, dates, universe and cost assumptions',
            'limitations':'Daily bars; no spread/lot-size/depth model. Bootstrap screen is not proof of edge.'}
        p.setdefault('forward_checkpoints',[]).append(copy.deepcopy(p['forward']))
        self.store.stage(cycle,'compare','Forward comparison complete; '+', '.join(k+(': pass' if v else ': fail') for k,v in gates.items()),'OBSERVING')
        if all(gates.values()):
            self.store.stage(cycle,'decide','Candidate passed all recorded forward gates','READY')
            self._promote(cycle,settings)
        elif not gates['sample'] or not gates['breadth']:
            if p['look_index']+1<len(policy['looks']):
                p['look_index']+=1
                self.store.stage(cycle,'decide','Keep observing: insufficient closed trades or coin coverage; next checkpoint was declared at proposal time','OBSERVING')
            else:
                self.store.stage(cycle,'decide','Rejected as inconclusive: insufficient sample at final checkpoint','REJECTED')
        else:
            self.store.stage(cycle,'decide','Rejected: forward performance did not meet the recorded comparison gates','REJECTED')

    def _promote(self,cycle,settings):
        if not settings.research_auto_adopt:
            self.store.put_meta('notice','Candidate passed; automatic adoption is paused in settings.')
            return
        result = self.promote(cycle)
        if result is None:
            self.store.put_meta("notice", "Candidate ready; waiting for the daily scan to finish before adoption.")
            return
        if result:
            self.store.stage(cycle,'decide','Adopted for future demo entries; existing positions retain their exit rules','ADOPTED')
            self.store.put_meta('notice','New demo strategy adopted: '+cycle['payload']['candidate_version'])
        else:
            self.store.stage(cycle,'decide','Rejected: active settings changed before promotion','REJECTED')

    def snapshot(self):
        settings=self.settings_store.get();last=self.store.meta('last_review_ms',0)
        return {'enabled':settings.research_enabled,'auto_adopt':settings.research_auto_adopt,'busy':self.busy,
            'stages':STAGES,'active_version':settings.strategy_version,'review_days':settings.research_review_days,
            'next_review_ms':last+settings.research_review_days*DAY_MS if last else None,
            'notice':self.store.meta('notice','Waiting for the first completed daily scan and full candle history.'),
            'data':self.store.meta('data_status'), 'trial_count':self.store.meta('trial_count',0),
            'cycles':self.store.cycles(), 'decisions':self.store.decisions(),
            'reference':self.store.meta('reference'), 'reference_result':self.store.meta('reference_result'),
            'policy':validation_policy(settings),
            'proposer':'Claude (configured)' if os.getenv('ANTHROPIC_API_KEY') and os.getenv('RESEARCH_ANTHROPIC_MODEL') else 'Rule-based bounded proposer (no LLM configured)'}
