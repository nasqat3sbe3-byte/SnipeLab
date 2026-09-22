import asyncio
import ftplib
import io
import os
import re
import json
from pathlib import Path
import time
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import httpx
from history import worker as historical_worker
import storage
from event_rules import borrow_events, ready_event, worker_health
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="SnipeLab Engine", version="0.6.0")
BOOTED_AT = datetime.now(timezone.utc)

UNIVERSE_SEED = ["MSGY","WCT","NCT","EPOW","CPOP","LGCL","NRSN","HUBC","MGN","FGL","OMH","AIXI","SFWL","TNMG","LRHC","RCON","CXAI","YYAI","YXT","RBNE","CISS","IZM","GAUZ","LGHL","UCAR","HLSQ","ALP","GTBP","GOSS","JAGX","NFE","IPDN","NXXT","ENLV","STKH","TRIB","FFAI"]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_FILE = "ftp2.interactivebrokers.com", "shortstock", "", "usa.txt"
SPLITS_URLS = ("https://stockanalysis.com/actions/splits/2026/", "https://stockanalysis.com/actions/splits/")
# Exchange-confirmed corporate actions supplement the lagging public calendar.
# Dates below are first split-adjusted TRADING dates, not legal effective times.
CONFIRMED_SPLITS = {
    "WHLR": {"symbol":"WHLR","company":"Wheeler Real Estate Investment Trust, Inc.",
             "effective_date":"2026-09-22","ratio":"1 for 9",
             "source":"Nasdaq Equity Corporate Actions ECA2026-666",
             "source_url":"https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-666"}
}


STATE = {
    "status":"starting","heartbeat":None,"heartbeat_count":0,"booted_at":BOOTED_AT.isoformat(),
    "universe_count":len(UNIVERSE_SEED),"last_universe_sync":None,"universe_error":None,"universe_attempts":0,"universe_source":"seed",
    "market_scan_count":0,"last_market_scan":None,"market_ok":0,"market_failed":0,"market_total_cached":0,"last_market_error":None,"market_cursor":0,"market_cycle":0,
    "borrow_scan_count":0,"last_borrow_scan":None,"borrow_ok":0,"borrow_missing":0,"last_borrow_error":None,
    "analytics_count":0,"last_analytics":None,"last_halt_scan":None,"halt_error":None,"last_news_scan":None,"news_error":None,"last_state_save":None,"persistence_error":None,"pid":os.getpid(),
}
UNIVERSE = {s:{"symbol":s,"effective_date":None,"source":"seed"} for s in UNIVERSE_SEED}
QUOTES = {}
BORROW = {}
EVENTS = []
ANALYTICS = {}
TRAIL = {}
HALTS = {}
NEWS = {}
HISTORY = {}
STATE_FILE = Path(os.environ.get("SNIPELAB_STATE_FILE","/tmp/snipelab_state.json"))
_LAST_SAVE = 0.0

def load_persistent_state():
    try:
        d=storage.load(("universe","quotes","analytics","borrow","history","events","halts"))
        STATE["restored_from_sqlite"]=bool(d)
        STATE["restored_at"]=utcnow().isoformat() if d else None
        if not d and STATE_FILE.exists():d=json.loads(STATE_FILE.read_text("utf-8"))
        UNIVERSE.update(d.get("universe") or {})
        QUOTES.update(d.get("quotes") or {})
        HALTS.update(d.get("halts") or {})
        ANALYTICS.update(d.get("analytics") or {})
        BORROW.update(d.get("borrow") or {})
        HISTORY.update(d.get("history") or {})
        EVENTS.extend((d.get("events") or [])[:100])
    except Exception as exc:
        STATE["persistence_error"]=f"load {type(exc).__name__}: {str(exc)[:100]}"

def save_persistent_state(force=False):
    global _LAST_SAVE
    now=time.time()
    if not force and now-_LAST_SAVE<60:return
    try:
        storage.save({"universe":UNIVERSE,"quotes":QUOTES,"analytics":ANALYTICS,"borrow":BORROW,"history":HISTORY,"events":EVENTS[:100],"halts":HALTS})
        _LAST_SAVE=now
        STATE["last_state_save"]=utcnow().isoformat(); STATE["persistence_error"]=None
    except Exception as exc:
        STATE["persistence_error"]=f"save {type(exc).__name__}: {str(exc)[:100]}"

def utcnow(): return datetime.now(timezone.utc)
def add_event(symbol, kind, text, data=None):
    if kind=="halt" and any(e.get("kind")=="halt" and e.get("symbol")==symbol and e.get("data")== (data or {}) for e in EVENTS):return
    EVENTS.insert(0,{"symbol":symbol,"kind":kind,"text":text,"at":utcnow().isoformat(),"data":data or {}})
    del EVENTS[100:]

