"""SQLite-backed snapshot cache. Mount SNIPELAB_DATA_DIR to a persistent Northflank volume."""
import json
from contextlib import closing
import os
import sqlite3
import time
from pathlib import Path

DATA_DIR=Path(os.getenv("SNIPELAB_DATA_DIR","/tmp"))
DB_PATH=Path(os.getenv("SNIPELAB_DB_PATH",str(DATA_DIR/"snipelab.sqlite3")))

def connect():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(DB_PATH),timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)")
    return conn

def load(names):
    result={}
    with closing(connect()) as conn:
        for name in names:
            row=conn.execute("SELECT value FROM snapshots WHERE name=?",(name,)).fetchone()
            if row:
                result[name]=json.loads(row[0])
    return result

def iter_snapshots(names):
    """Decode one durable collection at a time instead of materializing them all."""
    with closing(connect()) as conn:
        for name in names:
            row=conn.execute("SELECT value FROM snapshots WHERE name=?",(name,)).fetchone()
            if row:
                yield name,json.loads(row[0])

def save(data):
    now=time.time()
    rows=[(k,json.dumps(v,separators=(",",":")),now) for k,v in data.items()]
    with closing(connect()) as conn, conn:
        conn.executemany("INSERT INTO snapshots(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",rows)

def status():
    return {"path":str(DB_PATH),"persistent_volume_required":str(DB_PATH).startswith("/tmp"),"exists":DB_PATH.exists()}


def snapshot_info():
    """Last durable snapshot metadata; proves persistence only after restart."""
    with closing(connect()) as conn:
        rows=conn.execute("SELECT name,updated_at FROM snapshots").fetchall()
    return {"collections":{name:{"saved_at_epoch":ts} for name,ts in rows},"count":len(rows)}
