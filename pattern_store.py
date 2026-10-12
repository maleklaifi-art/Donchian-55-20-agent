"""Pattern research has a separate SQLite file; it never writes trade state."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def packed(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


class PatternStore:
    def __init__(self, path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS models(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_ms INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_ms INTEGER NOT NULL,symbol TEXT NOT NULL,model_id INTEGER NOT NULL,
                    issued_ms INTEGER NOT NULL,entry_ms INTEGER NOT NULL,exit_ms INTEGER NOT NULL,
                    prediction_pct REAL NOT NULL,leaf_id TEXT NOT NULL,atr REAL NOT NULL,
                    features TEXT NOT NULL,outcome TEXT,
                    UNIQUE(feature_ms,symbol));
                CREATE INDEX IF NOT EXISTS forecasts_date ON forecasts(feature_ms DESC);
                CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_ms INTEGER NOT NULL,stage TEXT NOT NULL,message TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def meta(self,key,default=None):
        with self.connect() as c:
            row=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else default

    def put(self,key,value):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,packed(value)))

    def event(self,time_ms,stage,message):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO activity(time_ms,stage,message) VALUES(?,?,?)',(time_ms,stage,message))
        self.put('status',{'stage':stage,'message':message,'updated_ms':time_ms})

    def add_model(self,created_ms,payload):
        with self.lock,self.connect() as c:
            return c.execute('INSERT INTO models(created_ms,payload) VALUES(?,?)',(created_ms,packed(payload))).lastrowid

    def models(self,limit=12,full=False):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM models ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        out=[]
        for row in rows:
            payload=json.loads(row['payload'])
            if not full: payload.pop('tree',None)
            out.append({'id':row['id'],'created_ms':row['created_ms'],**payload})
        return out

    def record_forecasts(self,rows):
        with self.lock,self.connect() as c:
            for r in rows:
                if r['issued_ms']>=r['entry_ms'] or r['feature_ms']>r['issued_ms']:
                    raise ValueError('Forecast must precede entry and follow feature availability')
                c.execute('''INSERT OR IGNORE INTO forecasts(feature_ms,symbol,model_id,issued_ms,entry_ms,
                    exit_ms,prediction_pct,leaf_id,atr,features) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    tuple(r[k] for k in ('feature_ms','symbol','model_id','issued_ms','entry_ms','exit_ms',
                                        'prediction_pct','leaf_id','atr'))+(packed(r['x']),))

    def forecasts(self,limit=200,offset=0,pending_only=False,all_rows=False):
        where=' WHERE outcome IS NULL' if pending_only else ''
        with self.connect() as c:
            total=c.execute('SELECT COUNT(*) FROM forecasts'+where).fetchone()[0]
            query='SELECT * FROM forecasts'+where+' ORDER BY feature_ms DESC,symbol'
            rows=c.execute(query if all_rows else query+' LIMIT ? OFFSET ?',() if all_rows else (limit,offset)).fetchall()
        out=[]
        for row in rows:
            r=dict(row);r['x']=json.loads(r.pop('features'));r['outcome']=json.loads(r['outcome']) if r['outcome'] else None
            out.append(r)
        return {'total':total,'rows':out}

    def outcome(self,row_id,value):
        with self.lock,self.connect() as c:
            c.execute('UPDATE forecasts SET outcome=? WHERE id=? AND outcome IS NULL',(packed(value),row_id))

    def scorecard(self):
        with self.connect() as c:
            rows=c.execute('''SELECT model_id,COUNT(*) AS forecasts,
                COUNT(outcome) AS matured,
                AVG(json_extract(outcome,'$.squared_error')) AS mse,
                AVG(json_extract(outcome,'$.net_return_pct')) AS average_actual_pct,
                AVG(CASE WHEN outcome IS NOT NULL THEN
                    CASE WHEN (prediction_pct>0)=(json_extract(outcome,'$.net_return_pct')>0) THEN 100.0 ELSE 0.0 END END) AS direction_accuracy_pct
                FROM forecasts GROUP BY model_id ORDER BY model_id DESC''').fetchall()
        import math
        return [{**dict(r),'rmse_pct':math.sqrt(r['mse']) if r['mse'] is not None else None} for r in rows]

    def snapshot(self):
        with self.connect() as c:
            activities=[dict(r) for r in c.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 30')]
        return {'status':self.meta('status',{'stage':'WAITING','message':'Waiting for complete daily history.'}),
            'config':self.meta('config'),'data':self.meta('data'),'models':self.models(),
            'forecasts':self.forecasts(),'scorecard':self.scorecard(),'activity':activities,
            'paper':self.meta('paper'),'next_training_ms':self.meta('next_training_ms')}
