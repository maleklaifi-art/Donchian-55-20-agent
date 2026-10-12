"""Durable research audit and point-in-time decisions, sharing the trade database."""
import json
import time
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ResearchStore:
    def __init__(self, db):
        self.db = db
        with db._lock, db._connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS research_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_cycles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_decisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                signal_close_ms INTEGER NOT NULL, strategy_version TEXT NOT NULL,
                observed_at TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                execution TEXT NOT NULL, execution_reason TEXT,
                payload TEXT NOT NULL, outcomes TEXT NOT NULL DEFAULT '{}',
                UNIQUE(symbol, signal_close_ms, strategy_version));
            CREATE INDEX IF NOT EXISTS idx_decisions_close ON daily_decisions(signal_close_ms DESC);
            ''')

    def meta(self, key, default=None):
        with self.db._connect() as c:
            row = c.execute('SELECT value FROM research_meta WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def put_meta(self, key, value):
        with self.db._lock, self.db._connect() as c:
            c.execute('INSERT INTO research_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, json.dumps(value, allow_nan=False)))

    def record_decisions(self, snapshot, config, execution_ready, enabled):
        with self.db._lock, self.db._connect() as c:
            for row in snapshot.get('decisions', []):
                action = row.get('action', 'NO_ACTION')
                status = 'SKIPPED' if action == 'NO_ACTION' else 'PENDING'
                reason = row.get('reason', '')
                if action == 'LONG':
                    reason = 'Close exceeded entry channel and ADX passed the entry threshold'
                    if not execution_ready:
                        status = 'NOT_READY'
                    elif not enabled:
                        status = 'PAUSED'
                elif action == 'EXIT':
                    reason = 'Held coin closed below its exit channel'
                    if not execution_ready:
                        status = 'NOT_READY'
                payload = {**row, 'config': config}
                # First valid observation is immutable. A data failure can be repaired
                # on a retry; a successful trade must never turn into a later "Held" skip.
                c.execute('''INSERT INTO daily_decisions(symbol,signal_close_ms,strategy_version,
                    observed_at,action,reason,execution,payload) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,signal_close_ms,strategy_version) DO UPDATE SET
                    observed_at=excluded.observed_at, action=excluded.action,reason=excluded.reason,
                    execution=excluded.execution,payload=excluded.payload
                    WHERE json_extract(daily_decisions.payload,'$.stage') IN ('Data','Universe')
                    AND json_extract(excluded.payload,'$.stage') NOT IN ('Data','Universe')''',
                    (row['symbol'], row['signal_close_ms'], row['strategy_version'], stamp(),
                     action, reason, status, json.dumps(payload, allow_nan=False)))

    def execution_event(self, event, payload):
        key = payload.get('exit_signal_key') if event == 'POSITION_CLOSED' else payload.get('signal_key')
        if not key or ':' not in key:
            return
        symbol, close = key.rsplit(':', 1)
        if not close.isdigit():
            return
        statuses = {'POSITION_OPENED': 'FILLED', 'POSITION_CLOSED': 'EXIT_FILLED',
                    'TRADE_REJECTED': 'REJECTED', 'EXIT_REJECTED': 'REJECTED',
                    'ENTRY_ERROR': 'ERROR', 'EXIT_ERROR': 'ERROR', 'ENTRY_FAILED': 'ERROR'}
        if event not in statuses:
            return
        with self.db._lock, self.db._connect() as c:
            c.execute('''UPDATE daily_decisions SET execution=?,execution_reason=?
                WHERE symbol=? AND signal_close_ms=? AND action != 'NO_ACTION'
                AND execution NOT IN ('FILLED','EXIT_FILLED')''',
                (statuses[event], payload.get('reason') or payload.get('error') or event, symbol, int(close)))

    def new_cycle(self, payload):
        with self.db._lock, self.db._connect() as c:
            now = stamp()
            cur = c.execute('INSERT INTO research_cycles(created_at,updated_at,status,stage,payload) VALUES(?,?,?,?,?)',
                            (now, now, 'RUNNING', 'analyze', json.dumps(payload, allow_nan=False)))
            cid = cur.lastrowid
        return self.cycle(cid)

    def cycle(self, cid):
        with self.db._connect() as c:
            row = c.execute('SELECT * FROM research_cycles WHERE id=?', (cid,)).fetchone()
            return {**dict(row), 'payload': json.loads(row['payload'])} if row else None

    def save_cycle(self, cycle):
        cycle['updated_at'] = stamp()
        with self.db._lock, self.db._connect() as c:
            c.execute('UPDATE research_cycles SET updated_at=?,status=?,stage=?,payload=? WHERE id=?',
                      (cycle['updated_at'], cycle['status'], cycle['stage'],
                       json.dumps(cycle['payload'], allow_nan=False), cycle['id']))

    def stage(self, cycle, stage, message, status='RUNNING', **details):
        cycle['stage'], cycle['status'] = stage, status
        cycle['payload'].setdefault('timeline', []).append(
            {'time': stamp(), 'stage': stage, 'message': message, **details})
        self.save_cycle(cycle)
        self.put_meta('notice', message)

    def cycles(self, limit=30):
        with self.db._connect() as c:
            rows = c.execute('SELECT * FROM research_cycles ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    def pending(self):
        with self.db._connect() as c:
            row = c.execute("SELECT id FROM research_cycles WHERE status IN ('RUNNING','OBSERVING','READY') ORDER BY id LIMIT 1").fetchone()
            return self.cycle(row['id']) if row else None

    def decisions(self, limit=200, offset=0, symbol=None, skipped=False):
        clauses, args = [], []
        if symbol:
            clauses.append('symbol=?'); args.append(symbol)
        if skipped:
            clauses.append("execution NOT IN ('FILLED','EXIT_FILLED') AND action != 'EXIT' AND COALESCE(json_extract(payload,'$.holding'),0)=0")
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.db._connect() as c:
            total = c.execute('SELECT COUNT(*) FROM daily_decisions' + where, args).fetchone()[0]
            rows = c.execute('SELECT * FROM daily_decisions' + where + ' ORDER BY signal_close_ms DESC,id DESC LIMIT ? OFFSET ?',
                             [*args, limit, offset]).fetchall()
        return {'total': total, 'rows': [{**dict(r), 'payload': json.loads(r['payload']),
                                         'outcomes': json.loads(r['outcomes'])} for r in rows]}

    def analysis_summary(self):
        with self.db._connect() as c:
            groups = c.execute("""SELECT json_extract(payload,'$.stage') AS stage,
                COUNT(*) AS decisions,
                COUNT(json_extract(outcomes,'$."7".hypothetical_net_pct')) AS matured_7d,
                AVG(json_extract(outcomes,'$."7".hypothetical_net_pct')) AS avg_7d_net_pct,
                SUM(CASE WHEN json_extract(outcomes,'$."7".hypothetical_net_pct')>0 THEN 1 ELSE 0 END) AS positive_7d,
                COUNT(json_extract(outcomes,'$.channel_exit.net_pct')) AS matured_channel_exits,
                AVG(json_extract(outcomes,'$.channel_exit.net_pct')) AS avg_channel_exit_net_pct
                FROM daily_decisions GROUP BY json_extract(payload,'$.stage')""").fetchall()
            return {'groups':[dict(g) for g in groups],
                    'note':'All recorded decisions, including losses. Overlapping horizons are descriptive, not independent trades. Recent outcomes remain censored until mature.'}

    def decision_symbols(self):
        with self.db._connect() as c:
            return [row[0] for row in c.execute('SELECT DISTINCT symbol FROM daily_decisions')]

    def update_outcomes(self, data, now_ms):
        """Measure *all* observed coins, not a hindsight-selected winners list.

        Horizon returns start at the next daily open and use complete candles only.
        MFE/MAE are descriptions, never the assumed exit or executable profits.
        """
        from strategy import DAY_MS
        with self.db._connect() as c:
            rows = c.execute("SELECT * FROM daily_decisions WHERE json_extract(outcomes,'$.complete') IS NOT 1").fetchall()
        for r in rows:
            p, old = json.loads(r['payload']), json.loads(r['outcomes'])
            if p.get('stage') in ('Data','Universe'):
                continue
            bars = data.get(r['symbol'], [])
            lookup = {b.open_time: b for b in bars}
            entry_time = r['signal_close_ms'] + 1
            first = lookup.get(entry_time)
            if first is None:
                continue
            outcomes = dict(old)
            cost = p['config']['cost']
            buy = first.open * (1 + cost['slip'])
            fee = cost['fee']
            for horizon in (1, 7, 30):
                window = [lookup.get(entry_time + k*DAY_MS) for k in range(horizon)]
                if any(b is None or b.close_time >= now_ms for b in window):
                    continue
                outcomes[str(horizon)] = {
                    'return_pct': (window[-1].close/first.open-1)*100,
                    'mfe_pct': (max(b.high for b in window)/first.open-1)*100,
                    'mae_pct': (min(b.low for b in window)/first.open-1)*100,
                    'hypothetical_net_pct': (window[-1].close*(1-cost['slip'])*(1-fee)/(buy*(1+fee))-1)*100,
                }
            # Counterfactual exit follows the original recorded exit rule, next open.
            # A per-signal unit trade, deliberately NOT a cash-constrained portfolio.
            start_idx = next((i for i,b in enumerate(bars) if b.open_time == entry_time), None)
            period = p.get('exit_period', p['config']['rules']['exit_period'])
            if start_idx is not None and 'channel_exit' not in outcomes:
                for i in range(start_idx, len(bars)-1):
                    if i < period or bars[i+1].close_time >= now_ms:
                        continue
                    if bars[i].close < min(b.low for b in bars[i-period:i]):
                        sell = bars[i+1].open*(1-cost['slip'])
                        outcomes['channel_exit'] = {'exit_time_ms': bars[i+1].open_time,
                            'net_pct': (sell*(1-fee)/(buy*(1+fee))-1)*100}
                        break
            outcomes['reference_open'] = first.open
            outcomes['complete'] = '30' in outcomes and 'channel_exit' in outcomes
            with self.db._lock, self.db._connect() as c:
                c.execute('UPDATE daily_decisions SET outcomes=? WHERE id=?',
                          (json.dumps(outcomes, allow_nan=False), r['id']))
