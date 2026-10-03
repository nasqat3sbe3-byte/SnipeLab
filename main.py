import asyncio
import ftplib
import io
import os
import re
import json
from pathlib import Path
import time
from datetime import datetime, timezone, date

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="SnipeLab Engine", version="0.7.0")
BOOTED_AT = datetime.now(timezone.utc)

UNIVERSE_SEED = ["MSGY","WCT","NCT","EPOW","CPOP","LGCL","NRSN","HUBC","MGN","FGL","OMH","AIXI","SFWL","TNMG","LRHC","RCON","CXAI","YYAI","YXT","RBNE","CISS","IZM","GAUZ","LGHL","UCAR","HLSQ","ALP","GTBP","GOSS","JAGX","NFE","IPDN","NXXT","ENLV","STKH","TRIB","FFAI"]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_FILE = "ftp2.interactivebrokers.com", "shortstock", "", "usa.txt"
SPLITS_URLS = ("https://stockanalysis.com/actions/splits/2026/", "https://stockanalysis.com/actions/splits/")

STATE = {
    "status":"starting","heartbeat":None,"heartbeat_count":0,"booted_at":BOOTED_AT.isoformat(),
    "universe_count":len(UNIVERSE_SEED),"last_universe_sync":None,"universe_error":None,"universe_attempts":0,"universe_source":"seed",
    "market_scan_count":0,"last_market_scan":None,"market_ok":0,"market_failed":0,"last_market_error":None,"market_cursor":0,"market_cycle":0,
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
DAILY = {}
BORROW_HISTORY = {}
STATE_FILE = Path(os.environ.get("SNIPELAB_STATE_FILE","/tmp/snipelab_state.json"))
_LAST_SAVE = 0.0

def load_persistent_state():
    try:
        if not STATE_FILE.exists(): return
        d=json.loads(STATE_FILE.read_text("utf-8"))
        ANALYTICS.update(d.get("analytics") or {})
        BORROW.update(d.get("borrow") or {})
        EVENTS.extend((d.get("events") or [])[:100])
        BORROW_HISTORY.update(d.get("borrow_history") or {})
    except Exception as exc:
        STATE["persistence_error"]=f"load {type(exc).__name__}: {str(exc)[:100]}"

def save_persistent_state(force=False):
    global _LAST_SAVE
    now=time.time()
    if not force and now-_LAST_SAVE<60:return
    try:
        tmp=STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at":utcnow().isoformat(),"analytics":ANALYTICS,"borrow":BORROW,"borrow_history":BORROW_HISTORY,"events":EVENTS[:100]},separators=(",",":")),"utf-8")
        tmp.replace(STATE_FILE); _LAST_SAVE=now
        STATE["last_state_save"]=utcnow().isoformat(); STATE["persistence_error"]=None
    except Exception as exc:
        STATE["persistence_error"]=f"save {type(exc).__name__}: {str(exc)[:100]}"

def utcnow(): return datetime.now(timezone.utc)
def add_event(symbol, kind, text, data=None):
    EVENTS.insert(0,{"symbol":symbol,"kind":kind,"text":text,"at":utcnow().isoformat(),"data":data or {}})
    del EVENTS[100:]

async def heartbeat_loop():
    while True:
        STATE["status"]="running"; STATE["heartbeat"]=utcnow().isoformat(); STATE["heartbeat_count"]+=1
        await asyncio.sleep(10)

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
            if eff < date(2026,5,1) or eff > date(2026,12,31): continue
            sym=tds[1].upper().strip()
            if sym:
                candidate={"symbol":sym,"company":tds[2],"effective_date":eff.isoformat(),"ratio":tds[4],"source":"stockanalysis"}
                previous=merged.get(sym)
                if previous is None or candidate["effective_date"] > previous["effective_date"]:
                    merged[sym]=candidate
    if not merged: raise RuntimeError("empty direct split feed | "+" | ".join(errors))
    UNIVERSE.clear(); UNIVERSE.update(merged)
    STATE["universe_count"]=len(UNIVERSE); STATE["last_universe_sync"]=utcnow().isoformat(); STATE["universe_error"]=None; STATE["universe_source"]="stockanalysis_direct"
    return True

async def sync_universe(client):
    STATE["universe_attempts"]+=1
    last_error=None
    try:
        if await fetch_direct_universe(client): return
    except Exception as exc:
        last_error=f"direct: {type(exc).__name__}"
    for path in ("/api/hunt","/api/splits"):
        try:
            r=await client.get(QANAS_WEB+path,timeout=60); r.raise_for_status(); rows=r.json()
            if not isinstance(rows,list) or not rows: raise RuntimeError("empty universe")
            fresh={}
            today=utcnow().date().isoformat()
            for x in rows:
                sym=str(x.get("symbol") or "").upper().strip()
                eff=str(x.get("effective_date") or "")[:10]
                if sym and (not eff or eff<=today): fresh[sym]=x
            if fresh:
                UNIVERSE.clear(); UNIVERSE.update(fresh)
                STATE["universe_count"]=len(UNIVERSE); STATE["last_universe_sync"]=utcnow().isoformat()
                STATE["universe_error"]=None
                return
        except Exception as exc: last_error=f"{path}: {type(exc).__name__}"
    # Render can be slow to wake up. Never leave the watcher empty while it retries.
    if not UNIVERSE:
        UNIVERSE.update({s:{"symbol":s,"effective_date":None,"source":"seed"} for s in UNIVERSE_SEED})
        STATE["universe_count"]=len(UNIVERSE)
    STATE["universe_error"]=last_error or "unknown"; STATE["universe_source"]="seed" if STATE["last_universe_sync"] is None else STATE["universe_source"]

async def universe_loop():
    # Keep Northflank ingress healthy before any external scraping starts.
    await asyncio.sleep(15)
    headers={"User-Agent":"Mozilla/5.0 QanasWatcher/0.3"}
    async with httpx.AsyncClient(follow_redirects=True,headers=headers) as client:
        while True:
            await sync_universe(client)
            await asyncio.sleep(120)

async def fetch_quote(client, sem, symbol):
    async with sem:
        try:
            r=await client.get(YAHOO.format(symbol=symbol),params={"range":"1d","interval":"1m","includePrePost":"true","events":"history"})
            r.raise_for_status(); result=(r.json().get("chart",{}).get("result") or [None])[0]
            if not result:return symbol,None
            ts=result.get("timestamp") or []; q=((result.get("indicators") or {}).get("quote") or [{}])[0]
            closes=q.get("close") or []; highs=q.get("high") or []; lows=q.get("low") or []
            valid=[(int(t),float(closes[i])) for i,t in enumerate(ts) if i<len(closes) and closes[i] is not None and float(closes[i])>0]
            if not valid:return symbol,None
            t,p=max(valid,key=lambda z:z[0]); hi=[float(v) for v in highs if v is not None and float(v)>0]; lo=[float(v) for v in lows if v is not None and float(v)>0]
            return symbol,{"symbol":symbol,"price":p,"day_high":max(hi) if hi else p,"day_low":min(lo) if lo else p,
                "market_timestamp":datetime.fromtimestamp(t,tz=timezone.utc).isoformat(),"received_at":utcnow().isoformat(),"source":"yahoo_1m_prepost"}
        except Exception:return symbol,None


def wilder_rsi(closes, period=14):
    vals=[float(x) for x in closes if x is not None and float(x)>0]
    if len(vals)<period+1:return None
    changes=[vals[i]-vals[i-1] for i in range(1,len(vals))]
    gains=[max(x,0.0) for x in changes]; losses=[max(-x,0.0) for x in changes]
    avg_gain=sum(gains[:period])/period; avg_loss=sum(losses[:period])/period
    for i in range(period,len(changes)):
        avg_gain=((avg_gain*(period-1))+gains[i])/period
        avg_loss=((avg_loss*(period-1))+losses[i])/period
    if avg_loss==0:return 100.0
    rs=avg_gain/avg_loss
    return 100.0-(100.0/(1.0+rs))

async def fetch_daily_rsi(client, sem, symbol):
    async with sem:
        try:
            r=await client.get(YAHOO.format(symbol=symbol),params={"range":"3mo","interval":"1d","includePrePost":"false","events":"history"})
            r.raise_for_status(); result=(r.json().get("chart",{}).get("result") or [None])[0]
            if not result:return symbol,None
            ts=result.get("timestamp") or []; q=((result.get("indicators") or {}).get("quote") or [{}])[0]
            closes=q.get("close") or []
            bars=[(int(ts[i]),float(v)) for i,v in enumerate(closes) if i<len(ts) and v is not None and float(v)>0]
            if len(bars)<15:return symbol,None
            # Daily Live matches the chart during the session; closed value is kept separately for audit/comparison.
            live=[v for _,v in bars]
            today=utcnow().date()
            completed=[v for t,v in bars if datetime.fromtimestamp(t,tz=timezone.utc).date()<today]
            if len(completed)<15:completed=[v for _,v in bars[:-1]]
            live_value=wilder_rsi(live,14); closed_value=wilder_rsi(completed,14)
            if live_value is None:return symbol,None
            return symbol,{"rsi14":round(live_value,2),"rsi14_live":round(live_value,2),
                "rsi14_closed":round(closed_value,2) if closed_value is not None else None,
                "period":14,"method":"Wilder","timeframe":"1d","includes_current_daily_candle":True,
                "bars_used":len(live),"received_at":utcnow().isoformat(),"source":"yahoo_1d"}
        except Exception:return symbol,None

async def daily_rsi_loop():
    await asyncio.sleep(75)
    headers={"User-Agent":"Mozilla/5.0 SnipeLab/0.7"}
    limits=httpx.Limits(max_connections=4,max_keepalive_connections=3)
    async with httpx.AsyncClient(timeout=10,follow_redirects=True,headers=headers,limits=limits) as client:
        while True:
            syms=sorted(UNIVERSE); sem=asyncio.Semaphore(3)
            for i in range(0,len(syms),10):
                rows=await asyncio.gather(*(fetch_daily_rsi(client,sem,s) for s in syms[i:i+10]))
                for s,row in rows:
                    if row is not None:DAILY[s]=row
                await asyncio.sleep(1)
            await asyncio.sleep(900)

def short_estimate(symbol):
    hist=BORROW_HISTORY.get(symbol) or []
    downs=[]
    for x in hist:
        try:
            old=float(x["old_available"]); new=float(x["available"]); price=float(x["price"])
        except Exception:continue
        if old>new and price>0:
            downs.append({"drop":old-new,"price":price,"available":new,"at":x.get("at")})
    if not downs:
        return {"status":"insufficient_data","message":"بيانات غير كافية","down_events":0,"short_avg_est":None,"zero_price_est":None,"confidence":"none"}
    total=sum(x["drop"] for x in downs)
    short_avg=sum(x["price"]*x["drop"] for x in downs)/total if total>0 else None
    zero=None; r2=None
    # Estimate price at Available=0 only from observed downward-borrow events. Require >=3 distinct observations.
    pts=[(x["available"],x["price"]) for x in downs]
    if len(pts)>=3 and len({a for a,_ in pts})>=3:
        ma=sum(a for a,_ in pts)/len(pts); mp=sum(p for _,p in pts)/len(pts)
        den=sum((a-ma)**2 for a,_ in pts)
        if den>0:
            beta=sum((a-ma)*(p-mp) for a,p in pts)/den
            alpha=mp-beta*ma
            pred=[alpha+beta*a for a,_ in pts]
            ss_res=sum((p-y)**2 for (_,p),y in zip(pts,pred)); ss_tot=sum((p-mp)**2 for _,p in pts)
            r2=1-(ss_res/ss_tot) if ss_tot>0 else 0
            prices=[p for _,p in pts]
            # Reject wild extrapolation: zero estimate must remain within a conservative band around observed prices.
            if alpha>0 and min(prices)*0.5<=alpha<=max(prices)*1.5 and r2>=0.35:zero=alpha
    conf="high" if len(downs)>=6 and r2 is not None and r2>=0.70 else "medium" if len(downs)>=4 and r2 is not None and r2>=0.50 else "low"
    status="ok" if zero is not None else "collecting"
    return {"status":status,"message":None if zero is not None else "بيانات غير كافية لتقدير سعر الصفر",
        "down_events":len(downs),"borrowed_observed":round(total,2),"short_avg_est":round(short_avg,4) if short_avg else None,
        "zero_price_est":round(zero,4) if zero else None,"fit_r2":round(r2,3) if r2 is not None else None,"confidence":conf,
        "method":"weighted price of observed Available drops + guarded linear extrapolation"}

async def delayed_market_start():
    await asyncio.sleep(30)
    await market_loop()

async def market_loop():
    # Scan small chunks so 255 symbols fit comfortably in the 256 MB sandbox.
    headers={"User-Agent":"Mozilla/5.0 QanasWatcher/0.3"}
    limits=httpx.Limits(max_connections=5,max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=8,follow_redirects=True,headers=headers,limits=limits) as client:
        while True:
            syms=sorted(UNIVERSE)
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
            STATE["last_market_error"]=None if ok else "no quotes returned"
            nxt=(cursor+len(batch)) % len(syms)
            if nxt <= cursor: STATE["market_cycle"]+=1
            STATE["market_cursor"]=nxt
            await asyncio.sleep(3)

def readiness_state(meta,q,b,a):
    price=float(q["price"]); live_low=float(q.get("day_low") or price)
    prior_low=a.get("post_split_low")
    new_low=prior_low is not None and live_low<float(prior_low)
    effective_low=live_low if new_low else (float(prior_low) if prior_low is not None else live_low)
    dist=((price/effective_low)-1)*100 if effective_low>0 else None
    sessions=0 if new_low else int(a.get("stability_sessions") or 0)
    market_day=str(q.get("market_timestamp") or "")[:10]
    last_day=a.get("last_market_day")
    if not new_low and prior_low is not None and market_day and market_day!=last_day:
        sessions=min(4,sessions+1)
    high=max(float(a.get("highest_since_split") or price),float(q.get("day_high") or price))
    half=high/2 if high>0 else None
    half_ok=bool(a.get("half_reached")) or (half is not None and effective_low<=half)
    av=b.get("available") if b else None
    price_ok=price>0; av_ok=av is not None and av<=20000
    dist_ok=dist is not None and dist<=10; sess_ok=sessions>=4
    missing=[]; close=True
    if not half_ok: missing.append(f"يحقق شرط النصف <= {half:.4f}" if half else "حساب مستوى النصف"); close=False
    if new_low: missing.append("كون قاع جديد اليوم: يبدأ الثبات من 0/4"); close=False
    if not av_ok:
        missing.append("Available ينزل إلى <=20K" if av is not None else "قراءة Available")
        close=close and av is not None and av<=20000
    if not dist_ok:
        missing.append(f"يرجع أقرب للقاع: الآن {dist:.2f}% والهدف <=10%" if dist is not None else "حساب البعد عن القاع")
        close=close and dist is not None and dist<=20
    if not sess_ok and not new_low:
        missing.append(f"{max(0,4-sessions)} جلسة ثبات إضافية للوصول إلى 4/4")
        close=close and sessions>=2
    if not price_ok: missing.append("تحديث السعر الحالي"); close=False
    full=price_ok and half_ok and not new_low and av_ok and dist_ok and sess_ok
    shortlist=full or (price_ok and half_ok and not new_low and close and 1<=len(missing)<=2)
    if av is None: ap=0
    elif av<=10000: ap=45
    elif av<=20000: ap=45-15*((av-10000)/10000)
    else: ap=0
    if dist is None: dp=0
    elif dist<=10: dp=30
    elif dist<=20: dp=30-15*((dist-10)/10)
    else: dp=0
    sp=25 if sessions>=4 else 19 if sessions==3 else 12 if sessions==2 else 6 if sessions==1 else 0
    pct=100.0 if full else round(min(99.0,ap+dp+sp),1)
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
        full=st["full"]; was_ready=bool(a.get("ready"))
        ready_at=a.get("ready_at"); ready_price=a.get("ready_price")
        if full and ready_at is None:
            ready_at=utcnow().isoformat(); ready_price=price
            add_event(sym,"ready","Entered ready list",{"price":price,"available":b.get("available") if b else None})
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
            "post_split_low":st["effective_low"],"highest_since_split":st["highest_since_split"],
            "half_level":st["half_level"],"half_reached":st["half_reached"],
            "distance_from_low_pct":round(st["effective_distance_pct"],2) if st["effective_distance_pct"] is not None else None,
            "stability_sessions":st["effective_sessions"],"effective_low":st["effective_low"],
            "effective_distance_pct":round(st["effective_distance_pct"],2) if st["effective_distance_pct"] is not None else None,
            "effective_sessions":st["effective_sessions"],"new_low_today":st["new_low_today"],
            "available":b.get("available") if b else None,"ctb":b.get("ctb") if b else None,"rebate":b.get("rebate") if b else None,
            "readiness_pct":st["readiness_pct"],"score":st["readiness_pct"],"ready":full,
            "near_ready":st["shortlist"] and not full and not launched,"shortlist":st["shortlist"] and not launched,
            "ready_candidate":(b is not None and b.get("available") is not None and b.get("available")<=20000 and st["effective_distance_pct"] is not None and st["effective_distance_pct"]<=10 and st["effective_sessions"]>=4 and not st["new_low_today"] and not launched),
            "missing_count":st["missing_count"],"missing":st["missing"],"strength":st["strength"],
            "ready_at":ready_at,"ready_price":ready_price,"launched":launched,
            "max_rise_pct":round(max_rise,2) if max_rise is not None else None,"rise_pct":round(max_rise,2) if max_rise is not None else None,
            "ignition":ignition,"last_market_day":st["market_day"],
            "daily_rsi":(DAILY.get(sym) or {}).get("rsi14"),"daily_rsi_meta":DAILY.get(sym),
            "short_estimate":short_estimate(sym)}

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
    await asyncio.sleep(150)
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
                if old:
                    oa,na=old.get("available"),new.get("available")
                    if oa is not None and na is not None and na<oa:
                        changed+=1
                        px=(QUOTES.get(sym) or {}).get("price")
                        if px is not None:
                            h=BORROW_HISTORY.setdefault(sym,[])
                            h.append({"at":now,"old_available":oa,"available":na,"price":float(px)})
                            if len(h)>120:del h[:-120]
                        add_event(sym,"available_down",f"Available {oa:g} -> {na:g}",{"old":oa,"new":na})
                    if oa!=0 and na==0:add_event(sym,"available_zero","Available reached 0",{"old":oa,"new":0})
                BORROW[sym]=new
            STATE["borrow_scan_count"]+=1; STATE["last_borrow_scan"]=now; STATE["borrow_ok"]=sum(1 for s in UNIVERSE if s in rows)
            STATE["borrow_missing"]=max(0,len(UNIVERSE)-STATE["borrow_ok"]); STATE["last_borrow_error"]=None
        except Exception as exc: STATE["last_borrow_error"]=f"{type(exc).__name__}: {str(exc)[:120]}"
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
    asyncio.create_task(heartbeat_loop()); asyncio.create_task(universe_loop()); asyncio.create_task(delayed_market_start()); asyncio.create_task(delayed_borrow_start()); asyncio.create_task(daily_rsi_loop()); asyncio.create_task(analytics_loop()); asyncio.create_task(halt_loop())

