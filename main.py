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
import websockets
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
BORROW_HISTORY = {}
RADAR_MEMORY = {}
SHORT_ANALYSIS = {}
STATE_FILE = Path(os.environ.get("SNIPELAB_STATE_FILE","/tmp/snipelab_state.json"))
_LAST_SAVE = 0.0

def load_persistent_state():
    try:
        d=storage.load(("universe","quotes","analytics","borrow","history","events","halts","borrow_history","radar_memory"))
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
        BORROW_HISTORY.update(d.get("borrow_history") or {})
        RADAR_MEMORY.update(d.get("radar_memory") or {})
    except Exception as exc:
        STATE["persistence_error"]=f"load {type(exc).__name__}: {str(exc)[:100]}"

def save_persistent_state(force=False):
    global _LAST_SAVE
    now=time.time()
    if not force and now-_LAST_SAVE<60:return
    try:
        storage.save({"universe":UNIVERSE,"quotes":QUOTES,"analytics":ANALYTICS,"borrow":BORROW,"history":HISTORY,"events":EVENTS[:100],"halts":HALTS,"borrow_history":BORROW_HISTORY,"radar_memory":RADAR_MEMORY})
        _LAST_SAVE=now
        STATE["last_state_save"]=utcnow().isoformat(); STATE["persistence_error"]=None
    except Exception as exc:
        STATE["persistence_error"]=f"save {type(exc).__name__}: {str(exc)[:100]}"

def utcnow(): return datetime.now(timezone.utc)
def add_event(symbol, kind, text, data=None):
    data=data or {}
    # Event Center dedupe: one logical alert per symbol/event/session. Repeated
    # worker scans may update market data, but must not spam the same alert.
    session=str(data.get("market_day") or data.get("halt_date") or data.get("at") or utcnow().date().isoformat())[:10]
    if kind=="halt": dedupe=(symbol,kind,str(data.get("halt_date") or ""),str(data.get("halt_time") or ""),str(data.get("reason") or ""))
    elif kind in {"price_25","available_10k","available_zero","ready","launched","ignition"}: dedupe=(symbol,kind,session)
    else: dedupe=(symbol,kind,session,text)
    for e in EVENTS:
        if tuple(e.get("_dedupe") or ())==dedupe:return
        # Backward-compatible protection for restored events created before _dedupe existed.
        if e.get("symbol")==symbol and e.get("kind")==kind:
            ed=e.get("data") or {};es=str(ed.get("market_day") or ed.get("halt_date") or e.get("at") or "")[:10]
            if kind=="halt" and ed==data:return
            if kind in {"price_25","available_10k","available_zero","ready","launched","ignition"} and es==session:return
    EVENTS.insert(0,{"symbol":symbol,"kind":kind,"text":text,"at":utcnow().isoformat(),"data":data,"_dedupe":list(dedupe)})
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
                # Only a verified chart reference is used for the +25% alert.
                meta=result.get("meta") or {}
                reference=meta.get("chartPreviousClose") or meta.get("previousClose")
                if interval=="1d" and len(valid)>=2:
                    earlier=[z for z in valid if z[0]<t]
                    if earlier: reference=max(earlier,key=lambda z:z[0])[2]
                try: reference=float(reference) if reference is not None else None
                except (TypeError,ValueError): reference=None
                if reference is not None and reference<=0: reference=None
                return symbol,{"symbol":symbol,"price":p,"day_high":hi,"day_low":lo,
                    "previous_close":reference,
                    "market_timestamp":datetime.fromtimestamp(t,tz=timezone.utc).isoformat(),
                    "received_at":utcnow().isoformat(),"source":"yahoo_"+interval+("_prepost" if interval=="1m" else "_fallback")}
            except Exception:
                continue
        return symbol,None

async def live_daily_rsi_loop():
    """Refresh today's Daily RSI(14) for every ticker, independent of split history."""
    await asyncio.sleep(12)
    headers={"User-Agent":"Mozilla/5.0 SnipeLab/0.7"}
    limits=httpx.Limits(max_connections=5,max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=10,follow_redirects=True,headers=headers,limits=limits) as client:
        while True:
            syms=sorted(UNIVERSE)
            sem=asyncio.Semaphore(4)
            async def one(sym):
                async with sem:
                    try:
                        r=await client.get(YAHOO.format(symbol=sym),params={"range":"2y","interval":"1d","includePrePost":"false","events":"history"})
                        r.raise_for_status()
                        result=(r.json().get("chart",{}).get("result") or [None])[0]
                        if not result:return
                        indicators=result.get("indicators") or {}
                        q=(indicators.get("quote") or [{}])[0]
                        adjusted=(indicators.get("adjclose") or [{}])[0].get("adjclose") or []
                        raw=q.get("close") or []
                        closes=[]
                        for i,x in enumerate(raw):
                            try:
                                a=adjusted[i] if i<len(adjusted) else None
                                v=float(a) if a is not None and float(a)>0 else float(x)
                                if v>0:closes.append(v)
                            except (TypeError,ValueError):continue
                        if len(closes)<15:return
                        changes=[closes[i]-closes[i-1] for i in range(1,len(closes))]
                        gains=[max(x,0.0) for x in changes];losses=[max(-x,0.0) for x in changes]
                        g=sum(gains[:14])/14.0;l=sum(losses[:14])/14.0
                        for i in range(14,len(changes)):
                            g=((g*13.0)+gains[i])/14.0;l=((l*13.0)+losses[i])/14.0
                        value=100.0 if l==0 else 100.0-(100.0/(1.0+g/l))
                        h=HISTORY.setdefault(sym,{})
                        h["rsi_daily"]=round(value,2);h["rsi_daily_live"]=round(value,2)
                        h["rsi_method"]="Wilder 14 / Yahoo split-adjusted 1d current candle"
                        h["rsi_live_updated_at"]=utcnow().isoformat()
                    except Exception:
                        return
            for pos in range(0,len(syms),20):
                await asyncio.gather(*(one(s) for s in syms[pos:pos+20]))
                await asyncio.sleep(1)
            save_persistent_state()
            await asyncio.sleep(120)

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
                if row is not None:
                    prior=QUOTES.get(s)
                    # An alert requires a same-session crossing, not an old
                    # cached price or the first observation after a restart.
                    if prior and row.get("previous_close") and prior.get("previous_close"):
                        market_day=str(row.get("market_timestamp") or "")[:10]
                        prior_day=str(prior.get("market_timestamp") or "")[:10]
                        old_pct=(float(prior["price"])/float(prior["previous_close"])-1)*100
                        new_pct=(float(row["price"])/float(row["previous_close"])-1)*100
                        if market_day and market_day==prior_day and old_pct<25<=new_pct:
                            add_event(s,"price_25",f"ارتفع +{new_pct:.1f}%",{"rise_pct":round(new_pct,2),"market_day":market_day})
                    # Never replace a newer cached market quote with an older
                    # daily fallback or delayed provider response.
                    if prior and prior.get("market_timestamp") and row.get("market_timestamp") and row["market_timestamp"]<prior["market_timestamp"]:
                        continue
                    QUOTES[s]=row; ok+=1
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

