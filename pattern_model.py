"""Small, inspectable regression tree for daily market-pattern research.

All thresholds are learned on training rows only. No exchange or app imports.
Models are JSON, not pickle or executable model output.
"""
import math
from statistics import mean, pstdev

from daily_history import validate
from strategy import DAY_MS, wilders_atr

FEATURES = ['return_1d_pct', 'return_7d_pct', 'return_28d_pct', 'volatility_20d_pct',
            'range_pct', 'close_in_range', 'volume_ratio_20d', 'distance_high_20d_pct',
            'distance_mean_50d_pct', 'btc_return_7d_pct', 'btc_return_28d_pct']
SPEC = {'version': 'patterns_tree_v1', 'horizon_days': 7, 'entry_delay_days': 2,
        'max_depth': 3, 'min_leaf': 200, 'min_leaf_days': 30,
        'quantiles': [0.2, 0.4, 0.6, 0.8], 'min_history_days': 730,
        'fold_days': 90, 'folds': 3, 'training_days': 1095,
        'retrain_days': 28, 'max_positions': 3, 'entry_threshold_pct': 1.0}


def net_return(entry, exit_price, cost):
    return (exit_price * (1-cost['slip']) * (1-cost['fee']) /
            (entry * (1+cost['slip']) * (1+cost['fee'])) - 1) * 100


def feature_rows(data, symbols):
    """Features for every complete eligible day, not only selected trade signals."""
    for symbol in symbols:
        validate(data[symbol])
    btc = {b.open_time: i for i, b in enumerate(data['BTCUSDT'])}
    result = []
    for symbol in symbols:
        bars = data[symbol]
        # Continuous ATR is for the fixed paper sizing rule, not a learned feature.
        atr = None
        for i, b in enumerate(bars):
            if i == 19:
                atr = wilders_atr(bars[:20], 20)
            elif i > 19:
                tr = max(b.high-b.low, abs(b.high-bars[i-1].close), abs(b.low-bars[i-1].close))
                atr = (atr*19+tr)/20
            if i < 60:
                continue
            j = btc.get(b.open_time)
            if j is None or j < 28:
                continue
            history = bars[i-20:i]
            vol = mean(x.volume for x in history)
            returns = [math.log(bars[k].close/bars[k-1].close) for k in range(i-19, i+1)]
            bc = data['BTCUSDT']
            x = [100*(b.close/bars[i-d].close-1) for d in (1, 7, 28)] + [
                100*pstdev(returns), 100*(b.high-b.low)/b.close,
                (b.close-b.low)/(b.high-b.low) if b.high>b.low else 0.5,
                b.volume/vol if vol>0 else 0.0,
                100*(b.close/max(v.high for v in history)-1),
                100*(b.close/mean(v.close for v in bars[i-49:i+1])-1),
                100*(bc[j].close/bc[j-7].close-1), 100*(bc[j].close/bc[j-28].close-1)]
            if not all(math.isfinite(v) for v in x):
                continue
            result.append({'symbol': symbol, 'feature_ms': b.close_time+1, 'x': x,
                           'atr': atr, 'entry_ms': b.open_time+2*DAY_MS,
                           'exit_ms': b.open_time+9*DAY_MS})
    # Keep dates common to the entire declared universe; never silently substitute.
    counts = {}
    for row in result:
        counts[row['feature_ms']] = counts.get(row['feature_ms'], 0)+1
    return sorted((r for r in result if counts[r['feature_ms']]==len(symbols)),
                  key=lambda r: (r['feature_ms'], symbols.index(r['symbol'])))


def labelled_rows(rows, data, cost, asof_ms):
    lookup = {s: {b.open_time: b for b in bars} for s, bars in data.items()}
    result = []
    for row in rows:
        entry = lookup[row['symbol']].get(row['entry_ms'])
        exit_bar = lookup[row['symbol']].get(row['exit_ms'])
        # Cache supplies completed bars: an outcome is usable only once available.
        if entry is None or exit_bar is None or exit_bar.close_time >= asof_ms:
            continue
        result.append({**row, 'y': net_return(entry.open, exit_bar.open, cost),
                       'label_available_ms': exit_bar.close_time+1})
    return result