async def heartbeat_loop():
    while True:
        STATE["status"]="running"; STATE["heartbeat"]=utcnow().isoformat(); STATE["heartbeat_count"]+=1
        await asyncio.sleep(10)

def apply_confirmed_splits():
    """Protect exchange-confirmed latest splits from stale calendar results."""
    changed=[]
    for sym,candidate in CONFIRMED_SPLITS.items():
        if candidate["effective_date"]>datetime.now(ZoneInfo("America/New_York")).date().isoformat():
            continue
        previous=UNIVERSE.get(sym) or {}
        old=previous.get("effective_date") or ""
        if old>candidate["effective_date"]:
            continue
        if old!=candidate["effective_date"]:
            HISTORY.pop(sym,None); ANALYTICS.pop(sym,None); TRAIL.pop(sym,None)
            changed.append(sym)
        if old!=candidate["effective_date"] or previous.get("source")!=candidate["source"]:
            UNIVERSE[sym]=dict(candidate)
    STATE["universe_count"]=len(UNIVERSE)
    return changed

async def fetch_direct_universe(client):
    await asyncio.sleep(0)
    merged={}
    errors=[]
    for url in SPLITS_URLS:
        try:
            r=await client.get(url,timeout=20); r.raise_for_status()
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        soup=BeautifulSoup(r.text,"html.parser")
        for tr in soup.select("table tbody tr"):
            tds=[td.get_text(" ",strip=True) for td in tr.select("td")]
            if len(tds)<5 or tds[3].lower()!="reverse": continue
            try: eff=datetime.strptime(tds[0],"%b %d, %Y").date()
            except Exception: continue
            if eff < date(2026,5,1) or eff > date(2026,12,30) or eff > utcnow().date(): continue
            sym=tds[1].upper().strip()
            if sym:
                candidate={"symbol":sym,"company":tds[2],"effective_date":eff.isoformat(),"ratio":tds[4],"source":"stockanalysis"}
                previous=merged.get(sym)
                if previous is None or candidate["effective_date"] > previous["effective_date"]:
                    merged[sym]=candidate
    if not merged: raise RuntimeError("empty direct split feed | "+" | ".join(errors))
    # Never replace a newer confirmed split with an older feed row.
    # When the latest split changes, invalidate calculations tied to the old date.
    for sym, candidate in merged.items():
        previous=UNIVERSE.get(sym) or {}
        old_date=previous.get("effective_date") or ""
        new_date=candidate["effective_date"]
        if old_date and old_date>new_date:
            continue
        if old_date!=new_date:
            HISTORY.pop(sym,None)
            ANALYTICS.pop(sym,None)
            TRAIL.pop(sym,None)
        UNIVERSE[sym]=candidate
    # Keep last known confirmed symbols when an upstream page is incomplete.
    apply_confirmed_splits()
    STATE["universe_count"]=len(UNIVERSE); STATE["last_universe_sync"]=utcnow().isoformat(); STATE["universe_error"]=None; STATE["universe_source"]="stockanalysis_plus_exchange_confirmed"
    return True

async def sync_universe(client):
    STATE["universe_attempts"]+=1
    apply_confirmed_splits()
    last_error=None
    try:
        if await fetch_direct_universe(client): return
    except Exception as exc:
        last_error=f"direct: {type(exc).__name__}"
    # Never fetch the legacy Qanas service: SnipeLab is fully independent.
    # Keep the last successfully synced universe when the public feed fails.
    # Render can be slow to wake up. Never leave the watcher empty while it retries.
    if not UNIVERSE:
        UNIVERSE.update({s:{"symbol":s,"effective_date":None,"source":"seed"} for s in UNIVERSE_SEED})
        STATE["universe_count"]=len(UNIVERSE)
    apply_confirmed_splits()
    STATE["universe_error"]=last_error or "StockAnalysis feed unavailable"; STATE["universe_source"]="exchange_confirmed_fallback" if STATE["last_universe_sync"] is None else STATE["universe_source"]

async def universe_loop():
    # Keep Northflank ingress healthy before any external scraping starts.
    await asyncio.sleep(3)
    headers={"User-Agent":"Mozilla/5.0 SnipeLab/2.0"}
    async with httpx.AsyncClient(follow_redirects=True,headers=headers) as client:
        while True:
            await sync_universe(client)
            await asyncio.sleep(120)

