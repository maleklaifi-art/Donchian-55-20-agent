"""Versioned finite strategy grammar and cash-constrained multi-timeframe replay."""
import hashlib
import json
import math
from collections import deque
from statistics import mean

from strategy import DAY_MS,wilders_adx_series
from explorer_data import FRAMES,resample
from pattern_model import fit_tree,predict

SPEC={'version':'explorer_v1','frames':['1h','4h','1d'],'history_days':1095,
      'weekly_budget':12,'forward_slots':3,'fold_days':90,'folds':3,
      'forward_looks_days':[90,180,360],'min_closed_trades':20,'min_coins':5,
      'bootstrap_samples':500000,'family_alpha':.05,'max_candidates':363}
FAMILIES=['breakout','ema_trend','momentum','rsi_reversion','bollinger_reversion',
          'trend_pullback','volatility_breakout','breakout_and_momentum',
          'bollinger_and_rsi','ema_and_momentum']
FILTERS=['none','adx25','volume','higher_trend','adx_volume','higher_volume']
ML_FEATURES=['return_1bar_pct','return_7bar_pct','return_28bar_pct','volatility_20bar_pct',
             'range_pct','close_in_range','volume_ratio_20bar','distance_high_20bar_pct',
             'distance_mean_50bar_pct','btc_return_7bar_pct','btc_return_28bar_pct']


def catalogue():
    items=[]
    # Breadth-first order is fixed before results: timeframes, then families and combinations.
    for variant in range(2):
        for filter_name in FILTERS:
            for family in FAMILIES:
                for frame in SPEC['frames']:
                    key=f'{frame}:{family}:v{variant+1}:{filter_name}'
                    items.append({'id':key,'frame':frame,'family':family,'variant':variant,
                                  'filter':filter_name,'label':f'{family.replace("_"," ")} · variant {variant+1} · {filter_name.replace("_"," ")}'})
    # Train on each timeframe early, with identical predetermined complexity.
    for i,frame in enumerate(SPEC['frames']):
        items.insert(i,{'id':f'{frame}:learned_tree:v1:none','frame':frame,'family':'learned_tree',
                       'variant':0,'filter':'none','label':'Learned regression tree · seven-bar target'})
    by_id={c['id']:c for c in items}
    parameters={'breakout':('entry_bars',20,55),'ema_trend':('fast_ema',20,10),
        'momentum':('lookback_bars',20,50),'rsi_reversion':('entry_rsi',30,25),
        'bollinger_reversion':('band_width',2,2.5),'trend_pullback':('entry_rsi',45,35),
        'volatility_breakout':('compression_ratio',.75,.9),
        'breakout_and_momentum':('entry_bars',20,55),'bollinger_and_rsi':('band_width',2,2.5),
        'ema_and_momentum':('fast_ema',20,10)}
    combinations={'breakout_and_momentum':('breakout','momentum > 0'),
        'bollinger_and_rsi':('bollinger_reversion','RSI14 < 30'),
        'ema_and_momentum':('ema_trend','momentum > 0'),
        'volatility_breakout':('breakout','ATR20 / mean TR100 < 0.75')}
    for c in items:
        f=c['filter'];v=c['variant'];family=c['family'];parent=None;change=None
        if f!='none':
            base={'adx_volume':'adx25','higher_volume':'volume'}.get(f,'none')
            parent=f"{c['frame']}:{family}:v{v+1}:{base}"
            added={'adx25':'ADX14 > 25','volume':'volume > 1.5 × prior 20-bar average',
                'higher_trend':'completed higher-timeframe close > EMA50',
                'adx_volume':'volume > 1.5 × prior 20-bar average',
                'higher_volume':'completed higher-timeframe close > EMA50'}[f]
            change={'factor':'entry condition','before':'absent','after':added}
        elif v:
            parent=f"{c['frame']}:{family}:v1:none"
            key,before,after=parameters[family];change={'factor':key,'before':before,'after':after}
        elif family in combinations:
            base,added=combinations[family];parent=f"{c['frame']}:{base}:v1:none"
            change={'factor':'entry condition','before':'absent','after':added}
        c.update(parent_id=parent,kind='CONTROLLED_CHANGE' if parent else 'DISCOVERY',change=change)
        c['hypothesis']=(f"Changing only {change['factor']} from {change['before']} to {change['after']} improves net performance without increasing drawdown versus the unchanged parent."
            if parent else f"The predeclared {family.replace('_',' ')} on {c['frame']} can beat daily Donchian after costs on new data.")
    # Resolve parent references only after annotation; no nested mutable ancestry.
    for c in items:
        if c['parent_id'] not in by_id and c['parent_id'] is not None:raise ValueError('Missing experiment parent')
    return items


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def sma(a,n):
    import numpy as np
    out=np.full(len(a),np.nan);s=np.cumsum(np.insert(a,0,0.0))
    out[n-1:]=(s[n:]-s[:-n])/n
    return out