def wilder_rsi_live_from_history(symbol):
    """Return the latest independently refreshed Daily RSI(14).

    The RSI worker calculates Wilder RSI from split-adjusted Yahoo daily closes,
    including today's in-progress daily candle. Do not overlay the raw quote here:
    after a reverse split raw quote and adjusted historical closes are on different
    price scales and corrupt the RSI.
    """
    hist=HISTORY.get(symbol) or {}
    return hist.get("rsi_daily_live",hist.get("rsi_daily"))

def readiness_state(meta,q,b,a):
    """Four weighted factors plus mandatory 2-session post-low stability gate."""
    price=float(q["price"])
    hist=HISTORY.get(meta.get("symbol"),{})
    verified=bool(hist.get("verified"))
    prior_low=hist.get("post_split_low") if verified else None
    day_low=float(q.get("day_low") or price)
    new_low=prior_low is not None and day_low<float(prior_low)
    effective_low=min(float(prior_low),day_low) if prior_low is not None else None
    dist=((price/effective_low)-1)*100 if effective_low and effective_low>0 else None
    sessions=0 if new_low else int(hist.get("stability_sessions") or 0)
    # The reference is the highest high across ALL sessions since the
    # reverse split, not just the split day. The touch must occur AFTER
    # that peak: a low recorded before a later high is not proof.
    post_high=hist.get("post_split_high")
    high_date=str(hist.get("post_split_high_date") or "")
    low_date=str(hist.get("post_split_low_date") or "")
    half=float(post_high)/2 if post_high is not None and float(post_high)>0 else None
    historical_touch=bool(verified and high_date and low_date and
                          low_date>=high_date and prior_low is not None and
                          half is not None and float(prior_low)<=half)
    # A new low in today's quote can confirm a touch after a historical peak.
    quote_day=str(q.get("market_timestamp") or "")[:10]
    live_touch=bool(verified and high_date and quote_day>=high_date and
                    half is not None and day_low<=half)
    half_ok=historical_touch or live_touch
    half_rule_current=verified and half is not None and bool(high_date)
    raw_av=b.get("available") if b else None
    try:
        av=float(str(raw_av).replace(",","")) if raw_av is not None else None
        if av is not None and (not 0<=av<float("inf")):av=None
    except (TypeError,ValueError):av=None
    raw_rsi=wilder_rsi_live_from_history(meta.get("symbol"))
    try:
        rsi=float(raw_rsi) if raw_rsi is not None else None
        if rsi is not None and not 0<=rsi<=100:rsi=None
    except (TypeError,ValueError):rsi=None
    av_ok=av is not None and av<15000
    rsi_ok=rsi is not None and rsi<=35
    dist_ok=dist is not None and 0<=dist<=25
    # Weights: borrow 55, daily RSI 20, highest post-split peak half touch 20, low distance 5.
    # Binary score: each of the four conditions earns its entire weight
    # when satisfied, otherwise zero. Stability gates the Ready label only.
    ap=55 if av_ok else 0
    rp=20 if rsi_ok else 0
    dp=5 if dist_ok else 0
    hp=20 if half_ok else 0
    # A missing source is not a failed condition or a zero Available reading.
    complete=verified and half_rule_current and av is not None and rsi is not None and dist is not None
    stable_ok=verified and sessions>=2 and not new_low
    full=bool(complete and av_ok and rsi_ok and half_ok and dist_ok and stable_ok)
    conditions=[("Available أقل من 15K",av_ok,av is not None),
                ("RSI اليومي 35 أو أقل",rsi_ok,rsi is not None),
                ("لمس نصف أعلى قمة بعد التقسيم",half_ok,verified and half_rule_current),
                ("يبعد عن القاع 25% أو أقل",dist_ok,verified and dist is not None),
                ("ثبات جلستين فوق القاع دون كسره",stable_ok,verified)]
    missing=[name for name,ok,known in conditions if not ok and known]
    missing.extend(name+" (بيانات ناقصة)" for name,ok,known in conditions if not known)
    met=sum(bool(ok) for _,ok,known in conditions if known)
    # Near-ready requires 3/4 market factors and at least one stable
    # completed session. A fresh low or zero stability cannot be ready.
    market_met=sum((av_ok,rsi_ok,half_ok,dist_ok))
    shortlist=bool(complete and not full and not new_low and sessions>=1 and market_met>=3)
    pct=round(ap+rp+dp+hp,2) if complete else None
    strengths=[name+" ✓" for name,ok,known in conditions if ok and known]
    return {"full":full,"shortlist":shortlist,"readiness_pct":pct,
        "missing_count":len(missing),"missing":" + ".join(missing) if missing else "مكتمل ✓",
        "strength":" | ".join(strengths),"new_low_today":new_low,
        "effective_low":effective_low,"effective_distance_pct":dist,
        "effective_sessions":sessions,"highest_since_split":hist.get("post_split_high"),
        "half_level":half,"half_reached":half_ok,"split_half_reached":half_ok,
        "readiness_rule_version":10,"score_breakdown":{"available":ap,"rsi":rp,"distance":dp,"half":hp},
        "market_day":str(q.get("market_timestamp") or "")[:10]}

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
        if hist.get("verified") and b and b.get("available") is not None:
            try:
                av=float(b["available"]);rsi=float(wilder_rsi_live_from_history(sym))
                dist=float(st["effective_distance_pct"]);sessions=int(st["effective_sessions"])
                missing=[name for name,ok in (("الشورت",av<15000),("RSI",rsi<=35),
                    ("نصف القمة",st["half_reached"] is True),("القاع",dist<=25),
                    ("الثبات",sessions>=2)) if not ok]
                stage="ready" if not missing else "radar" if len(missing)<=2 and av<=20000 and rsi<=40 and dist<=35 else "watch"
                memory=RADAR_MEMORY.setdefault(sym,{"timeline":[],"first_radar":None,"first_ready":None})
                prev=memory["timeline"][-1] if memory["timeline"] else None
                if not prev or prev["stage"]!=stage or prev["missing"]!=missing:
                    memory["timeline"].append({"at":utcnow().isoformat(),"stage":stage,"missing":missing,
                        "price":price,"available":av,"rsi":rsi,"distance_pct":dist,"sessions":sessions})
                    memory["timeline"]=memory["timeline"][-80:]
                for name,active in (("radar",stage=="radar"),("ready",stage=="ready")):
                    key="first_"+name
                    if active and not memory[key]:
                        memory[key]={"at":utcnow().isoformat(),"price":price,"high_observed":price,"low_observed":price,"last_observed":price}
                    if memory[key]:
                        rec=memory[key];rec["high_observed"]=max(rec["high_observed"],price)
                        rec["low_observed"]=min(rec["low_observed"],price);rec["last_observed"]=price
                        rec["last_at"]=utcnow().isoformat()
            except (TypeError,ValueError,KeyError,OverflowError):
                pass
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
            "history_verified":bool(hist.get("verified")),"split_day_high":hist.get("split_day_high"),"split_day_4h_high":hist.get("split_day_4h_high"),"split_day_4h_status":hist.get("split_day_4h_status"),"split_day_open":hist.get("split_day_open"),"rsi_daily":wilder_rsi_live_from_history(sym),
            "top_10_gain_pct":hist.get("top_10_gain_pct"),"top_10_low":hist.get("top_10_low"),"top_10_high":hist.get("top_10_high"),
            "top_10_low_date":hist.get("top_10_low_date"),"top_10_high_date":hist.get("top_10_high_date"),"top_10_verified":bool(hist.get("top_10_verified")),
            "top_10_sessions_since_peak":hist.get("top_10_sessions_since_peak"),
            "top_calculator_version":hist.get("top_calculator_version",0),
            "top_10_source":hist.get("top_10_source"),
            "top_10_provisional":bool(hist.get("top_10_provisional")),
            "half_level":st["half_level"],"half_reached":st["half_reached"],
            "split_half_reached":st["split_half_reached"],
            "readiness_rule_version":st["readiness_rule_version"],
            "score_breakdown":st["score_breakdown"],
            "half_reference_high":hist.get("half_reference_high") or (hist.get("post_split_high") if hist.get("post_split_high_date") and hist.get("post_split_low_date") and hist["post_split_high_date"]<hist["post_split_low_date"] else None),
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