async def fetch_quote(client, sem, symbol):
    async with sem:
        # The 1m endpoint can be empty outside the session; daily bars are a
        # clearly labelled fallback, not a claim of live market data.
        for interval,window in (("1m","1d"),("1d","5d")):
            try:
                r=await client.get(YAHOO.format(symbol=symbol),params={"range":window,"interval":interval,"includePrePost":"true","events":"history"})
                r.raise_for_status()
                result=(r.json().get("chart",{}).get("result") or [None])[0]
                if not result:continue
                ts=result.get("timestamp") or []
                q=((result.get("indicators") or {}).get("quote") or [{}])[0]
                closes=q.get("close") or []
                valid=[(int(t),i,float(closes[i])) for i,t in enumerate(ts) if i<len(closes) and closes[i] is not None and float(closes[i])>0]
                if not valid:continue
                t,idx,p=max(valid,key=lambda z:z[0])
                # Daily fallback must use the last session only, not the whole range.
                if interval=="1d":
                    hi=float(q["high"][idx]) if q.get("high") and q["high"][idx] is not None else p
                    lo=float(q["low"][idx]) if q.get("low") and q["low"][idx] is not None else p
                else:
                    hi=max([float(v) for v in (q.get("high") or []) if v is not None and float(v)>0] or [p])
                    lo=min([float(v) for v in (q.get("low") or []) if v is not None and float(v)>0] or [p])
                return symbol,{"symbol":symbol,"price":p,"day_high":hi,"day_low":lo,
                    "market_timestamp":datetime.fromtimestamp(t,tz=timezone.utc).isoformat(),
                    "received_at":utcnow().isoformat(),"source":"yahoo_"+interval+("_prepost" if interval=="1m" else "_fallback")}
            except Exception:
                continue
        return symbol,None

async def delayed_market_start():
    await asyncio.sleep(5)
    await market_loop()

async def market_loop():
    # Scan small chunks so 255 symbols fit comfortably in the 256 MB sandbox.
    headers={"User-Agent":"Mozilla/5.0 QanasWatcher/0.3"}
    limits=httpx.Limits(max_connections=5,max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=8,follow_redirects=True,headers=headers,limits=limits) as client:
        while True:
            # Keep the scan order fixed while advancing the cursor.
            # Sorting by cache status can permanently skip some symbols.
            syms=sorted(UNIVERSE,key=lambda sym:(sym!="RETO",sym))
            if not syms:
                await asyncio.sleep(10); continue
            cursor=int(STATE["market_cursor"]) % len(syms)
            batch=syms[cursor:cursor+12]
            if len(batch)<12: batch += syms[:12-len(batch)]
            sem=asyncio.Semaphore(4)
            rows=await asyncio.gather(*(fetch_quote(client,sem,s) for s in batch))
            ok=0
            for s,row in rows:
                if row is not None: QUOTES[s]=row; ok+=1
            STATE["market_scan_count"]+=1
            STATE["last_market_scan"]=utcnow().isoformat()
            STATE["market_ok"]=ok; STATE["market_failed"]=len(batch)-ok
            STATE["market_total_cached"]=len(QUOTES)
            STATE["last_market_error"]=None if ok else "no quotes returned"
            nxt=(cursor+len(batch)) % len(syms)
            if nxt <= cursor: STATE["market_cycle"]+=1
            STATE["market_cursor"]=nxt
            save_persistent_state()
            await asyncio.sleep(3)