@app.get("/")
async def root():
    return {"service":"snipelab-engine","message":"SnipeLab Engine is alive","version":"0.7.0",**STATE,
        "prices_ready":len(QUOTES),"borrow_ready":len(BORROW),"events":len(EVENTS),
        "uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),
        "endpoints":["/dashboard","/health","/universe","/prices","/borrow","/snapshot","/signals","/ready","/zero-short","/momentum","/top","/halts","/news","/events"]}

DASHBOARD = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>SnipeLab</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f1e8;color:#13232a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,Arial;padding-bottom:78px}.w{max-width:760px;margin:auto;padding:12px}.head{background:#fffdf8;border:1px solid #e5dece;border-radius:20px;padding:14px 16px;box-shadow:0 8px 28px #25343b0d}.h1{display:flex;justify-content:space-between;align-items:center}.logo{font-size:24px;font-weight:900;letter-spacing:-.5px;color:#102c36}.logo i{font-style:normal;color:#a98543}.status{text-align:left;font-size:11px;color:#69777a}.live{color:#2f8b66;font-weight:800}.uptime{font-size:13px;font-weight:900;color:#102c36;margin-top:3px}.toolbar{display:flex;align-items:center;justify-content:space-between;margin:15px 1px 9px}.title{font-size:15px;font-weight:900}.sub{font-size:10px;color:#7a8586;margin-top:2px}.filterBtn{border:1px solid #d8cdb9;background:#fffdf8;color:#17343d;border-radius:12px;padding:9px 12px;font-weight:800}.chips{display:flex;gap:6px;overflow:auto;padding:0 0 9px;scrollbar-width:none}.chip{font-size:10px;background:#eee7da;border:1px solid #dfd4c1;border-radius:999px;padding:6px 9px;white-space:nowrap;color:#5e696a}.card{background:#fffdf8;border:1px solid #e4dccd;border-radius:18px;padding:13px;margin-bottom:10px;box-shadow:0 6px 22px #24343b0b}.top{display:flex;justify-content:space-between;align-items:flex-start}.sym{font-size:19px;font-weight:950;color:#102c36}.meta{font-size:9px;color:#7a8586;margin-top:2px}.price{text-align:left;font-weight:900}.ready{font-size:9px;color:#2f8b66;background:#e8f4ed;border-radius:999px;padding:4px 7px;display:inline-block;margin-top:4px}.primary{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:12px}.metric{background:#f5f1e8;border-radius:12px;padding:9px;text-align:center}.metric small,.secondary small{display:block;color:#7b8585;font-size:8px;margin-bottom:4px}.metric b{font-size:13px;color:#102c36}.metric.hot b{color:#9b6a24}.secondary{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-top:7px}.secondary div{text-align:center;padding:6px 2px}.secondary b{font-size:10px}.retest{margin-top:8px;border-radius:10px;padding:8px 10px;background:#edf5ef;color:#287354;font-size:10px;font-weight:900;display:flex;justify-content:space-between}.missing{margin-top:7px;font-size:9px;color:#8a6331;background:#faf2df;border-radius:9px;padding:7px 9px}.empty{text-align:center;color:#7d8788;padding:35px}.drawer{display:none;background:#fffdf8;border:1px solid #dfd5c3;border-radius:18px;padding:12px;margin-bottom:10px}.drawer.open{display:block}.fg{margin-bottom:12px}.fg h4{margin:0 0 6px;font-size:11px;color:#203b43}.opts{display:flex;gap:5px;flex-wrap:wrap}.opt{border:1px solid #ddd2bf;background:#f7f3eb;border-radius:9px;padding:6px 8px;font-size:9px}.opt.on{background:#173943;color:#fff;border-color:#173943}.clear{border:0;background:none;color:#9a7137;font-size:10px}.nav{position:fixed;bottom:0;left:0;right:0;background:#fffdf8f2;border-top:1px solid #ddd4c5;backdrop-filter:blur(12px)}.navin{max-width:760px;margin:auto;display:grid;grid-template-columns:repeat(4,1fr)}.nav button{border:0;background:none;padding:11px 3px;color:#8a9392;font-size:9px}.nav button b{display:block;font-size:16px;margin-bottom:2px}.nav button.on{color:#173943;font-weight:900}@media(min-width:650px){#cards{display:grid;grid-template-columns:1fr 1fr;gap:10px}.card{margin:0}}
</style></head><body><div class="w"><header class="head"><div class="h1"><div class="logo">Snipe<span style="color:#a98543">Lab</span></div><div class="status"><div id="server" class="live">● Server Live</div><div id="uptime" class="uptime">0:00:00</div></div></div></header>
<div class="toolbar"><div><div class="title">أسهم التقسيم</div><div id="count" class="sub">جاري تحميل البيانات...</div></div><button id="filterBtn" class="filterBtn">☷ الفلاتر</button></div><div id="active" class="chips"></div><div id="drawer" class="drawer"></div><div id="cards"></div></div>
<div class="nav"><div class="navin"><button class="on"><b>◈</b>أسهم التقسيم</button><button><b>◉</b>مركز الأحداث</button><button><b>▣</b>الأرشيف</button><button><b>✦</b>Snipe AI</button></div></div>
<script>
let d={},bootBase=0,bootSeen=Date.now(),F={rsi:null,av:null,stab:null,retest:null,float:null,cap:null,price:null};const N=v=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:1}),P=v=>v==null?'—':Number(v).toFixed(1)+'%',D=v=>v==null?'—':'$'+Number(v).toFixed(Number(v)<1?4:2),K=v=>v==null?'—':Number(v)>=1e9?(Number(v)/1e9).toFixed(1)+'B':Number(v)>=1e6?(Number(v)/1e6).toFixed(1)+'M':Number(v)>=1e3?(Number(v)/1e3).toFixed(0)+'K':N(v);
const groups=[['rsi','RSI',[40,35,30,25,20,15]],['av','Available',[40000,30000,25000,20000,15000,10000,0]],['stab','الثبات',[2,3,4]],['retest','إعادة اختبار الدعم',['ناجح','فشل']],['float','Free Float',[2000000,1000000,500000]],['cap','Market Cap',['small','micro']],['price','Last Stock Price',[10,5,3,1.5]]];
function lab(k,v){if(k==='rsi')return v===15?'RSI < 15':'RSI < '+v;if(k==='av')return v===0?'Available = 0 🔥':'Available < '+K(v);if(k==='stab')return v+'/4';if(k==='float')return 'Float < '+K(v);if(k==='cap')return v==='small'?'Small Cap 300M–2B':'Micro Cap <300M';if(k==='price')return 'Price < $'+v;return v}
function buildFilters(){drawer.innerHTML=groups.map(g=>'<div class="fg"><h4>'+g[1]+'</h4><div class="opts">'+g[2].map(v=>'<button class="opt '+(String(F[g[0]])===String(v)?'on':'')+'" data-k="'+g[0]+'" data-v="'+v+'">'+lab(g[0],v)+'</button>').join('')+'</div></div>').join('')+'<button class="clear">مسح الكل</button>';drawer.querySelectorAll('.opt').forEach(b=>b.onclick=()=>{let k=b.dataset.k,v=b.dataset.v;if(['rsi','av','stab','float','price'].includes(k))v=Number(v);F[k]=String(F[k])===String(v)?null:v;buildFilters();render()});drawer.querySelector('.clear').onclick=()=>{Object.keys(F).forEach(k=>F[k]=null);buildFilters();render()}}
function rows(){return Object.values(d.rows||{}).map(x=>({...x,...(x.signal||{}),q:x.price||{},br:x.borrow||{}}))}
function passes(x){let av=x.br.available??x.available,px=x.q.price??x.price,r=x.daily_rsi,st=x.effective_sessions??0,ff=x.free_float??x.float,mc=x.market_cap;if(F.rsi!=null&&(r==null||!(r<F.rsi)))return false;if(F.av!=null&&(av==null||(F.av===0?Number(av)!==0:!(av<F.av))))return false;if(F.stab!=null&&st<F.stab)return false;if(F.price!=null&&(px==null||!(px<F.price)))return false;if(F.float!=null&&(ff==null||!(ff<F.float)))return false;if(F.cap==='micro'&&(mc==null||mc>=3e8))return false;if(F.cap==='small'&&(mc==null||mc<3e8||mc>2e9))return false;if(F.retest&&x.support_retest_status&&x.support_retest_status!==F.retest)return false;return true}
function card(x){let av=x.br.available??x.available,px=x.q.price??x.price,sup=x.support??x.effective_low,dist=x.support_distance_pct??x.effective_distance_pct,ff=x.free_float??x.float,mc=x.market_cap,rs=x.support_retest_status||'بانتظار الاختبار',ok=rs==='ناجح';return '<div class="card"><div class="top"><div><div class="sym">🇺🇸 '+x.symbol+'</div><div class="meta">NASDAQ · Reverse Split '+(x.effective_date||'—')+'</div></div><div class="price">'+D(px)+'<br><span class="ready">'+N(x.readiness_pct)+'% جاهزية</span></div></div><div class="primary"><div class="metric hot"><small>Available</small><b>'+K(av)+'</b></div><div class="metric"><small>الدعم التاريخي</small><b>'+D(sup)+'</b></div><div class="metric"><small>بعده عن الدعم</small><b>'+P(dist)+'</b></div></div><div class="secondary"><div><small>RSI Daily</small><b>'+N(x.daily_rsi)+'</b></div><div><small>Free Float</small><b>'+K(ff)+'</b></div><div><small>Market Cap</small><b>'+K(mc)+'</b></div><div><small>الثبات</small><b>'+(x.effective_sessions??0)+'/4</b></div></div><div class="retest" style="'+(!ok&&rs==='فشل'?'background:#f8e9e6;color:#a84d43':'')+'"><span>'+(ok?'✓':rs==='فشل'?'✕':'○')+' إعادة اختبار الدعم: '+rs+'</span><span>'+(x.support_age_sessions!=null?'منذ '+x.support_age_sessions+' جلسات':'')+'</span></div>'+(x.missing_count===1?'<div class="missing">👀 باقي شرط واحد: '+((x.missing||[])[0]||'—')+'</div>':'')+'</div>'}
function render(){let a=rows().filter(passes).sort((a,b)=>(Number(b.readiness_pct||0)-Number(a.readiness_pct||0))||(Number(a.br.available??a.available??1e15)-Number(b.br.available??b.available??1e15)));count.textContent=a.length+' سهم مطابق';cards.innerHTML=a.map(card).join('')||'<div class="empty">لا توجد أسهم مطابقة للفلاتر الحالية</div>';let ac=Object.entries(F).filter(x=>x[1]!=null);active.innerHTML=ac.map(([k,v])=>'<span class="chip">'+lab(k,v)+'</span>').join('')}
async function load(){try{let r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error(r.status);d=await r.json();bootBase=Number(d.uptime_seconds||0);bootSeen=Date.now();server.textContent='● Server Live';server.className='live';render()}catch(e){server.textContent='● Server Offline';server.className='';}}function tick(){let s=bootBase+Math.floor((Date.now()-bootSeen)/1000),h=Math.floor(s/3600),m=Math.floor(s%3600/60),q=s%60;uptime.textContent=h+':'+String(m).padStart(2,'0')+':'+String(q).padStart(2,'0')}filterBtn.onclick=()=>drawer.classList.toggle('open');buildFilters();load();tick();setInterval(tick,1000);setInterval(load,10000);
</script></body></html>"""

@app.get("/dashboard",response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)

@app.get("/health")
async def health():
    last=STATE["heartbeat"]; age=(utcnow()-datetime.fromisoformat(last)).total_seconds() if last else None
    return {"ok":bool(last) and age<30,"heartbeat_age_seconds":age,**STATE}

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

@app.get("/short-estimates")
async def short_estimates():
    rows={s:{"symbol":s,"available":(BORROW.get(s) or {}).get("available"),"rsi_daily":DAILY.get(s),"estimate":short_estimate(s),"history_points":len(BORROW_HISTORY.get(s) or [])} for s in UNIVERSE}
    return {"generated_at":utcnow().isoformat(),"rows":rows}

@app.get("/momentum")
async def momentum():
    rows=[x for x in ANALYTICS.values() if (x.get("ignition") or {}).get("fresh")]
    rows.sort(key=lambda x:(x.get("ignition") or {}).get("pct",0),reverse=True)
    return {"count":len(rows),"rows":rows}

@app.get("/top")
async def top():
    rows=[x for x in ANALYTICS.values() if x.get("launched")]
    rows.sort(key=lambda x:x.get("max_rise_pct") or 0,reverse=True)
    return {"count":len(rows),"rows":rows}

@app.get("/api/dashboard")
async def dashboard_data():
    rows={}
    for sym,meta in UNIVERSE.items():
        rows[sym]={"symbol":sym,"effective_date":meta.get("effective_date"),"price":QUOTES.get(sym),"borrow":BORROW.get(sym),"signal":ANALYTICS.get(sym)}
    return {"server_time":utcnow().isoformat(),"uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),"health":{"ok":STATE.get("status") in ("ok","running"),"heartbeat":STATE.get("heartbeat"),"universe_count":len(UNIVERSE),"price_count":len(QUOTES),"borrow_count":len(BORROW),"analytics_count":len(ANALYTICS)},"rows":rows,"events":EVENTS[-40:][::-1],"halts":HALTS,"news":NEWS}

@app.get("/halts")
async def halts():
    return {"last_scan":STATE["last_halt_scan"],"error":STATE["halt_error"],"count":len(HALTS),"rows":HALTS}

@app.get("/news")
async def news():
    return {"last_scan":STATE["last_news_scan"],"error":STATE["news_error"],"count":len(NEWS),"rows":NEWS}

@app.get("/events")
async def events():
    return {"count":len(EVENTS),"events":EVENTS}
