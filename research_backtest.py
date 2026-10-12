"""Deterministic daily portfolio simulator. No exchange orders or network access."""
import math
import random
from collections import deque
from statistics import mean
from strategy import DAY_MS, wilders_adx_series
from daily_history import validate


def features(bars, rules):
    validate(bars)
    adx = wilders_adx_series(bars, rules['adx_period'])
    n = rules['atr_period']
    atr = None
    seed = []
    result = []
    highs, lows = deque(), deque()
    for i, b in enumerate(bars):
        prev = bars[i-1].close if i else b.close
        tr = max(b.high-b.low, abs(b.high-prev), abs(b.low-prev))
        if atr is None:
            seed.append(tr)
            if len(seed) == n:
                atr = sum(seed)/n
        else:
            atr = (atr*(n-1)+tr)/n
        entry_p, exit_p = rules['entry_period'], rules['exit_period']
        while highs and highs[0][0] < i-entry_p:
            highs.popleft()
        while lows and lows[0][0] < i-exit_p:
            lows.popleft()
        eh = highs[0][1] if i >= entry_p else None
        el = lows[0][1] if i >= exit_p else None
        result.append({'atr': atr, 'adx': adx[i], 'high': eh, 'low': el,
                       'buy': eh is not None and b.close > eh and adx[i] is not None and adx[i] > rules['adx_threshold'],
                       'sell': el is not None and b.close < el})
        while highs and highs[-1][1] <= b.high:
            highs.pop()
        highs.append((i,b.high))
        while lows and lows[-1][1] >= b.low:
            lows.pop()
        lows.append((i,b.low))
    return result


def simulate(data, config, start_ms, end_ms):
    """[start, end) complete daily bars; decisions at close, fill next open.

    Signals before start are NOT traded. Flat and equal capital at each comparison
    start. Exit orders first, then list-ordered entries. No leverage/stop/TP.
    All configured symbols must have contiguous history through end.
    """
    symbols = config['symbols']; rules = config['rules']; cost = config['cost']
    cash = float(config['capital']); fee, slip = cost['fee'], cost['slip']
    prepared, indices = {}, {}
    for s in symbols:
        if s not in data:
            raise ValueError(f'Missing {s} history')
        bars = data[s]
        prepared[s] = features(bars, rules)
        indices[s] = {b.open_time:i for i,b in enumerate(bars)}
        if any(t not in indices[s] for t in range(start_ms, end_ms, DAY_MS)):
            raise ValueError(f'{s}: comparison window has missing days')
        first = indices[s][start_ms]
        if first < max(rules['entry_period'], rules['exit_period'], rules['atr_period'], 2*rules['adx_period']):
            raise ValueError(f'{s}: insufficient warmup before comparison window')
    positions, pending_buys, pending_sells = {}, [], []
    trades, equity, daily_returns = [], [], []
    previous_equity = cash
    for t in range(start_ms, end_ms, DAY_MS):
        current = {s:data[s][indices[s][t]] for s in symbols}
        for s in pending_sells:
            if s not in positions:
                continue
            p = positions.pop(s)
            proceeds = p['qty']*current[s].open*(1-slip)*(1-fee)
            pnl = proceeds-p['cost']; cash += proceeds
            trades.append({'symbol':s, 'entry_time_ms':p['time'], 'exit_time_ms':t,
                           'pnl':pnl, 'r':pnl/p['risk'] if p['risk'] else 0})
        for s, atr in pending_buys:
            if s in positions or not atr or atr <= 0:
                continue
            marked = cash + sum(p['qty']*current[a].open for a,p in positions.items())
            fill = current[s].open*(1+slip)
            qty = min(marked*config['risk_pct']/100/(rules['atr_multiplier']*atr), cash/(fill*(1+fee)))
            notional = qty*fill
            if qty <= 0 or notional < cost['min_notional']:
                continue
            spent = notional*(1+fee); cash = max(0.0,cash-spent)
            positions[s] = {'qty':qty, 'cost':spent, 'time':t, 'risk':qty*rules['atr_multiplier']*atr}
        pending_buys, pending_sells = [], []
        for s in symbols:
            row = prepared[s][indices[s][t]]
            if s in positions:
                if row['sell']:
                    pending_sells.append(s)
            elif row['buy']:
                pending_buys.append((s,row['atr']))
        value = cash+sum(p['qty']*current[s].close for s,p in positions.items())
        equity.append([t,value]); daily_returns.append(value/previous_equity-1 if previous_equity else 0)
        previous_equity = value
    if not equity:
        raise ValueError('Empty comparison period')
    peak = config['capital']; dd = 0.0
    for _,value in equity:
        peak=max(peak,value); dd=max(dd,(1-value/peak)*100 if peak else 0)
    wins = [t for t in trades if t['pnl']>0]; losses=[t for t in trades if t['pnl']<0]
    gain=sum(t['pnl'] for t in wins); loss=-sum(t['pnl'] for t in losses)
    metrics = {'start_ms':start_ms, 'end_ms':end_ms, 'days':len(equity), 'closed_trades':len(trades),
        'open_positions':len(positions), 'net_profit':equity[-1][1]-config['capital'],
        'return_pct':(equity[-1][1]/config['capital']-1)*100, 'max_drawdown_pct':dd,
        'profit_factor':gain/loss if loss else None, 'win_rate':100*len(wins)/len(trades) if trades else None,
        'avg_win_r':mean(t['r'] for t in wins) if wins else None,
        'avg_loss_r':mean(t['r'] for t in losses) if losses else None,
        'symbols_traded':len({t['symbol'] for t in trades}), 'fee_bps':fee*10000, 'slippage_bps':slip*10000,
        'ending_equity':equity[-1][1], 'closed_pnl':sum(t['pnl'] for t in trades)}
    return {'metrics':metrics, 'equity':equity, 'returns':daily_returns, 'trades':trades}


def paired_lower_bound(active, candidate, alpha, seed, samples=5000, block_days=7):
    """Circular paired block-bootstrap of daily return differences.

    Operational screening heuristic, not proof of edge; correlated daily returns
    are sampled in blocks, with the same dates for both strategies.
    """
    if len(active)!=len(candidate) or not active:
        raise ValueError('Paired return series must share dates and length')
    diff=[b-a for a,b in zip(active,candidate)]
    rng=random.Random(seed); n=len(diff); estimates=[]
    blocks=[sum(diff[(i+j)%n] for j in range(block_days)) for i in range(n)]
    count, tail=divmod(n,block_days)
    for _ in range(samples):
        total=sum(blocks[rng.randrange(n)] for _ in range(count))
        index=rng.randrange(n)
        total+=sum(diff[(index+j)%n] for j in range(tail))
        estimates.append(total/n)
    estimates.sort()
    return estimates[max(0,int(alpha*samples)-1)]