def readiness_state(meta,q,b,a):
    price=float(q["price"]); live_low=float(q.get("day_low") or price)
    hist=HISTORY.get(meta.get("symbol"),{})
    prior_low=hist.get("post_split_low") if hist.get("verified") else None
    new_low=prior_low is not None and live_low<float(prior_low)
    effective_low=live_low if new_low else (float(prior_low) if prior_low is not None else live_low)
    dist=((price/effective_low)-1)*100 if effective_low>0 else None
    sessions=0 if new_low else int(a.get("stability_sessions") or 0)
    market_day=str(q.get("market_timestamp") or "")[:10]
    last_day=a.get("last_market_day")
    if not new_low and prior_low is not None and market_day and last_day and market_day!=last_day:
        sessions=min(4,sessions+1)
    high=max(float(a.get("highest_since_split") or price),float(q.get("day_high") or price))
    half=hist["split_day_4h_high"]/2 if hist.get("verified") and hist.get("split_day_4h_high") is not None else None
    half_ok=bool(half is not None and effective_low<=half)
    av=b.get("available") if b else None
    price_ok=price>0; av_ok=av is not None and av<10000
    dist_ok=dist is not None and dist<=10; sess_ok=sessions>=4
    missing=[]; close=True
    if not half_ok: missing.append(f"يحقق شرط النصف <= {half:.4f}" if half else "حساب مستوى النصف"); close=False
    if new_low: missing.append("كون قاع جديد اليوم: يبدأ الثبات من 0/4"); close=False
    if not av_ok:
        missing.append("Available ينزل إلى أقل من 10K" if av is not None else "قراءة Available")
        close=close and av is not None and av<10000
    if not dist_ok:
        missing.append(f"يرجع أقرب للقاع: الآن {dist:.2f}% والهدف <=10%" if dist is not None else "حساب البعد عن القاع")
        close=close and dist is not None and dist<=20
    if not sess_ok and not new_low:
        missing.append(f"{max(0,4-sessions)} جلسة ثبات إضافية للوصول إلى 4/4")
        close=close and sessions>=2
    if not hist.get("verified"):missing.append("تاريخ القاع والقمة بعد التقسيم");close=False
    if hist.get("rsi_daily") is None or hist["rsi_daily"]>=30:missing.append("RSI اليومي أقل من 30");close=False
    if not price_ok: missing.append("تحديث السعر الحالي"); close=False
    full=price_ok and hist.get("verified") and hist.get("rsi_daily") is not None and hist["rsi_daily"]<30 and half_ok and not new_low and av_ok and dist_ok and sess_ok
    shortlist=full or (price_ok and hist.get("verified") and av is not None and 1<=len(missing)<=2)
    if av is None: ap=0
    elif av<10000: ap=50
    elif av<=20000: ap=0
    else: ap=0
    if dist is None: dp=0
    elif dist<=10: dp=30
    elif dist<=20: dp=30-15*((dist-10)/10)
    else: dp=0
    sp=20 if sessions>=4 else 15 if sessions==3 else 10 if sessions==2 else 5 if sessions==1 else 0
    pct=100.0 if full else round(min(99.0,ap+dp+sp),1)
    if not hist.get("verified") or av is None or hist.get("rsi_daily") is None:pct=None
    strengths=[]
    if half_ok: strengths.append("شرط النصف ✓")
    if av_ok: strengths.append(f"Available {int(av):,} ✓")
    if dist_ok: strengths.append(f"عن القاع {dist:.2f}% ✓")
    if sess_ok: strengths.append("ثبات 4/4 ✓")
    return {"full":full,"shortlist":shortlist,"readiness_pct":pct,"missing_count":len(missing),
        "missing":" + ".join(missing) if missing else "مكتمل ✓","strength":" | ".join(strengths),
        "new_low_today":new_low,"effective_low":effective_low,"effective_distance_pct":dist,
        "effective_sessions":sessions,"highest_since_split":high,"half_level":half,"half_reached":half_ok,
        "market_day":market_day}

def refresh_analytics():
    now=time.time()
    for sym,meta in UNIVERSE.items():
        q=QUOTES.get(sym); b=BORROW.get(sym); a=ANALYTICS.get(sym,{})
        if not q: continue
        price=float(q["price"]); eff=str(meta.get("effective_date") or "")
        active=bool(eff and eff<=utcnow().date().isoformat())
        trail=TRAIL.setdefault(sym,[]); trail.append((now,price)); trail[:]=[(t,p) for t,p in trail if now-t<=900]
        ignition=None
        old=[z for z in trail if 180<=now-z[0]<=480]
        if old:
            z=min(old,key=lambda z:abs((now-z[0])-300)); pct5=(price/z[1]-1)*100
            ignition={"pct":round(pct5,2),"minutes":round((now-z[0])/60,1),"fresh":3<=pct5<=14.99}
        if not active:
            ANALYTICS[sym]={"symbol":sym,"active":False,"effective_date":eff,"price":price,"ignition":ignition}; continue
        st=readiness_state(meta,q,b,a)
        hist=HISTORY.get(sym,{})
        full=st["full"]; was_ready=bool(a.get("ready"))
        ready_at=a.get("ready_at"); ready_price=a.get("ready_price")
        if full and not was_ready:
            ready_at=utcnow().isoformat(); ready_price=price
            ev=ready_event(sym,was_ready,full,price,b.get("available") if b else None)
            if ev:add_event(*ev)
        launched=bool(a.get("launched")); max_rise=a.get("max_rise_pct")
        if ready_price and ready_price>0:
            # Match Qanas: TOP follows the highest observed price after the first qualifying ready moment.
            hi=float(q.get("day_high") or price); rise=(hi/ready_price-1)*100
            max_rise=max(float(max_rise or 0),rise)
            if max_rise>=40 and not launched:
                launched=True; add_event(sym,"launched","Reached +40% after ready",{"rise_pct":round(max_rise,2)})
        if ignition and ignition["fresh"] and not (a.get("ignition") or {}).get("fresh"):
            add_event(sym,"ignition",f"Momentum +{ignition['pct']:.1f}%",ignition)
        ANALYTICS[sym]={"symbol":sym,"active":True,"effective_date":eff,"price":price,
            "post_split_low":hist.get("post_split_low"),"highest_since_split":hist.get("post_split_high"),
            "history_verified":bool(hist.get("verified")),"split_day_high":hist.get("split_day_high"),"split_day_4h_high":hist.get("split_day_4h_high"),"split_day_4h_status":hist.get("split_day_4h_status"),"split_day_open":hist.get("split_day_open"),"rsi_daily":hist.get("rsi_daily"),
            "top_10_gain_pct":hist.get("top_10_gain_pct"),"top_10_low":hist.get("top_10_low"),"top_10_high":hist.get("top_10_high"),
            "top_10_low_date":hist.get("top_10_low_date"),"top_10_high_date":hist.get("top_10_high_date"),"top_10_verified":bool(hist.get("top_10_verified")),
            "half_level":st["half_level"],"half_reached":st["half_reached"],
            "distance_from_low_pct":round((price/hist["post_split_low"]-1)*100,2) if hist.get("post_split_low") else None,
            "stability_sessions":st["effective_sessions"],"effective_low":st["effective_low"],
            "effective_distance_pct":round(st["effective_distance_pct"],2) if st["effective_distance_pct"] is not None else None,
            "effective_sessions":st["effective_sessions"],"new_low_today":st["new_low_today"],
            "available":b.get("available") if b else None,"ctb":b.get("ctb") if b else None,"rebate":b.get("rebate") if b else None,
            "readiness_pct":st["readiness_pct"],"score":st["readiness_pct"],"ready":full,
            "near_ready":st["shortlist"] and not full,"shortlist":st["shortlist"],
            "ready_candidate":(b is not None and b.get("available") is not None and b.get("available")<10000 and hist.get("verified") and st["effective_distance_pct"] is not None and st["effective_distance_pct"]<=10 and st["effective_sessions"]>=4 and not st["new_low_today"] ),
            "missing_count":st["missing_count"],"missing":st["missing"],"strength":st["strength"],
            "ready_at":ready_at,"ready_price":ready_price,"launched":launched,
            "max_rise_pct":round(max_rise,2) if max_rise is not None else None,"rise_pct":round(max_rise,2) if max_rise is not None else None,
            "ignition":ignition,"last_market_day":st["market_day"]}