def fit_tree(rows, depth=3, min_leaf=200, min_days=30):
    import numpy as np  # Lazy: optional research import failure cannot stop trading.
    x = np.asarray([r['x'] for r in rows], dtype=float)
    y = np.asarray([r['y'] for r in rows], dtype=float)
    days = np.asarray([r['feature_ms'] for r in rows], dtype='int64')
    if len(rows)<min_leaf or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Insufficient or non-finite training data')
    def build(indices, remaining, path):
        values = y[indices]
        node = {'id': path, 'prediction_pct': float(values.mean()), 'samples': len(indices),
                'days': int(len(np.unique(days[indices]))), 'positive_fraction': float((values>0).mean())}
        if remaining==0 or len(indices)<2*min_leaf:
            return node
        best, best_loss = None, float(((values-values.mean())**2).sum())
        for feature in range(len(FEATURES)):
            for threshold in np.unique(np.quantile(x[indices, feature], SPEC['quantiles'])):
                mask = x[indices, feature]<=threshold
                left, right = indices[mask], indices[~mask]
                if min(len(left),len(right))<min_leaf:
                    continue
                if min(len(np.unique(days[left])),len(np.unique(days[right])))<min_days:
                    continue
                yl, yr = y[left], y[right]
                loss = float(((yl-yl.mean())**2).sum()+((yr-yr.mean())**2).sum())
                if loss < best_loss-1e-9:
                    best_loss, best = loss, (feature,float(threshold),left,right)
        if best is not None:
            feature, threshold, left, right = best
            node.update(feature=feature, threshold=threshold,
                        left=build(left,remaining-1,path+'L'), right=build(right,remaining-1,path+'R'))
        return node
    return build(np.arange(len(rows)), depth, 'root')


def predict(tree, x):
    node = tree
    while 'feature' in node:
        node = node['left'] if x[node['feature']]<=node['threshold'] else node['right']
    return node


def rules(tree, conditions=None):
    conditions = conditions or []
    if 'feature' not in tree:
        return [{**tree, 'conditions': conditions}]
    name, cutoff = FEATURES[tree['feature']], tree['threshold']
    return (rules(tree['left'], conditions+[{'feature':name,'op':'<=','value':cutoff}]) +
            rules(tree['right'], conditions+[{'feature':name,'op':'>','value':cutoff}]))


def train_and_validate(rows, config):
    import numpy as np
    dates = sorted({r['feature_ms'] for r in rows})
    if len(dates)<SPEC['min_history_days']:
        raise ValueError(f"Need {SPEC['min_history_days']} common labelled days; have {len(dates)}")
    folds, predictions, baselines = [], [], []
    first = len(dates)-SPEC['folds']*SPEC['fold_days']
    for n in range(SPEC['folds']):
        lo = dates[first+n*SPEC['fold_days']]
        hi = dates[first+(n+1)*SPEC['fold_days']-1]+DAY_MS
        training = [r for r in rows if r['label_available_ms']<lo and r['feature_ms']>=lo-SPEC['training_days']*DAY_MS]
        testing = [r for r in rows if lo<=r['feature_ms']<hi]
        if len({r['feature_ms'] for r in training})<365:
            raise ValueError('Need one full training year before each chronological test fold')
        tree = fit_tree(training, SPEC['max_depth'], SPEC['min_leaf'], SPEC['min_leaf_days'])
        average = mean(r['y'] for r in training)
        fold_preds = [{**r, 'prediction_pct':predict(tree,r['x'])['prediction_pct']} for r in testing]
        mse = mean((r['prediction_pct']-r['y'])**2 for r in fold_preds)
        naive = mean((average-r['y'])**2 for r in testing)
        positive = [r['y'] for r in fold_preds if r['prediction_pct']>SPEC['entry_threshold_pct']]
        folds.append({'test_start_ms':lo,'test_end_ms':hi,'training_rows':len(training),
            'last_training_label_ms':max(r['label_available_ms'] for r in training),
            'test_rows':len(testing),'test_days':len({r['feature_ms'] for r in testing}),
            'rmse_pct':math.sqrt(mse),'mean_baseline_rmse_pct':math.sqrt(naive),
            'selected_rows':len(positive),'selected_avg_net_pct':mean(positive) if positive else None,
            'beats_mean_baseline':mse<naive})
        predictions.extend(fold_preds)
        baselines.extend([average]*len(testing))
    cutoff = dates[-1]-SPEC['training_days']*DAY_MS
    final_rows = [r for r in rows if r['feature_ms']>=cutoff]
    tree = fit_tree(final_rows, SPEC['max_depth'], SPEC['min_leaf'], SPEC['min_leaf_days'])
    test_rmse = math.sqrt(mean((r['prediction_pct']-r['y'])**2 for r in predictions))
    baseline_rmse = math.sqrt(mean((p-r['y'])**2 for p,r in zip(baselines,predictions)))
    return {'tree':tree,'rules':rules(tree),'training_rows':len(final_rows),'numpy_version':np.__version__,
        'training_start_ms':min(r['feature_ms'] for r in final_rows),
        'last_training_label_ms':max(r['label_available_ms'] for r in final_rows),
        'validation':{'label':'Retrospective walk-forward diagnostics; not a fresh holdout',
            'folds':folds,'rmse_pct':test_rmse,'mean_baseline_rmse_pct':baseline_rmse,
            'beats_mean_baseline':test_rmse<baseline_rmse,'rows':len(predictions),
            'note':'Rows overlap in time and share market exposure; row counts are not independent trades.'}}, predictions


