"""Durable, bounded journal of observed stock changes. No personal plans here.

Only validated, current fields are supplied by the app. Missing/stale fields
never turn into zero/false. First observations and split changes seed a new
baseline rather than fabricating threshold crossings.
"""
import asyncio
import json
import math
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
import storage

FIELDS={'available','stability','retest','ready','broken','support'}
MAX_ROWS=30000
RETENTION=30*86400


def clean(values):
    out={}
    for key,value in values.items():
        if key not in FIELDS or value is None:continue
        if key in {'ready','broken'}:
            if type(value) is bool:out[key]=value
        elif key=='retest':
            if value in {'waiting','success','failed'}:out[key]=value
        elif type(value) in {float,int} and math.isfinite(value) and value>=0:
            out[key]=round(value,6)
    return out


def important(before,after,changed):
    kinds=[]
    for field in changed:
        a,b=before[field],after[field]
        if field=='available':
            limits=(0,10000,15000,20000,25000,40000)
            crossed=any((a==0)!=(b==0) if n==0 else (a<n)!=(b<n) for n in limits)
            if crossed or a>0 and b<=a*.5:kinds.append(field)
        elif field=='stability':
            if min(a,4)!=min(b,4):kinds.append(field)
        else:kinds.append(field)
    return kinds


class Journal:
    def __init__(self):
        self.baselines={}
        self.current={}
        self.updated_at=None
        self.error=None
        self.ready=False

    def connect(self):
        conn=storage.connect()
        conn.execute('CREATE TABLE IF NOT EXISTS watch_baselines (symbol TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS watch_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL, symbol TEXT NOT NULL, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS watch_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if conn.execute("SELECT value FROM watch_metadata WHERE key='identity'").fetchone() is None:
            conn.execute("INSERT OR IGNORE INTO watch_metadata(key,value) VALUES('identity',?)",(str(uuid.uuid4()),));conn.commit()
        return conn

    def restore(self):
        with closing(self.connect()) as conn:
            self.baselines={symbol:json.loads(value) for symbol,value in conn.execute('SELECT symbol,value FROM watch_baselines')}
        self.ready=True

    def ingest(self,rows,stamp=None):
        stamp=time.time() if stamp is None else stamp
        at=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        baselines=dict(self.baselines)
        entries=[]; updates=[]; current={}
        for sym,row in rows.items():
            values=clean(row.get('values') or {})
            current[sym]={'values':values,'context':row.get('context'),'market':row.get('market','split')}
            if not values:continue
            old=baselines.get(sym)
            reset=old is None or old.get('context')!=row.get('context')
            previous={} if reset else old.get('values',{})
            changed=[k for k in sorted(values) if k in previous and previous[k]!=values[k]]
            after={**previous,**values}
            record=dict(context=row.get('context'),market=row.get('market','split'),values=after)
            baselines[sym]=record;updates.append((sym,json.dumps(record,separators=(',',':'))))
            if changed:
                entry=dict(symbol=sym,at=at,market=row.get('market','split'),context=row.get('context'),
                           before={k:previous[k] for k in changed},after={k:values[k] for k in changed},changed=changed,
                           kinds=important(previous,after,changed))
                entries.append((stamp,sym,json.dumps(entry,separators=(',',':'))))
        # Update baseline and append changes atomically: restarting cannot replay them.
        with closing(self.connect()) as conn,conn:
            conn.executemany('INSERT INTO watch_baselines(symbol,value) VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET value=excluded.value',updates)
            conn.executemany('INSERT INTO watch_changes(observed,symbol,value) VALUES(?,?,?)',entries)
            conn.execute('DELETE FROM watch_changes WHERE observed<?',(stamp-RETENTION,))
            conn.execute('DELETE FROM watch_changes WHERE seq <= COALESCE((SELECT seq FROM watch_changes ORDER BY seq DESC LIMIT 1 OFFSET ?),0)',(MAX_ROWS,))
        self.baselines=baselines;self.current=current;self.updated_at=at;self.error=None
        return len(entries)

    def page(self,after=None,limit=250,symbols=(),journal_id=None):
        limit=max(1,min(300,int(limit)))
        with closing(self.connect()) as conn:
            identity=conn.execute("SELECT value FROM watch_metadata WHERE key='identity'").fetchone()[0]
            oldest,latest=conn.execute('SELECT MIN(seq),MAX(seq) FROM watch_changes').fetchone()
            reset=bool(journal_id and journal_id!=identity or after is not None and after>(latest or 0))
            if reset:after=None
            # First visit starts with recent history; reconnects replay exact pages.
            if after is None:
                records=list(reversed(conn.execute('SELECT seq,value FROM watch_changes ORDER BY seq DESC LIMIT ?',(limit,)).fetchall()))
            else:
                records=conn.execute('SELECT seq,value FROM watch_changes WHERE seq>? ORDER BY seq LIMIT ?',(after,limit)).fetchall()
        events=[dict(json.loads(value),seq=seq) for seq,value in records]
        cursor=events[-1]['seq'] if events else max(after or 0,latest or 0)
        return dict(journal_id=identity,reset=reset,events=events,next_cursor=cursor,latest_cursor=latest or 0,oldest_cursor=oldest or 0,
                    has_more=bool(latest and cursor<latest),truncated=bool(after is not None and oldest and after<oldest-1),
                    observations={s:self.current[s] for s in symbols if s in self.current},
                    observed_at=self.updated_at,status='error' if self.error else 'ready' if self.updated_at else 'starting')


JOURNAL=Journal()


async def worker(get_rows):
    try:await asyncio.to_thread(JOURNAL.restore)
    except Exception:JOURNAL.error='تعذر استعادة سجل التغييرات'
    while True:
        try:
            if not JOURNAL.ready:await asyncio.to_thread(JOURNAL.restore)
            rows=get_rows()
            await asyncio.to_thread(JOURNAL.ingest,rows)
        except Exception:JOURNAL.error='تعذر تحديث سجل التغييرات'
        await asyncio.sleep(20)