async def analytics_loop():
    await asyncio.sleep(40)
    while True:
        refresh_analytics(); STATE["analytics_count"]=len(ANALYTICS); STATE["last_analytics"]=utcnow().isoformat(); save_persistent_state()
        await asyncio.sleep(10)

def download_ibkr():
    ftp=ftplib.FTP(timeout=20)
    try:
        ftp.connect(FTP_HOST,21); ftp.login(FTP_USER,FTP_PASSWORD); data=io.BytesIO(); ftp.retrbinary("RETR "+FTP_FILE,data.write)
        return data.getvalue().decode("utf-8",errors="replace")
    finally:
        try: ftp.quit()
        except Exception:
            try: ftp.close()
            except Exception: pass

def parse_ibkr(text):
    out={}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):continue
        f=[x.strip().strip('"') for x in re.split(r"[|\t]",raw.strip())]
        if len(f)<8 or f[1].upper().strip()!="USD":continue
        sym=f[0].upper().strip()
        try: rebate=float(f[5]); fee=float(f[6]); available=float(f[7].replace(",",""))
        except Exception:continue
        if sym and available>=0:out[sym]={"available":available,"ctb":fee,"rebate":rebate,"source":"IBKR public FTP usa.txt"}
    return out

async def delayed_borrow_start():
    # Let universe and price workers settle first on the 256 MB sandbox.
    await asyncio.sleep(10)
    await borrow_loop()

async def borrow_loop():
    while True:
        try:
            text=await asyncio.wait_for(asyncio.to_thread(download_ibkr),timeout=30)
            rows=parse_ibkr(text); now=utcnow().isoformat(); changed=0
            for sym in list(UNIVERSE):
                new=rows.get(sym)
                if not new:continue
                new={**new,"received_at":now}; old=BORROW.get(sym)
                for ev in borrow_events(sym,old,new):
                    changed+=1; add_event(*ev)
                BORROW[sym]=new
            STATE["borrow_scan_count"]+=1; STATE["last_borrow_scan"]=now; STATE["borrow_ok"]=sum(1 for s in UNIVERSE if s in rows)
            STATE["borrow_missing"]=max(0,len(UNIVERSE)-STATE["borrow_ok"]); STATE["last_borrow_error"]=None
        except Exception as exc: STATE["last_borrow_error"]=f"{type(exc).__name__}: {str(exc)[:120]}"
        save_persistent_state()
        await asyncio.sleep(300)