async def fetch_short_analysis(client, symbol):
    """Estimate post-split average short-sale price from FINRA flow + Yahoo daily OHLC.

    This is deliberately separate from IBKR Available/CTB/Rebate. FINRA daily
    short-sale volume is transaction flow, not open short interest, so the
    result is labelled an estimate rather than an actual open-position cost basis.
    """
    meta=UNIVERSE.get(symbol) or {}
    effective=meta.get("effective_date")
    out={"symbol":symbol,"estimated_short_avg_price":None,
         "short_target_drop_pct":None,"short_volume_used":None,
         "short_days_used":0,"method":"FINRA short volume × Yahoo daily typical price",
         "source":"FINRA Reg SHO + Yahoo 1d","effective_date":effective,
         "updated_at":utcnow().isoformat(),"error":None}
    if not effective:
        out["error"]="missing_effective_date"; return out
    try:
        # Public FINRA Reg SHO daily flow. One symbol request returns all
        # reporting facilities; aggregate facilities by trade date.
        payload={"limit":5000,
          "fields":["tradeReportDate","securitiesInformationProcessorSymbolIdentifier","shortParQuantity"],
          "compareFilters":[{"compareType":"equal",
            "fieldName":"securitiesInformationProcessorSymbolIdentifier","fieldValue":symbol}]}
        fr=await client.post("https://api.finra.org/data/group/otcMarket/name/regShoDaily",
            json=payload,headers={"Accept":"application/json"},timeout=15)
        if fr.status_code==204:
            out["error"]="finra_no_rows"; return out
        fr.raise_for_status()
        daily_short={}
        for row in fr.json():
            day=str(row.get("tradeReportDate") or "")[:10]
            if day<effective:continue
            try:v=float(row.get("shortParQuantity") or 0)
            except (TypeError,ValueError):continue
            if v>0:daily_short[day]=daily_short.get(day,0.0)+v
        if not daily_short:
            out["error"]="finra_no_post_split_short_volume"; return out

        start=int(datetime.combine(date.fromisoformat(effective),datetime.min.time(),timezone.utc).timestamp())-86400
        yr=await client.get(YAHOO.format(symbol=symbol),params={
            "period1":start,"period2":int(time.time())+86400,"interval":"1d","events":"history"},timeout=15)
        yr.raise_for_status()
        data=(yr.json().get("chart",{}).get("result") or [None])[0]
        if not data:
            out["error"]="yahoo_no_daily_bars"; return out
        tz=ZoneInfo((data.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York")
        q=((data.get("indicators") or {}).get("quote") or [{}])[0]
        prices={}
        for i,t in enumerate(data.get("timestamp") or []):
            day=datetime.fromtimestamp(t,tz).date().isoformat()
            if day<effective:continue
            try:
                high=float(q["high"][i]); low=float(q["low"][i]); close=float(q["close"][i])
                if min(high,low,close)<=0:continue
            except (IndexError,KeyError,TypeError,ValueError):continue
            # Daily typical price is a transparent proxy because FINRA's daily
            # aggregate contains volumes, not each short sale's execution price.
            prices[day]=(high+low+close)/3.0

        weighted=0.0; total=0.0; used=0
        for day,sv in daily_short.items():
            px=prices.get(day)
            if px is None:continue
            weighted+=px*sv; total+=sv; used+=1
        if total<=0 or used==0:
            out["error"]="no_overlapping_finra_yahoo_days"; return out
        avg=weighted/total
        current=QUOTES.get(symbol,{}).get("price")
        try:current=float(current) if current is not None else None
        except (TypeError,ValueError):current=None
        out.update({"estimated_short_avg_price":round(avg,4),
            "short_volume_used":round(total,2),"short_days_used":used,
            "short_target_drop_pct":round((avg/current-1)*100,2) if current and current>0 else None,
            "error":None})
        return out
    except Exception as exc:
        out["error"]=type(exc).__name__+":"+str(exc)[:120]
        return out

async def short_analysis_loop():
    # Cover the full universe in small batches, then refresh estimates.
    # Core IBKR borrow collection is completely independent of this worker.
    await asyncio.sleep(20)
    cursor=0
    headers={"User-Agent":"Mozilla/5.0 SnipeLab/2.0"}
    async with httpx.AsyncClient(follow_redirects=True,headers=headers) as client:
        while True:
            syms=sorted(s for s,m in UNIVERSE.items() if m.get("effective_date"))
            if not syms:
                await asyncio.sleep(60); continue
            if cursor>=len(syms):cursor=0
            batch=syms[cursor:cursor+8]
            for sym in batch:
                SHORT_ANALYSIS[sym]=await fetch_short_analysis(client,sym)
                await asyncio.sleep(0.35)
            cursor+=len(batch)
            if cursor>=len(syms):
                cursor=0
                await asyncio.sleep(900)
            else:
                await asyncio.sleep(3)

async def analytics_loop():
    await asyncio.sleep(40)
    while True:
        refresh_analytics(); STATE["analytics_count"]=len(ANALYTICS); STATE["last_analytics"]=utcnow().isoformat(); save_persistent_state()
        await asyncio.sleep(10)

def available_zero_estimate(symbol):
    """Estimate price where observed IBKR Available trend reaches ~0 without altering the borrow feed."""
    hist=BORROW_HISTORY.get(symbol) or []
    pts=[]
    for x in hist:
        try:
            old=float(x["old_available"]); new=float(x["available"]); price=float(x["price"])
        except (KeyError,TypeError,ValueError):
            continue
        if old>new and new>=0 and price>0:pts.append((new,price))
    if len(pts)<3 or len({a for a,_ in pts})<3:
        return {"status":"insufficient_data","zero_price_est":None,"fit_r2":None,"observations":len(pts),"message":"بيانات غير كافية لتقدير سعر Available≈0"}
    ma=sum(a for a,_ in pts)/len(pts);mp=sum(p for _,p in pts)/len(pts)
    den=sum((a-ma)**2 for a,_ in pts)
    if den<=0:return {"status":"insufficient_data","zero_price_est":None,"fit_r2":None,"observations":len(pts),"message":"بيانات غير كافية لتقدير سعر Available≈0"}
    beta=sum((a-ma)*(p-mp) for a,p in pts)/den;alpha=mp-beta*ma
    pred=[alpha+beta*a for a,_ in pts];ss_res=sum((p-y)**2 for (_,p),y in zip(pts,pred));ss_tot=sum((p-mp)**2 for _,p in pts)
    r2=1-(ss_res/ss_tot) if ss_tot>0 else 0.0;prices=[p for _,p in pts]
    valid=alpha>0 and min(prices)*0.5<=alpha<=max(prices)*1.5 and r2>=0.35
    return {"status":"ok" if valid else "collecting","zero_price_est":round(alpha,4) if valid else None,
        "fit_r2":round(r2,3),"observations":len(pts),"message":None if valid else "بيانات غير كافية لتقدير سعر Available≈0",
        "method":"guarded linear fit: price vs observed IBKR Available decreases"}

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
            if not rows:raise ValueError("IBKR returned zero valid USD rows; existing readings preserved")
            for sym in list(UNIVERSE):
                new=rows.get(sym)
                if new is None:continue
                new={**new,"received_at":now}; old=BORROW.get(sym)
                for ev in borrow_events(sym,old,new):
                    changed+=1; add_event(*ev)
                BORROW[sym]=new
                history=BORROW_HISTORY.setdefault(sym,[])
                sample={"at":now,"available":new["available"],"ctb":new["ctb"],"rebate":new["rebate"],"source":new["source"]}
                if old and old.get("available") is not None and float(old["available"])>float(new["available"]):
                    q=QUOTES.get(sym) or {}
                    try:px=float(q.get("price"))
                    except (TypeError,ValueError):px=None
                    if px and px>0:sample.update({"old_available":old["available"],"price":px})
                if not history or any(history[-1].get(k)!=sample[k] for k in ("available","ctb","rebate")) or (datetime.fromisoformat(now)-datetime.fromisoformat(history[-1]["at"])).total_seconds()>=3600:
                    history.append(sample)
                cutoff=time.time()-3*86400
                history[:]=[p for p in history if datetime.fromisoformat(p["at"]).timestamp()>=cutoff][-300:]
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
                        # Nasdaq also lists resumed halts; never call them HALT now.
                        resumed=len(p)>9 and bool(p[9].strip())
                        if sym in UNIVERSE and not resumed:
                            fresh[sym]={"symbol":sym,"reason":p[5],"halt_time":p[1],"halt_date":p[0]}
                for sym,row in fresh.items():
                    key=row["halt_date"]+" "+row["halt_time"]+" "+row["reason"]
                    if HALTS.get(sym,{}).get("_key")!=key:add_event(sym,"halt","توقف التداول الآن · "+row["reason"],row)
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
    asyncio.create_task(heartbeat_loop()); asyncio.create_task(universe_loop()); asyncio.create_task(delayed_market_start()); asyncio.create_task(live_daily_rsi_loop()); asyncio.create_task(delayed_borrow_start()); asyncio.create_task(analytics_loop()); asyncio.create_task(short_analysis_loop()); asyncio.create_task(finnhub_live_loop()); asyncio.create_task(halt_loop()); asyncio.create_task(historical_worker(UNIVERSE,HISTORY,YAHOO,save_persistent_state))

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
    refresh_analytics()
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

def data_freshness(row, timestamp_field, max_age_seconds):
    """Explicit freshness for cached quotes/borrow; no fabricated live claims."""
    stamp=(row or {}).get(timestamp_field)
    if not stamp:
        return {"status":"missing","age_seconds":None,"timestamp":None}
    try:
        parsed=datetime.fromisoformat(stamp.replace("Z","+00:00"))
        age=max(0,int((utcnow()-parsed.astimezone(timezone.utc)).total_seconds()))
        return {"status":"fresh" if age<=max_age_seconds else "stale",
                "age_seconds":age,"timestamp":stamp}
    except (TypeError,ValueError):
        return {"status":"unknown","age_seconds":None,"timestamp":stamp}

@app.get("/api/data-freshness")
async def data_freshness_report():
    """Read-only source coverage and age, without waiting for upstream APIs."""
    quotes={sym:data_freshness(QUOTES.get(sym),"received_at",900) for sym in UNIVERSE}
    borrow={sym:data_freshness(BORROW.get(sym),"received_at",1200) for sym in UNIVERSE}
    def counts(rows):
        return {state:sum(v["status"]==state for v in rows.values())
                for state in ("fresh","stale","missing","unknown")}
    return {"generated_at":utcnow().isoformat(),"universe_count":len(UNIVERSE),
            "quotes":counts(quotes),"borrow":counts(borrow),
            "last_market_scan":STATE["last_market_scan"],
            "last_borrow_scan":STATE["last_borrow_scan"],
            "last_market_error":STATE["last_market_error"],
            "last_borrow_error":STATE["last_borrow_error"],
            "quote_max_age_seconds":900,"borrow_max_age_seconds":1200}

@app.get("/api/radar-memory/{symbol}")
async def radar_memory(symbol: str):
    symbol=re.sub(r"[^A-Z0-9.-]","",symbol.upper())[:12]
    return {"symbol":symbol,"memory":RADAR_MEMORY.get(symbol,{}),"note":"Only observed quotes since feature deployment; not intraday high/low."}

@app.get("/api/lab")
async def signal_lab():
    signals=[]
    for sym,m in RADAR_MEMORY.items():
        for stage in ("radar","ready"):
            rec=m.get("first_"+stage)
            if rec and rec.get("price"):
                base=rec["price"]
                signals.append({"symbol":sym,"stage":stage,**rec,
                    "max_observed_pct":round((rec["high_observed"]/base-1)*100,2),
                    "min_observed_pct":round((rec["low_observed"]/base-1)*100,2)})
    return {"count":len(signals),"signals":sorted(signals,key=lambda x:x["at"],reverse=True)[:200],
        "note":"Observed quote snapshots only, not historical backtest or trades."}

@app.get("/api/borrow-history/{symbol}")
async def borrow_history(symbol: str):
    symbol=re.sub(r"[^A-Z0-9.-]","",symbol.upper())[:12]
    return {"symbol":symbol,"period_days":3,"source":"IBKR public FTP","last_scan":STATE.get("last_borrow_scan"),"scan_error":STATE.get("last_borrow_error"),"last_reading":BORROW.get(symbol),"snapshots":BORROW_HISTORY.get(symbol,[]),"note":"Recording starts after deployment; no invented historical values."}

@app.get("/api/dashboard")
async def dashboard_data():
    # Dashboard must be read-only. Recomputing the entire universe inside
    # the HTTP handler can raise on a single malformed quote and cause 500.
    # The background analytics worker owns refreshes.
    rows={}
    for sym,meta in UNIVERSE.items():
        h=HISTORY.get(sym,{})
        signal={**(ANALYTICS.get(sym) or {}),
            "quote_freshness":data_freshness(QUOTES.get(sym),"received_at",900),
            "borrow_freshness":data_freshness(BORROW.get(sym),"received_at",1200),
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
            "rsi_daily":wilder_rsi_live_from_history(sym),
            "top_10_gain_pct":h.get("top_10_gain_pct"),
            "top_10_low":h.get("top_10_low"),
            "top_10_high":h.get("top_10_high"),
            "top_10_low_date":h.get("top_10_low_date"),
            "top_10_high_date":h.get("top_10_high_date"),
            "top_10_verified":bool(h.get("top_10_verified")),
            "top_10_sessions_since_peak":h.get("top_10_sessions_since_peak"),
            "top_calculator_version":h.get("top_calculator_version",0),
            "top_10_source":h.get("top_10_source"),
            "top_10_provisional":bool(h.get("top_10_provisional")),
            "history_status":h.get("error") or ("verified" if h.get("verified") else "pending"),
            "short_analysis":SHORT_ANALYSIS.get(sym,{}),
            "available_zero_estimate":available_zero_estimate(sym)}
        # A same-day live rise is a separate, explicitly provisional measure:
        # never mix it silently with the completed-session low-to-high TOP.
        q=QUOTES.get(sym) or {}
        try:
            from zoneinfo import ZoneInfo
            market_day=datetime.fromisoformat(q["market_timestamp"].replace("Z","+00:00")).astimezone(ZoneInfo("America/New_York")).date()
            received=datetime.fromisoformat(q["received_at"].replace("Z","+00:00"))
            reference=float(q["previous_close"])
            high=float(q["day_high"])
            quote_current=(utcnow()-received).total_seconds()<=900
            if (reference>0 and high>=reference and quote_current
                    and market_day==utcnow().astimezone(ZoneInfo("America/New_York")).date()
                    and h.get("verified")):
                signal["live_day_rise_pct"]=round((high/reference-1)*100,2)
                signal["live_day_rise_source"]="previous_close_to_day_high"
                signal["live_day_rise_provisional"]=True
        except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):
            pass
        rows[sym]={"symbol":sym,"company_name":meta.get("company_name") or meta.get("name") or (QUOTES.get(sym) or {}).get("short_name"),"effective_date":meta.get("effective_date"),"price":QUOTES.get(sym),"borrow":BORROW.get(sym),"borrow_history":BORROW_HISTORY.get(sym,[])[-12:],"signal":signal}
    relevant_kinds={"price_25","halt","available_10k","available_zero","ready"}
    important_events=[e for e in EVENTS if e.get("kind") in relevant_kinds]
    return {"server_time":utcnow().isoformat(),"uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),"storage":storage.status(),"history_count":sum(bool(HISTORY.get(sym,{}).get("verified")) for sym in UNIVERSE),"history_pending":sum(1 for sym in UNIVERSE if not HISTORY.get(sym,{}).get("verified")),"health":{"ok":STATE.get("status")=="running","heartbeat":STATE.get("heartbeat"),"universe_count":len(UNIVERSE),"price_count":len(QUOTES),"borrow_count":len(BORROW),"analytics_count":len(ANALYTICS)},"rows":rows,"events":important_events[:40],"halts":HALTS,"news":NEWS}

@app.get("/halts")
async def halts():
    return {"last_scan":STATE["last_halt_scan"],"error":STATE["halt_error"],"count":len(HALTS),"rows":HALTS}

@app.get("/news")
async def news():
    return {"last_scan":STATE["last_news_scan"],"error":STATE["news_error"],"count":len(NEWS),"rows":NEWS}

@app.get("/events")
async def events():
    return {"count":len(EVENTS),"events":EVENTS}

# Independent per-ticker RSS cache; no dependency on the disabled legacy site.
_NEWS_CACHE = {}
_NEWS_INFLIGHT = {}
_NEWS_TTL = 1800

@app.get("/api/news/{symbol}")
async def ticker_news(symbol: str):
    """Source-backed headlines, optional Arabic summary when an AI key is configured."""
    from xml.etree import ElementTree as ET
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    symbol = re.sub(r"[^A-Z0-9.-]", "", symbol.upper())[:12]
    if not symbol or symbol not in UNIVERSE:
        return {"symbol":symbol,"items":[],"message":"الرمز غير موجود في قائمة الأسهم الحالية."}
    cached = _NEWS_CACHE.get(symbol)
    if cached and time.time()-cached["at"] < _NEWS_TTL:
        return cached["result"]
    if symbol in _NEWS_INFLIGHT:
        try:
            return await asyncio.wait_for(asyncio.shield(_NEWS_INFLIGHT[symbol]),timeout=12)
        except (asyncio.TimeoutError,Exception):
            return cached["result"] if cached else {"symbol":symbol,"items":[],"message":"الأخبار قيد التحديث."}
    async def gather():
        items=[]
        try:
            query=quote(f'"{symbol}" stock NASDAQ OR NYSE when:7d')
            url=f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            async with httpx.AsyncClient(timeout=8,follow_redirects=True) as client:
                response=await client.get(url,headers={"User-Agent":"SnipeLab/2.0 news reader"})
                response.raise_for_status()
            root=ET.fromstring(response.content)
            for item in root.findall("./channel/item")[:5]:
                title=(item.findtext("title") or "").strip()
                link=(item.findtext("link") or "").strip()
                if not title or not link.startswith("https://"):continue
                published=item.findtext("pubDate") or ""
                try:published=parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
                except (ValueError,TypeError):pass
                source_node=item.find("source")
                source=source_node.text if source_node is not None else "Google News"
                items.append({"title":title,"url":link,"source":source,
                              "published_at":published,"sentiment":None,"summary_ar":None})
            # No inferred Arabic summaries or sentiment from untranslated titles.
            # An optional key enables a grounded summary of ONLY the retrieved titles.
            key=os.environ.get("OPENAI_API_KEY")
            if items and key:
                try:
                    headlines=[{"title":x["title"],"source":x["source"],"date":x["published_at"]} for x in items[:3]]
                    request={"model":os.environ.get("SNIPELAB_NEWS_MODEL","gpt-4o-mini"),
                             "response_format":{"type":"json_object"},
                             "temperature":0,
                             "messages":[{"role":"system","content":"Summarize ONLY the supplied news headlines in concise Arabic. Return JSON with summary_ar (string, maximum 60 Arabic words) and sentiment (positive, negative, neutral). If headlines lack enough information to classify, use neutral. Do not invent events, numbers, facts or investment advice. Distinguish headlines from verified company announcements."},
                                         {"role":"user","content":json.dumps(headlines,ensure_ascii=False)}]}
                    async with httpx.AsyncClient(timeout=12) as client:
                        result=await client.post("https://api.openai.com/v1/chat/completions",
                            headers={"Authorization":"Bearer "+key},json=request)
                        result.raise_for_status()
                    parsed=json.loads(result.json()["choices"][0]["message"]["content"])
                    tone=parsed.get("sentiment")
                    if tone not in ("positive","negative","neutral"):tone=None
                    summary=str(parsed.get("summary_ar") or "").strip()[:600]
                    if summary:
                        items[0]["summary_ar"]=summary
                        items[0]["sentiment"]=tone
                except Exception:
                    pass  # Preserve sourced headlines without inventing a summary.
            message=("أخبار من مصادر منشورة. التصنيف الآلي للعنوان لا يُعد توصية تداول."
                     if any(x.get("summary_ar") for x in items)
                     else "عناوين من مصادر منشورة؛ الملخص العربي والتصنيف غير متاحين حالياً.")
            result={"symbol":symbol,"items":items,"message":message}
        except Exception as exc:
            result={"symbol":symbol,"items":cached["result"]["items"] if cached else [],
                    "message":"تعذر تحديث الأخبار من المصدر؛ قد تكون النتائج السابقة قديمة.",
                    "error":type(exc).__name__}
        _NEWS_CACHE[symbol]={"at":time.time(),"result":result}
        return result
    task=asyncio.create_task(gather())
    _NEWS_INFLIGHT[symbol]=task
    try:return await task
    finally:_NEWS_INFLIGHT.pop(symbol,None)

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

@app.get("/api/history-audit")
async def history_audit():
    """Compact actionable report; do not confuse field coverage with validation."""
    report=await history_coverage()
    rows=report.pop("rows")
    pending=[x for x in rows if x["audit_status"]=="pending"]
    return {**report,
        "pending_symbols":[{"symbol":x["symbol"],"effective_date":x["effective_date"],
            "missing":x["missing"],"error":x["error"],
            "split_day_4h_status":x["split_day_4h_status"],
            "last_attempt":x["last_attempt"]} for x in pending],
        "quality_examples":[{"symbol":x["symbol"],"warnings":x["quality_warnings"]}
            for x in rows if x["quality_warnings"]][:20],
        "note":"Completed fields are not independent price validation."}

FINNHUB_PROBE = {"status":"idle","mode":None,"started_at":None,"finished_at":None,"requested":0,"subscribed":0,"symbols_with_trades":0,"trade_messages":0,"trades":0,"errors":[],"seen_symbols":[],"last_trade_at":None,"limit_results":[],"max_without_limit_error":None}
_FINNHUB_PROBE_TASK = None

def _finnhub_token():
    return os.environ.get("FINNHUB_API_KEY") or os.environ.get("FINNHUB_TOKEN")

async def _finnhub_limit_trial(token, syms, count, settle=2.0):
    errors=[];seen=set();trade_messages=trades=0
    async with websockets.connect("wss://ws.finnhub.io?token="+token,open_timeout=15,ping_interval=20,ping_timeout=20,max_size=2_000_000) as ws:
        for sym in syms[:count]:
            await ws.send(json.dumps({"type":"subscribe","symbol":sym}))
            await asyncio.sleep(0.015)
        deadline=time.monotonic()+settle
        while time.monotonic()<deadline:
            try: raw=await asyncio.wait_for(ws.recv(),timeout=min(.5,max(.05,deadline-time.monotonic())))
            except asyncio.TimeoutError: continue
            try: msg=json.loads(raw)
            except Exception: continue
            if msg.get("type")=="error":
                err=str(msg.get("msg") or msg)[:300]
                if err not in errors:errors.append(err)
            elif msg.get("type")=="trade":
                trade_messages+=1;data=msg.get("data") or [];trades+=len(data)
                seen.update(str(x.get("s") or "").upper() for x in data if x.get("s"))
    too_many=any("too many symbols" in e.lower() for e in errors)
    return {"count":count,"accepted":not too_many,"too_many_symbols":too_many,"errors":errors,"symbols_with_trades":len(seen),"trade_messages":trade_messages,"trades":trades}

FINNHUB_LIVE={"status":"idle","connected":False,"selected":[],"ready_slots":[],"hot_slots":[],"reserve_slots":2,"updates":0,"last_update":None,"error":None}

def _live_rise_pct(sym):
    q=QUOTES.get(sym) or {}
    try:
        p=float(q.get("price"));ref=float(q.get("previous_close"));return (p/ref-1)*100 if ref>0 else None
    except (TypeError,ValueError,ZeroDivisionError):return None

def select_finnhub_live_symbols():
    # 53 proven account slots: 36 readiness + 15 movers >=30% + 2 reserve.
    ranked=[]
    for sym,a in ANALYTICS.items():
        if sym not in UNIVERSE or not a.get("active"):continue
        ranked.append((-(float(a.get("readiness_pct") or a.get("score") or 0)),int(a.get("missing_count") or 99),sym))
    ready=[x[2] for x in sorted(ranked)[:36]]
    hot=[]
    for sym in UNIVERSE:
        pct=_live_rise_pct(sym)
        if pct is not None and pct>=30 and sym not in ready:hot.append((pct,sym))
    hot=[sym for pct,sym in sorted(hot,reverse=True)[:15]]
    return ready,hot,ready+hot

async def finnhub_live_loop():
    await asyncio.sleep(55)
    token=_finnhub_token()
    if not token:FINNHUB_LIVE.update(status="disabled",error="Finnhub key missing");return
    while True:
        try:
            FINNHUB_LIVE.update(status="connecting",connected=False,error=None)
            async with websockets.connect("wss://ws.finnhub.io?token="+token,open_timeout=15,ping_interval=20,ping_timeout=20,max_size=2_000_000) as ws:
                active=set();FINNHUB_LIVE.update(status="running",connected=True)
                while True:
                    ready,hot,wanted=select_finnhub_live_symbols();wanted=set(wanted)
                    for sym in sorted(active-wanted):await ws.send(json.dumps({"type":"unsubscribe","symbol":sym}));active.discard(sym)
                    for sym in sorted(wanted-active):await ws.send(json.dumps({"type":"subscribe","symbol":sym}));active.add(sym);await asyncio.sleep(.015)
                    FINNHUB_LIVE["selected"]=sorted(active);FINNHUB_LIVE["ready_slots"]=ready;FINNHUB_LIVE["hot_slots"]=hot
                    try:raw=await asyncio.wait_for(ws.recv(),timeout=2)
                    except asyncio.TimeoutError:continue
                    try:msg=json.loads(raw)
                    except Exception:continue
                    if msg.get("type")=="error":FINNHUB_LIVE["error"]=str(msg.get("msg") or msg)[:300];continue
                    if msg.get("type")!="trade":continue
                    for trade in msg.get("data") or []:
                        sym=str(trade.get("s") or "").upper()
                        if sym not in active or sym not in UNIVERSE:continue
                        try:p=float(trade.get("p"));ts=int(trade.get("t") or 0)
                        except (TypeError,ValueError):continue
                        if p<=0:continue
                        q=QUOTES.get(sym) or {};old_ts=q.get("market_timestamp")
                        market_ts=datetime.fromtimestamp(ts/1000,tz=timezone.utc).isoformat() if ts else utcnow().isoformat()
                        # Preserve Yahoo session fields used by analytics; Finnhub supplies the tick price/time.
                        QUOTES[sym]={**q,"symbol":sym,"price":p,"market_timestamp":market_ts,"received_at":utcnow().isoformat(),"source":"finnhub_live","previous_source":q.get("source")}
                        FINNHUB_LIVE["updates"]+=1;FINNHUB_LIVE["last_update"]=utcnow().isoformat()
        except Exception as exc:
            FINNHUB_LIVE.update(status="reconnecting",connected=False,error=type(exc).__name__+": "+str(exc)[:250]);await asyncio.sleep(10)

FINNHUB_TICKER_PROBE={"status":"idle","started_at":None,"finished_at":None,"symbol":"FFAI","messages":0,"trade_messages":0,"trades":0,"last_price":None,"last_trade_at":None,"message_types":[],"raw_samples":[],"errors":[]}
_FINNHUB_TICKER_TASK=None

async def run_finnhub_ticker_probe(seconds=30):
    global FINNHUB_TICKER_PROBE
    token=_finnhub_token();seconds=max(10,min(int(seconds),90))
    report={"status":"running","started_at":utcnow().isoformat(),"finished_at":None,"symbol":"FFAI","seconds":seconds,"messages":0,"trade_messages":0,"trades":0,"last_price":None,"last_trade_at":None,"message_types":[],"raw_samples":[],"errors":[]}
    FINNHUB_TICKER_PROBE=report
    if not token:report.update(status="error",finished_at=utcnow().isoformat(),errors=["Finnhub key missing"]);return
    # Finnhub permits one WebSocket per key. Temporarily close the production
    # live socket so this isolated FFAI diagnostic can own that connection.
    FINNHUB_LIVE["probe_pause"]=True
    deadline=time.monotonic()+seconds
    try:
        await asyncio.sleep(3)
        async with websockets.connect("wss://ws.finnhub.io?token="+token,open_timeout=15,ping_interval=20,ping_timeout=20,max_size=2_000_000) as ws:
            await ws.send(json.dumps({"type":"subscribe","symbol":"FFAI"}))
            while time.monotonic()<deadline:
                try:raw=await asyncio.wait_for(ws.recv(),timeout=min(2,max(.1,deadline-time.monotonic())))
                except asyncio.TimeoutError:continue
                report["messages"]+=1
                if len(report["raw_samples"])<8:report["raw_samples"].append(str(raw)[:1000])
                try:msg=json.loads(raw)
                except Exception:continue
                typ=str(msg.get("type") or "unknown");
                if typ not in report["message_types"]:report["message_types"].append(typ)
                if typ=="error":
                    err=str(msg.get("msg") or msg)[:300]
                    if err not in report["errors"]:report["errors"].append(err)
                elif typ=="trade":
                    data=msg.get("data") or [];report["trade_messages"]+=1;report["trades"]+=len(data)
                    for t in data:
                        if str(t.get("s") or "").upper()=="FFAI":
                            report["last_price"]=t.get("p");report["last_trade_at"]=t.get("t")
        report["status"]="complete"
    except Exception as exc:
        report["status"]="error";report["errors"].append(type(exc).__name__+": "+str(exc)[:300])
    finally:
        report["finished_at"]=utcnow().isoformat();FINNHUB_LIVE["probe_pause"]=False

async def run_finnhub_limit_probe():
    global FINNHUB_PROBE
    token=_finnhub_token();syms=sorted(UNIVERSE);started=utcnow().isoformat()
    report={"status":"running","mode":"limit_single_socket","started_at":started,"finished_at":None,"requested":len(syms),"subscribed":0,"symbols_with_trades":0,"trade_messages":0,"trades":0,"errors":[],"seen_symbols":[],"last_trade_at":None,"limit_results":[],"max_without_limit_error":None,"first_rejected_at":None}
    FINNHUB_PROBE=report
    if not token:
        report["status"]="error";report["errors"]=["Missing FINNHUB_API_KEY (or FINNHUB_TOKEN) environment variable"];report["finished_at"]=utcnow().isoformat();return
    seen=set();accepted=0
    try:
        # Deliberately keep ONE WebSocket open for the whole test. This avoids
        # confusing Finnhub's connection/rate limit (HTTP 429) with its symbol cap.
        async with websockets.connect("wss://ws.finnhub.io?token="+token,open_timeout=15,ping_interval=20,ping_timeout=20,max_size=2_000_000) as ws:
            for idx,sym in enumerate(syms,1):
                await ws.send(json.dumps({"type":"subscribe","symbol":sym}))
                report["subscribed"]=idx
                rejected=False;other_errors=[]
                # Give the server a short chance to reject this exact addition.
                deadline=time.monotonic()+0.12
                while time.monotonic()<deadline:
                    try:raw=await asyncio.wait_for(ws.recv(),timeout=max(.01,deadline-time.monotonic()))
                    except asyncio.TimeoutError:break
                    try:msg=json.loads(raw)
                    except Exception:continue
                    if msg.get("type")=="error":
                        err=str(msg.get("msg") or msg)[:300]
                        if "too many symbols" in err.lower():rejected=True
                        elif err not in other_errors:other_errors.append(err)
                    elif msg.get("type")=="trade":
                        report["trade_messages"]+=1;data=msg.get("data") or [];report["trades"]+=len(data)
                        seen.update(str(x.get("s") or "").upper() for x in data if x.get("s"));report["last_trade_at"]=utcnow().isoformat()
                if other_errors:
                    for err in other_errors:
                        if err not in report["errors"]:report["errors"].append(err)
                if rejected:
                    report["first_rejected_at"]=idx;report["limit_results"].append({"count":idx,"symbol":sym,"accepted":False,"too_many_symbols":True});break
                accepted=idx
                if idx in (10,20,30,40,50,75,100,150,200,len(syms)):
                    report["limit_results"].append({"count":idx,"symbol":sym,"accepted":True,"too_many_symbols":False})
                report["max_without_limit_error"]=accepted;report["symbols_with_trades"]=len(seen);report["seen_symbols"]=sorted(seen)
                await asyncio.sleep(.02)
            # Drain briefly so a delayed rejection is not missed.
            drain_deadline=time.monotonic()+2
            while time.monotonic()<drain_deadline and report["first_rejected_at"] is None:
                try:raw=await asyncio.wait_for(ws.recv(),timeout=.25)
                except asyncio.TimeoutError:continue
                try:msg=json.loads(raw)
                except Exception:continue
                if msg.get("type")=="error" and "too many symbols" in str(msg.get("msg") or msg).lower():
                    report["first_rejected_at"]=report["subscribed"];report["max_without_limit_error"]=max(0,report["subscribed"]-1);report["limit_results"].append({"count":report["subscribed"],"accepted":False,"too_many_symbols":True,"delayed":True});break
                if msg.get("type")=="trade":
                    report["trade_messages"]+=1;data=msg.get("data") or [];report["trades"]+=len(data);seen.update(str(x.get("s") or "").upper() for x in data if x.get("s"))
    except Exception as exc:
        report["errors"].append(type(exc).__name__+": "+str(exc)[:300])
    report["symbols_with_trades"]=len(seen);report["seen_symbols"]=sorted(seen);report["finished_at"]=utcnow().isoformat();report["status"]="complete" if not report["errors"] else "complete_with_errors"

async def run_finnhub_probe(seconds=120):
    global FINNHUB_PROBE
    token=_finnhub_token();syms=sorted(UNIVERSE)
    report={"status":"running","mode":"coverage","started_at":utcnow().isoformat(),"finished_at":None,"requested":len(syms),"subscribed":0,"symbols_with_trades":0,"trade_messages":0,"trades":0,"errors":[],"seen_symbols":[],"last_trade_at":None,"limit_results":[],"max_without_limit_error":None};FINNHUB_PROBE=report
    if not token:
        report["status"]="error";report["errors"]=["Missing FINNHUB_API_KEY (or FINNHUB_TOKEN) environment variable"];report["finished_at"]=utcnow().isoformat();return
    seen=set()
    try:
        async with websockets.connect("wss://ws.finnhub.io?token="+token,open_timeout=15,ping_interval=20,ping_timeout=20,max_size=2_000_000) as ws:
            for sym in syms:
                await ws.send(json.dumps({"type":"subscribe","symbol":sym}));report["subscribed"]+=1;await asyncio.sleep(.01)
            deadline=time.monotonic()+max(30,min(int(seconds),600))
            while time.monotonic()<deadline:
                try:raw=await asyncio.wait_for(ws.recv(),timeout=min(10,max(.1,deadline-time.monotonic())))
                except asyncio.TimeoutError:continue
                try:msg=json.loads(raw)
                except Exception:continue
                if msg.get("type")=="trade":
                    report["trade_messages"]+=1;data=msg.get("data") or [];report["trades"]+=len(data)
                    for trade in data:
                        sym=str(trade.get("s") or "").upper()
                        if sym in UNIVERSE:seen.add(sym)
                    report["last_trade_at"]=utcnow().isoformat()
                elif msg.get("type")=="error":
                    err=str(msg.get("msg") or msg)[:300]
                    if err not in report["errors"]:report["errors"].append(err)
                report["symbols_with_trades"]=len(seen);report["seen_symbols"]=sorted(seen)
    except Exception as exc:report["errors"].append(type(exc).__name__+": "+str(exc)[:300])
    report["symbols_with_trades"]=len(seen);report["seen_symbols"]=sorted(seen);report["finished_at"]=utcnow().isoformat();report["status"]="complete" if not report["errors"] else "complete_with_errors"

@app.post("/api/finnhub-probe/start")
async def start_finnhub_probe(seconds: int=120):
    global _FINNHUB_PROBE_TASK
    if _FINNHUB_PROBE_TASK and not _FINNHUB_PROBE_TASK.done():return {"started":False,"reason":"probe already running","report":FINNHUB_PROBE}
    _FINNHUB_PROBE_TASK=asyncio.create_task(run_finnhub_probe(seconds));return {"started":True,"mode":"coverage","seconds":max(30,min(int(seconds),600)),"universe":len(UNIVERSE)}

@app.post("/api/finnhub-probe/ffai")
async def start_finnhub_ffai_probe(seconds: int=30):
    global _FINNHUB_TICKER_TASK
    if _FINNHUB_TICKER_TASK and not _FINNHUB_TICKER_TASK.done():return {"started":False,"reason":"FFAI probe already running","report":FINNHUB_TICKER_PROBE}
    _FINNHUB_TICKER_TASK=asyncio.create_task(run_finnhub_ticker_probe(seconds));return {"started":True,"symbol":"FFAI","seconds":max(10,min(int(seconds),90))}

@app.get("/api/finnhub-probe/ffai")
async def finnhub_ffai_probe_status():
    return {**FINNHUB_TICKER_PROBE,"key_configured":bool(_finnhub_token())}

@app.post("/api/finnhub-probe/limit")
async def start_finnhub_limit_probe():
    global _FINNHUB_PROBE_TASK
    if _FINNHUB_PROBE_TASK and not _FINNHUB_PROBE_TASK.done():return {"started":False,"reason":"probe already running","report":FINNHUB_PROBE}
    _FINNHUB_PROBE_TASK=asyncio.create_task(run_finnhub_limit_probe());return {"started":True,"mode":"limit","universe":len(UNIVERSE),"note":"Tests progressively, then binary-searches the exact symbol boundary."}

@app.get("/api/finnhub-probe")
async def finnhub_probe_status():
    return {**FINNHUB_PROBE,"universe_now":len(UNIVERSE),"key_configured":bool(_finnhub_token()),"note":"Limit mode measures subscription rejection; trade activity is not required to identify the cap."}

@app.get("/api/finnhub-live")
async def finnhub_live_status():
    return {**FINNHUB_LIVE,"selected_count":len(FINNHUB_LIVE.get("selected") or []),"ready_count":len(FINNHUB_LIVE.get("ready_slots") or []),"hot_count":len(FINNHUB_LIVE.get("hot_slots") or []),"capacity":53,"allocation":{"ready":36,"hot_30pct":15,"reserve":2}}

@app.get("/api/diagnostics/{symbol}")
async def ticker_diagnostics(symbol: str):
    symbol=re.sub(r"[^A-Z0-9.-]","",symbol.upper())[:12]
    # Never serve a restored pre-v4 readiness score after a deployment.
    # Diagnostics must remain available even if a background calculation fails.
    return {"symbol":symbol,"in_split_universe":symbol in UNIVERSE,"server_time":utcnow().isoformat(),"last_market_scan":STATE["last_market_scan"],"last_borrow_scan":STATE["last_borrow_scan"],
            "split":UNIVERSE.get(symbol),"history":HISTORY.get(symbol),
            "quote":QUOTES.get(symbol),"borrow":BORROW.get(symbol),
            "signal":ANALYTICS.get(symbol),
            "note":"A rally alone does not establish a qualifying reverse split."}