def paper_portfolio(data, forecasts, config, start_ms, end_ms):
    """Long-only, max three positions; seven-day open-to-open exits, no leverage.

    Forecasts must exist before their planned entry open. No invented forecast for
    service downtime. Ledger can be replayed deterministically without any orders.
    """
    if end_ms<=start_ms:
        return None
    symbols=config['symbols'];cost=config['cost'];cash=float(config['capital'])
    lookup={s:{b.open_time:b for b in data[s]} for s in symbols}
    scheduled={}
    for r in forecasts:
        if r['issued_ms']>=r['entry_ms']:
            raise ValueError('Forecast was not recorded before its entry open')
        scheduled.setdefault(r['entry_ms'],[]).append(r)
    positions={};trades=[];curve=[];skips=[]
    for day in range(start_ms,end_ms,DAY_MS):
        if any(day not in lookup[s] for s in symbols):
            raise ValueError('Missing paper comparison candle; cannot guess a fill or mark')
        for s,p in list(positions.items()):
            if day>=p['exit_ms']:
                proceeds=p['quantity']*lookup[s][day].open*(1-cost['slip'])*(1-cost['fee'])
                cash+=proceeds; pnl=proceeds-p['cost']
                trades.append({**p,'symbol':s,'exit_time_ms':day,'pnl':pnl,'r':pnl/p['risk'] if p['risk'] else 0})
                del positions[s]
        for r in sorted(scheduled.get(day,[]),key=lambda r:(-r['prediction_pct'],r['symbol'])):
            s=r['symbol']
            if r['prediction_pct']<=SPEC['entry_threshold_pct']:
                continue
            if s in positions or len(positions)>=SPEC['max_positions']:
                skips.append({'symbol':s,'entry_ms':day,'reason':'Already held or three-position limit'})
                continue
            price=lookup[s][day].open*(1+cost['slip'])
            equity=cash+sum(p['quantity']*lookup[k][day].open for k,p in positions.items())
            distance=2*r['atr']
            if distance<=0:
                continue
            qty=min(equity*config['risk_pct']/100/distance,cash/(price*(1+cost['fee'])))
            if qty*price<cost['min_notional']:
                skips.append({'symbol':s,'entry_ms':day,'reason':'Cash or minimum notional'})
                continue
            spent=qty*price*(1+cost['fee']);cash=max(0,cash-spent)
            positions[s]={'entry_time_ms':day,'exit_ms':r['exit_ms'],'quantity':qty,'cost':spent,
                          'risk':qty*distance,'model_id':r.get('model_id'),'prediction_pct':r['prediction_pct']}
        equity=cash+sum(p['quantity']*lookup[s][day].close for s,p in positions.items())
        curve.append([day,equity])
    peak=config['capital'];dd=0
    for _,value in curve:
        peak=max(peak,value);dd=max(dd,100*(1-value/peak))
    wins=[t for t in trades if t['pnl']>0];losses=[t for t in trades if t['pnl']<0]
    gains=sum(t['pnl'] for t in wins);loss=-sum(t['pnl'] for t in losses)
    metrics={'start_ms':start_ms,'end_ms':end_ms,'days':len(curve),'ending_equity':curve[-1][1],
        'return_pct':100*(curve[-1][1]/config['capital']-1),'net_profit':curve[-1][1]-config['capital'],
        'max_drawdown_pct':dd,'closed_trades':len(trades),'open_positions':len(positions),
        'profit_factor':gains/loss if loss else None,'win_rate':100*len(wins)/len(trades) if trades else None,
        'avg_win_r':mean(t['r'] for t in wins) if wins else None,'avg_loss_r':mean(t['r'] for t in losses) if losses else None,
        'fee_bps':cost['fee']*10000,'slippage_bps':cost['slip']*10000,'cash':cash,'skipped_entries':len(skips)}
    return {'metrics':metrics,'trades':trades,'positions':positions,'equity':curve,'skips':skips}