async def halt_loop():
    await asyncio.sleep(60)
    url="https://www.nasdaqtrader.com/dynamic/symdir/tradinghalts.txt"
    async with httpx.AsyncClient(timeout=8,follow_redirects=True) as client:
        while True:
            try:
                r=await client.get(url); r.raise_for_status(); fresh={}
                for line in r.text.splitlines():
                    p=line.split("|")
                    if len(p)>=6 and p[0] and p[0]!="Halt Date":
                        sym=p[2].upper().strip()
                        if sym in UNIVERSE:fresh[sym]={"symbol":sym,"reason":p[5],"halt_time":p[1],"halt_date":p[0]}
                for sym,row in fresh.items():
                    key=row["halt_date"]+" "+row["halt_time"]+" "+row["reason"]
                    if HALTS.get(sym,{}).get("_key")!=key:add_event(sym,"halt","HALT "+row["reason"],row)
                    row["_key"]=key
                HALTS.clear(); HALTS.update(fresh); STATE["last_halt_scan"]=utcnow().isoformat(); STATE["halt_error"]=None
            except Exception as exc:STATE["halt_error"]=f"{type(exc).__name__}: {str(exc)[:100]}"
            save_persistent_state()
            await asyncio.sleep(120)

async def legacy_news_loop_disabled():
    # Reuse the proven Qanas SEC layer without putting news into readiness scoring.
    await asyncio.sleep(210)
    while True:
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=True) as client:
                raise RuntimeError("Legacy Qanas news integration disabled for repository isolation")
            fresh={}
            for tone_name in ("positive","negative"):
                for x in data.get(tone_name,[]) or []:
                    sym=str(x.get("symbol") or "").upper()
                    if sym in UNIVERSE:
                        item={**x,"tone":tone_name}; fresh.setdefault(sym,[]).append(item)
                        key=str(x.get("published_at"))+"|"+str(x.get("title"))
                        seen={str(z.get("published_at"))+"|"+str(z.get("title")) for z in NEWS.get(sym,[])}
                        if tone_name=="positive" and key not in seen:add_event(sym,"positive_news","Positive news",{"title":x.get("title"),"source":x.get("source")})
            NEWS.clear(); NEWS.update(fresh); STATE["last_news_scan"]=utcnow().isoformat(); STATE["news_error"]=None
        except Exception as exc:STATE["news_error"]=f"{type(exc).__name__}: {str(exc)[:100]}"
        await asyncio.sleep(600)

@app.on_event("startup")
async def startup():
    load_persistent_state()
    asyncio.create_task(heartbeat_loop()); asyncio.create_task(universe_loop()); asyncio.create_task(delayed_market_start()); asyncio.create_task(delayed_borrow_start()); asyncio.create_task(analytics_loop()); asyncio.create_task(halt_loop()); asyncio.create_task(historical_worker(UNIVERSE,HISTORY,YAHOO,save_persistent_state))

@app.get("/")
async def root():
    return {"service":"snipelab-engine","message":"SnipeLab Engine is alive","version":"0.6.0",**STATE,
        "prices_ready":len(QUOTES),"borrow_ready":len(BORROW),"events":len(EVENTS),
        "uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),"storage":storage.status(),
        "endpoints":["/dashboard","/health","/universe","/prices","/borrow","/snapshot","/signals","/ready","/zero-short","/momentum","/top","/halts","/news","/events"]}

DASHBOARD = (Path(__file__).parent / "dashboard.html").read_text("utf-8")

@app.get("/dashboard",response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)

@app.get("/health")
async def health():
    last=STATE["heartbeat"]; age=(utcnow()-datetime.fromisoformat(last)).total_seconds() if last else None
    now=utcnow()
    workers={"market":worker_health(now,STATE.get("last_market_scan"),180),"borrow":worker_health(now,STATE.get("last_borrow_scan"),420),"history":worker_health(now,max((v.get("attempted_at","") for v in HISTORY.values()),default=None),900),"analytics":worker_health(now,STATE.get("last_analytics"),120),"halt":worker_health(now,STATE.get("last_halt_scan"),240)}
    return {"ok":bool(last) and age<30,"heartbeat_age_seconds":age,"workers":workers,"storage":storage.status(),"quotes_cached":len(QUOTES),"history_cached":len(HISTORY),"history_verified":sum(bool(HISTORY.get(sym,{}).get("verified")) for sym in UNIVERSE),"history_failed":sum(bool(HISTORY.get(sym,{}).get("error")) for sym in UNIVERSE),"history_last_attempt":max((v.get("attempted_at","") for v in HISTORY.values()),default=None),**STATE}

@app.get("/api/storage-check")
async def storage_check():
    """Read-only proof of snapshots and startup recovery; does not restart service."""
    try:
        snapshots=storage.snapshot_info()
        error=None
    except Exception as exc:
        snapshots=None; error=f"{type(exc).__name__}: {str(exc)[:120]}"
    return {"booted_at":BOOTED_AT.isoformat(),"uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),
        "restored_from_sqlite":STATE.get("restored_from_sqlite",False),
        "restored_at":STATE.get("restored_at"),"last_state_save":STATE.get("last_state_save"),
        "storage":storage.status(),"snapshots":snapshots,"storage_error":error,
        "counts":{"universe":len(UNIVERSE),"quotes":len(QUOTES),"history":len(HISTORY),"borrow":len(BORROW),"events":len(EVENTS)}}