def ema(a,n):
    import numpy as np
    out=np.full(len(a),np.nan)
    if len(a)<n:return out
    out[n-1]=float(np.mean(a[:n]));alpha=2/(n+1)
    for i in range(n,len(a)):out[i]=alpha*a[i]+(1-alpha)*out[i-1]
    return out


def extremes(a,n,high=True):
    import numpy as np
    out=np.full(len(a),np.nan);q=deque()
    for i,value in enumerate(a):
        while q and q[0]<i-n:q.popleft()
        if i>=n:out[i]=a[q[0]]
        while q and (a[q[-1]]<=value if high else a[q[-1]]>=value):q.pop()
        q.append(i)
    return out


def prepare(hourly,frame,end_ms):
    import numpy as np
    interval=FRAMES[frame];higher={'1h':4*3600000,'4h':DAY_MS,'1d':7*DAY_MS}[frame]
    data={s:resample(bars,interval,end_ms) for s,bars in hourly.items()}
    out={}
    for s,bars in data.items():
        c=np.array([b.close for b in bars]);o=np.array([b.open for b in bars]);h=np.array([b.high for b in bars]);l=np.array([b.low for b in bars]);v=np.array([b.volume for b in bars])
        n=len(c)
        if n<250:raise ValueError('Insufficient indicator warmup')
        times=np.array([b.open_time for b in bars],dtype='int64')
        tr=np.maximum(h-l,np.maximum(np.abs(h-np.r_[c[0],c[:-1]]),np.abs(l-np.r_[c[0],c[:-1]])))
        atr=np.full(n,np.nan);atr[19]=np.mean(tr[:20])
        for i in range(20,n):atr[i]=(atr[i-1]*19+tr[i])/20
        delta=np.diff(c,prepend=c[0]);gain=np.maximum(delta,0);loss=np.maximum(-delta,0)
        rsi=np.full(n,np.nan);g=np.mean(gain[1:15]);z=np.mean(loss[1:15])
        for i in range(14,n):
            if i>14:g=(g*13+gain[i])/14;z=(z*13+loss[i])/14
            rsi[i]=100*g/(g+z) if g+z else 50
        av=sma(v,20);av=np.r_[np.nan,av[:-1]]
        volume=np.divide(v,av,out=np.zeros(n),where=av>0)
        middle=sma(c,20);std=np.sqrt(np.maximum(0,sma(c*c,20)-middle*middle))
        hi=resample(hourly[s],higher,end_ms);hc=np.array([b.close for b in hi]);he=ema(hc,50)
        close_times=np.array([b.close_time for b in hi],dtype='int64')
        ix=np.searchsorted(close_times,times+interval-1,side='right')-1
        higher_ok=np.zeros(n,dtype=bool)
        valid=ix>=0;higher_ok[valid]=hc[ix[valid]]>he[ix[valid]]
        logret=np.diff(np.log(c),prepend=np.log(c[0]));vol=np.sqrt(np.maximum(0,sma(logret*logret,20)-sma(logret,20)**2))*100
        def roc(period):
            x=np.full(n,np.nan);x[period:]=100*(c[period:]/c[:-period]-1);return x
        high20=extremes(h,20)
        out[s]={'bars':bars,'time':times,'open':o,'close':c,'high':h,'low':l,'atr':atr,'rsi':rsi,
            'adx':np.array([float('nan') if x is None else x for x in wilders_adx_series(bars)]),
            'volume':volume,'middle':middle,'std':std,'ema20':ema(c,20),'ema50':ema(c,50),'ema200':ema(c,200),
            'high20':high20,'high55':extremes(h,55),'low10':extremes(l,10,False),'low20':extremes(l,20,False),
            'roc20':roc(20),'roc50':roc(50),'higher':higher_ok,'atr100':sma(tr,100)}
        with np.errstate(divide='ignore',invalid='ignore'):
            out[s]['x']=np.column_stack([roc(1),roc(7),roc(28),vol,100*(h-l)/c,
                np.divide(c-l,h-l,out=np.full(n,.5),where=h>l),volume,100*(c/high20-1),100*(c/sma(c,50)-1),roc(7)*0,roc(28)*0])
    btc=out['BTCUSDT'];btc_index={int(t):i for i,t in enumerate(btc['time'])}
    for s,p in out.items():
        for i,t in enumerate(p['time']):
            j=btc_index.get(int(t))
            p['x'][i,9:]=btc['x'][j,1:3] if j is not None else [np.nan,np.nan]
    return out


