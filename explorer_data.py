"""Resumable public hourly candles and strict UTC resampling for exploration."""
import math
import time
import shutil
from pathlib import Path
from dataclasses import asdict
from pattern_store import PatternStore, packed
from strategy import Candle, DAY_MS

HOUR_MS=3600000
FRAMES={'1h':HOUR_MS,'4h':4*HOUR_MS,'1d':DAY_MS}


def validate_bars(bars,interval):
    for i,b in enumerate(bars):
        if not all(math.isfinite(v) for v in (b.open,b.high,b.low,b.close,b.volume,b.quote_volume)):
            raise ValueError('Non-finite candle')
        if min(b.open,b.high,b.low,b.close)<=0 or min(b.volume,b.quote_volume)<0 or b.low>min(b.open,b.close) or b.high<max(b.open,b.close):
            raise ValueError('Invalid OHLCV')
        if b.open_time%interval or b.close_time!=b.open_time+interval-1:
            raise ValueError('Invalid candle alignment')
        if i and b.open_time!=bars[i-1].open_time+interval:
            raise ValueError('Gap or duplicate in candle history')


def resample(bars,interval,asof_ms):
    validate_bars(bars,HOUR_MS)
    if interval==HOUR_MS:return [b for b in bars if b.close_time<asof_ms]
    offset=3*DAY_MS if interval==7*DAY_MS else 0 # Monday UTC for weekly context.
    groups={}
    for b in bars:
        start=(b.open_time+offset)//interval*interval-offset
        if start+interval>asof_ms:continue
        groups.setdefault(start,[]).append(b)
    out=[];size=interval//HOUR_MS
    for start,group in sorted(groups.items()):
        if len(group)!=size or group[0].open_time!=start or group[-1].close_time!=start+interval-1:
            # Leading partial groups have no complete candle; never interpolate.
            continue
        out.append(Candle(start,group[0].open,max(b.high for b in group),min(b.low for b in group),
            group[-1].close,sum(b.volume for b in group),start+interval-1,sum(b.quote_volume for b in group)))
    if any(b.open_time!=a.open_time+interval for a,b in zip(out,out[1:])):
        raise ValueError('Resampled candles contain a gap')
    return out


class ExplorerStore(PatternStore):
    def __init__(self,path):
        super().__init__(path)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS candles(symbol TEXT,open_time INTEGER,
                open REAL,high REAL,low REAL,close REAL,volume REAL,close_time INTEGER,quote_volume REAL,
                PRIMARY KEY(symbol,open_time));
                CREATE TABLE IF NOT EXISTS trials(id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT UNIQUE NOT NULL,registered_ms INTEGER NOT NULL,status TEXT NOT NULL,
                payload TEXT NOT NULL);''')

    def append(self,symbol,bars):
        validate_bars(bars,HOUR_MS)
        with self.lock,self.connect() as c:
            c.executemany('INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?)',
                          [(symbol,*asdict(b).values()) for b in bars])

    def bounds(self,symbol):
        with self.connect() as c:
            r=c.execute('SELECT MIN(open_time),MAX(open_time),COUNT(*) FROM candles WHERE symbol=?',(symbol,)).fetchone()
            return tuple(r)

    def load(self,symbol,end_ms):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM candles WHERE symbol=? AND close_time<? ORDER BY open_time',(symbol,end_ms)).fetchall()
        bars=[Candle(**{k:r[k] for k in r.keys() if k!='symbol'}) for r in rows]
        validate_bars(bars,HOUR_MS)
        return bars

    def new_trial(self,candidate,now_ms,payload):
        with self.lock,self.connect() as c:
            cid=c.execute('INSERT INTO trials(candidate_id,registered_ms,status,payload) VALUES(?,?,?,?)',
                (candidate['id'],now_ms,'RUNNING',packed({'candidate':candidate,'timeline':[],**payload}))).lastrowid
        return self.trial(cid)

    def trial(self,cid):
        with self.connect() as c:
            r=c.execute('SELECT * FROM trials WHERE id=?',(cid,)).fetchone()
        return {**dict(r),'payload':__import__('json').loads(r['payload'])} if r else None

    def trials(self,limit=1000):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM trials ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        return [{**dict(r),'payload':__import__('json').loads(r['payload'])} for r in rows]

    def stage(self,trial,now_ms,status,message):
        trial['status']=status
        trial['payload']['timeline'].append({'time_ms':now_ms,'status':status,'message':message})
        self.save_trial(trial)
        self.event(now_ms,status,message)

    def save_trial(self,trial):
        with self.lock,self.connect() as c:
            c.execute('UPDATE trials SET status=?,payload=? WHERE id=?',(trial['status'],packed(trial['payload']),trial['id']))


def fetch_page(symbol,start_ms,end_ms):
    import requests
    r=requests.get('https://api.binance.com/api/v3/klines',params={'symbol':symbol,'interval':'1h',
        'startTime':start_ms,'endTime':end_ms-1,'limit':1000},timeout=15)
    if r.status_code in (418,429):
        raise RuntimeError('Public market-data rate limit; downloader will back off and retry later')
    r.raise_for_status()
    raw=r.json()
    if not isinstance(raw,list):raise ValueError('Invalid market-data response')
    return [Candle(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),int(x[6]),float(x[7])) for x in raw]


def refresh_history(store,symbols,start_ms,now_ms,get_page=fetch_page,should_stop=lambda:False,max_pages=60):
    """One bounded batch. Each page commits, so retries/redeployments resume safely."""
    end=now_ms//HOUR_MS*HOUR_MS;pages=0;errors={};began=time.monotonic()
    # Round robin: download gaps for all coins, rather than starving the last coin.
    cursors={s:(store.bounds(s)[1]+HOUR_MS if store.bounds(s)[1] is not None else start_ms) for s in symbols}
    while pages<max_pages and time.monotonic()-began<45 and not should_stop():
        progressed=False
        for symbol in symbols:
            cursor=cursors[symbol]
            if cursor>=end or symbol in errors:continue
            if pages>=max_pages or time.monotonic()-began>=45 or should_stop():break
            try:
                if shutil.disk_usage(store.path.parent).free<64*1024*1024:
                    raise RuntimeError('Insufficient free storage; hourly download paused')
                bars=get_page(symbol,cursor,end);pages+=1
                validate_bars(bars,HOUR_MS)
                if not bars or bars[0].open_time!=cursor or bars[-1].close_time>=end:
                    raise ValueError('Missing first requested hour or unexpected unfinished candle')
                store.append(symbol,bars);cursors[symbol]=bars[-1].open_time+HOUR_MS;progressed=True
            except Exception as exc:
                errors[symbol]=str(exc)[:300]
                # Stop the batch on rate limiting; do not hammer remaining symbols.
                if 'rate limit' in str(exc).lower():break
        if not progressed or any('rate limit' in e.lower() for e in errors.values()):break
    rows=[];expected=max(0,(end-start_ms)//HOUR_MS)
    for s in symbols:
        first,last,count=store.bounds(s)
        rows.append({'symbol':s,'bars':count,'expected':expected,'latest_ms':last,
                     'ready':first==start_ms and last==end-HOUR_MS and count==expected})
    result={'updated_ms':now_ms,'start_ms':start_ms,'end_ms':end,'coins':rows,'errors':errors,
            'available':sum(r['ready'] for r in rows),'required':len(symbols),'pages_this_batch':pages}
    store.put('data',result)
    return result