@app.get("/universe")
async def universe():
    return {"count":len(UNIVERSE),"last_sync":STATE["last_universe_sync"],"source":STATE["universe_source"],"attempts":STATE["universe_attempts"],"error":STATE["universe_error"],"symbols":sorted(UNIVERSE)}

@app.get("/prices")
async def prices():
    return {"source":"yahoo_1m_prepost","last_market_scan":STATE["last_market_scan"],"market_scan_count":STATE["market_scan_count"],
        "ok":STATE["market_ok"],"failed":STATE["market_failed"],"cursor":STATE["market_cursor"],"cycle":STATE["market_cycle"],"universe_count":len(UNIVERSE),"count":len(QUOTES),"quotes":QUOTES}

@app.get("/borrow")
async def borrow():
    return {"source":"IBKR public FTP usa.txt","last_borrow_scan":STATE["last_borrow_scan"],"borrow_scan_count":STATE["borrow_scan_count"],
        "ok":STATE["borrow_ok"],"missing":STATE["borrow_missing"],"error":STATE["last_borrow_error"],"count":len(BORROW),"rows":BORROW}

@app.get("/snapshot")
async def snapshot():
    rows={}
    for sym,meta in UNIVERSE.items():
        rows[sym]={"symbol":sym,"effective_date":meta.get("effective_date"),"price":QUOTES.get(sym),"borrow":BORROW.get(sym),"signal":ANALYTICS.get(sym)}
    return {"generated_at":utcnow().isoformat(),"count":len(rows),"rows":rows}

@app.get("/signals")
async def signals():
    rows=sorted(ANALYTICS.values(),key=lambda x:(x.get("score") or 0),reverse=True)
    return {"generated_at":utcnow().isoformat(),"count":len(rows),"rows":rows}

@app.get("/ready")
async def ready():
    rows=[x for x in ANALYTICS.values() if x.get("ready")]
    rows.sort(key=lambda x:(x.get("available") is None,x.get("available") or 10**18,-(x.get("score") or 0)))
    return {"count":len(rows),"rows":rows}

@app.get("/zero-short")
async def zero_short():
    rows=[x for x in ANALYTICS.values() if x.get("available") is not None and float(x["available"])==0]
    return {"count":len(rows),"rows":rows}

@app.get("/momentum")
async def momentum():
    rows=[x for x in ANALYTICS.values() if (x.get("ignition") or {}).get("fresh")]
    rows.sort(key=lambda x:(x.get("ignition") or {}).get("pct",0),reverse=True)
    return {"count":len(rows),"rows":rows}

@app.get("/top")
async def top():
    rows=[x for x in ANALYTICS.values() if x.get("top_10_verified") and (x.get("top_10_gain_pct") or 0)>=40]
    rows.sort(key=lambda x:x.get("top_10_gain_pct") or 0,reverse=True)
    return {"count":len(rows),"rows":rows}

@app.get("/api/dashboard")
async def dashboard_data():
    rows={}
    for sym,meta in UNIVERSE.items():
        h=HISTORY.get(sym,{})
        signal={**(ANALYTICS.get(sym) or {}),
            "history_verified":bool(h.get("verified")),
            "post_split_low":h.get("post_split_low"),
            "highest_since_split":h.get("post_split_high"),
            "split_day_high":h.get("split_day_high"),
            "split_day_4h_high":h.get("split_day_4h_high"),
            "split_day_4h_status":h.get("split_day_4h_status"),
            "split_day_open":h.get("split_day_open"),
            "post_split_high_date":h.get("post_split_high_date"),
            "post_split_low_date":h.get("post_split_low_date"),
            "quality_warnings":h.get("quality_warnings",[]),
            "extended_history_complete":h.get("extended_history_complete",False),
            "rsi_daily":h.get("rsi_daily"),
            "top_10_gain_pct":h.get("top_10_gain_pct"),
            "top_10_low":h.get("top_10_low"),
            "top_10_high":h.get("top_10_high"),
            "top_10_low_date":h.get("top_10_low_date"),
            "top_10_high_date":h.get("top_10_high_date"),
            "top_10_verified":bool(h.get("top_10_verified")),
            "history_status":h.get("error") or ("verified" if h.get("verified") else "pending")}
        rows[sym]={"symbol":sym,"effective_date":meta.get("effective_date"),"price":QUOTES.get(sym),"borrow":BORROW.get(sym),"signal":signal}
    return {"server_time":utcnow().isoformat(),"storage":storage.status(),"history_count":sum(bool(HISTORY.get(sym,{}).get("verified")) for sym in UNIVERSE),"history_pending":sum(1 for sym in UNIVERSE if not HISTORY.get(sym,{}).get("verified")),"health":{"ok":STATE.get("status")=="running","heartbeat":STATE.get("heartbeat"),"universe_count":len(UNIVERSE),"price_count":len(QUOTES),"borrow_count":len(BORROW),"analytics_count":len(ANALYTICS)},"rows":rows,"events":EVENTS[:40],"halts":HALTS,"news":NEWS}