def model_rows(prepared,frame,config,cutoff_ms):
    """One deterministic rotating-hour sample per UTC date/coin, to bound memory.

    Target: next-bar open to the open seven bars later, including costs. Every
    label must be observable before the training cutoff for ALL coins.
    """
    import numpy as np
    interval=FRAMES[frame];per_day=DAY_MS//interval;rows=[];cost=config['cost']
    for symbol,p in prepared.items():
        for i in range(250,len(p['time'])-8):
            t=int(p['time'][i]);available=int(p['time'][i+8])+interval
            if available>=cutoff_ms or t<cutoff_ms-1095*DAY_MS:continue
            # Each successive date rotates the sampled intraday hour.
            if (t//interval)%per_day!=(t//DAY_MS)%per_day:continue
            if not np.isfinite(p['x'][i]).all():continue
            y=100*(p['open'][i+8]*(1-cost['slip'])*(1-cost['fee'])/(p['open'][i+1]*(1+cost['slip'])*(1+cost['fee']))-1)
            rows.append({'symbol':symbol,'feature_ms':t+interval,'label_available_ms':available,
                         'x':p['x'][i].tolist(),'y':float(y)})
    if len({r['feature_ms']//DAY_MS for r in rows})<365:raise ValueError('Need a full prior year of matured model labels')
    return rows


def signals(prepared,candidate,tree=None):
    import numpy as np
    result={};v=candidate['variant'];family=candidate['family'];f=candidate['filter']
    for symbol,p in prepared.items():
        c=p['close'];n=len(c);a=p['ema20'] if v==0 else ema(c,10);b=p['ema50']
        entry55=v and family in ('breakout','breakout_and_momentum')
        breakout=c>(p['high55'] if entry55 else p['high20']);breakexit=c<p['low10']
        trend=a>b;cross=trend & ~np.r_[False,trend[:-1]]
        momentum=p['roc50' if v and family=='momentum' else 'roc20']>0
        lower=p['middle']-(2 if v==0 else 2.5)*p['std']
        if family in ('breakout','volatility_breakout','breakout_and_momentum'):
            buy=breakout.copy();sell=breakexit
            if family=='volatility_breakout':buy &= p['atr']/p['atr100']<(.75 if v==0 else .9)
            if family=='breakout_and_momentum':buy &= momentum
        elif family in ('ema_trend','ema_and_momentum'):
            buy=cross.copy();sell=~trend
            if family=='ema_and_momentum':buy &= momentum
        elif family=='momentum':buy=momentum.copy();sell=~momentum
        elif family=='rsi_reversion':buy=p['rsi']<(30 if v==0 else 25);sell=p['rsi']>50
        elif family in ('bollinger_reversion','bollinger_and_rsi'):
            buy=c<lower;sell=c>p['middle']
            if family=='bollinger_and_rsi':buy &= p['rsi']<30
        elif family=='trend_pullback':buy=(c>p['ema50']) & (p['rsi']<(45 if v==0 else 35));sell=(c<p['ema50']) | (p['rsi']>65)
        elif family=='learned_tree':
            if tree is None:raise ValueError('Missing frozen learned model')
            buy=np.zeros(n,dtype=bool);sell=np.zeros(n,dtype=bool)
            for i,x in enumerate(p['x']):
                if np.isfinite(x).all():buy[i]=predict(tree,x)['prediction_pct']>1
        else:raise ValueError('Unknown strategy family')
        if f in ('adx25','adx_volume'):buy &= p['adx']>25
        if f in ('volume','adx_volume','higher_volume'):buy &= p['volume']>1.5
        if f in ('higher_trend','higher_volume'):buy &= p['higher']
        buy[:250]=False;sell[:250]=False
        result[symbol]={'buy':buy,'sell':sell,'hold_bars':7 if family=='learned_tree' else None}
    return result


def simulate(prepared,signals_by_symbol,frame,config,start,end):
    interval=FRAMES[frame];symbols=config['symbols'];cost=config['cost'];cash=float(config['capital'])
    indices={s:{int(t):i for i,t in enumerate(prepared[s]['time'])} for s in symbols}
    count=(end-start)//interval
    if count<=0 or start%DAY_MS or end%DAY_MS:raise ValueError('Comparison must cover full UTC days')
    for s in symbols:
        if any(t not in indices[s] for t in range(start,end,interval)):raise ValueError(f'{s}: missing comparison bars')
    positions={};buys=[];sells=[];trades=[];daily=[];peak=cash;dd=0;prev=cash
    for t in range(start,end,interval):
        for s in sells:
            if s not in positions:continue
            p=positions.pop(s);i=indices[s][t]
            proceeds=p['qty']*prepared[s]['open'][i]*(1-cost['slip'])*(1-cost['fee']);cash+=proceeds
            pnl=proceeds-p['cost'];trades.append({'symbol':s,'entry_ms':p['time'],'exit_ms':t,'pnl':float(pnl),'r':float(pnl/p['risk']) if p['risk'] else 0})
        for s,atr in buys:
            if s in positions or not math.isfinite(atr) or atr<=0:continue
            marked=cash+sum(p['qty']*prepared[k]['open'][indices[k][t]] for k,p in positions.items())
            fill=prepared[s]['open'][indices[s][t]]*(1+cost['slip']);qty=min(marked*config['risk_pct']/100/(2*atr),cash/(fill*(1+cost['fee'])))
            if qty*fill<cost['min_notional'] or qty<=0:continue
            spent=qty*fill*(1+cost['fee']);cash=max(0,cash-spent)
            positions[s]={'qty':qty,'cost':spent,'risk':qty*2*atr,'time':t}
        buys=[];sells=[]
        for s in symbols:
            i=indices[s][t];sig=signals_by_symbol[s]
            if s in positions:
                due=sig['hold_bars'] and (t-positions[s]['time'])//interval+1>=sig['hold_bars']
                if sig['sell'][i] or due:sells.append(s)
            elif sig['buy'][i]:buys.append((s,prepared[s]['atr'][i]))
        value=float(cash+sum(p['qty']*prepared[s]['close'][indices[s][t]] for s,p in positions.items()))
        peak=max(peak,value);dd=max(dd,100*(1-value/peak))
        if (t+interval)%DAY_MS==0:daily.append(value/prev-1);prev=value
    wins=[r for r in trades if r['pnl']>0];losses=[r for r in trades if r['pnl']<0];loss=-sum(r['pnl'] for r in losses)
    metrics={'start_ms':start,'end_ms':end,'days':len(daily),'return_pct':100*(value/config['capital']-1),
        'ending_equity':value,'net_profit':value-config['capital'],'max_drawdown_pct':dd,'closed_trades':len(trades),
        'open_positions':len(positions),'symbols_traded':len({r['symbol'] for r in trades}),
        'profit_factor':sum(r['pnl'] for r in wins)/loss if loss else None,
        'win_rate':100*len(wins)/len(trades) if trades else None,'avg_win_r':mean(r['r'] for r in wins) if wins else None,
        'avg_loss_r':mean(r['r'] for r in losses) if losses else None,'fee_bps':cost['fee']*10000,'slippage_bps':cost['slip']*10000}
    return {'metrics':metrics,'returns':daily,'trades':trades}