@app.get("/halts")
async def halts():
    return {"last_scan":STATE["last_halt_scan"],"error":STATE["halt_error"],"count":len(HALTS),"rows":HALTS}

@app.get("/news")
async def news():
    return {"last_scan":STATE["last_news_scan"],"error":STATE["news_error"],"count":len(NEWS),"rows":NEWS}

@app.get("/events")
async def events():
    return {"count":len(EVENTS),"events":EVENTS}

@app.get("/api/news/{symbol}")
async def ticker_news(symbol: str):
    symbol = re.sub(r"[^A-Z0-9.-]", "", symbol.upper())[:12]
    return {"symbol":symbol,"items":NEWS.get(symbol,[]),"message":"مصدر الأخبار المستقل وتلخيص الذكاء الاصطناعي قيد الربط؛ لا توجد قراءة مصنفة موثوقة حالياً."}

@app.get("/api/history-coverage")
async def history_coverage():
    """Every discovered split and the four requested metrics, with provenance."""
    rows=[]
    for sym,meta in sorted(UNIVERSE.items()):
        h=HISTORY.get(sym) or {}
        if not meta.get("effective_date"):continue
        valid=h.get("verified") and h.get("effective_date")==meta["effective_date"]
        fields={"split_day_open":h.get("split_day_open") if valid else None,
                "split_day_4h_high":h.get("split_day_4h_high") if valid else None,
                "post_split_high":h.get("post_split_high") if valid else None,
                "post_split_high_date":h.get("post_split_high_date") if valid else None,
                "post_split_low":h.get("post_split_low") if valid else None,
                "post_split_low_date":h.get("post_split_low_date") if valid else None}
        missing=[key for key in ("split_day_open","split_day_4h_high","post_split_high","post_split_low") if fields[key] is None]
        warnings=h.get("quality_warnings",[])
        if not valid or missing:audit_status="pending"
        elif any(w in warnings for w in ("invalid_extrema","rolling_extrema_inconsistent")):audit_status="inconsistent"
        elif warnings or not h.get("extended_history_complete",False):audit_status="needs_validation"
        else:audit_status="verified"
        rows.append({"symbol":sym,"audit_status":audit_status,"effective_date":meta["effective_date"],
            "ratio":meta.get("ratio"),"split_source":meta.get("source"),
            **fields,"complete":not missing,"missing":missing,
            "quality_warnings":h.get("quality_warnings",[]),
            "extended_history_complete":h.get("extended_history_complete",False),
            "split_day_4h_status":h.get("split_day_4h_status"),
            "last_attempt":h.get("attempted_at"),"error":h.get("error")})
    return {"generated_at":utcnow().isoformat(),"total":len(rows),
            "four_fields_complete":sum(x["complete"] for x in rows),
            "pending":sum(not x["complete"] for x in rows),
            "quality_flagged":sum(bool(x["quality_warnings"]) for x in rows),
            "needs_validation":sum(x["audit_status"]=="needs_validation" for x in rows),
            "inconsistent":sum(x["audit_status"]=="inconsistent" for x in rows),
            "audited":sum(x["audit_status"]=="verified" for x in rows),
            "pending_never_attempted":sum(x["audit_status"]=="pending" and not x["last_attempt"] for x in rows),
            "pending_attempted":sum(x["audit_status"]=="pending" and bool(x["last_attempt"]) for x in rows),
            "warning_counts":{w:sum(w in x["quality_warnings"] for x in rows)
                              for w in sorted({w for x in rows for w in x["quality_warnings"]})},
            "rows":rows}

@app.get("/api/diagnostics/{symbol}")
async def ticker_diagnostics(symbol: str):
    symbol=re.sub(r"[^A-Z0-9.-]","",symbol.upper())[:12]
    return {"symbol":symbol,"in_split_universe":symbol in UNIVERSE,"server_time":utcnow().isoformat(),"last_market_scan":STATE["last_market_scan"],"last_borrow_scan":STATE["last_borrow_scan"],
            "split":UNIVERSE.get(symbol),"history":HISTORY.get(symbol),
            "quote":QUOTES.get(symbol),"borrow":BORROW.get(symbol),
            "signal":ANALYTICS.get(symbol),
            "note":"A rally alone does not establish a qualifying reverse split."}
