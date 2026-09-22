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

app = FastAPI(title="SnipeLab Engine", version="0.6.0")
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
STATE_FILE = Path(os.environ.get("SNIPELAB_STATE_FILE","/tmp/snipelab_state.json"))
_LAST_SAVE = 0.0

def load_persistent_state():
    try:
        if not STATE_FILE.exists(): return
        d=json.loads(STATE_FILE.read_text("utf-8"))
        ANALYTICS.update(d.get("analytics") or {})
        BORROW.update(d.get("borrow") or {})
        EVENTS.extend((d.get("events") or [])[:100])
    except Exception as exc:
        STATE["persistence_error"]=f"load {type(exc).__name__}: {str(exc)[:100]}"

def save_persistent_state(force=False):
    global _LAST_SAVE
    now=time.time()
    if not force and now-_LAST_SAVE<60:return
    try:
        tmp=STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at":utcnow().isoformat(),"analytics":ANALYTICS,"borrow":BORROW,"events":EVENTS[:100]},separators=(",",":")),"utf-8")
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
            if eff < date(2026,5,1) or eff > date(2026,12,30): continue
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
    # Never fetch the legacy Qanas service: SnipeLab is fully independent.
    # Keep the last successfully synced universe when the public feed fails.
    # Render can be slow to wake up. Never leave the watcher empty while it retries.
    if not UNIVERSE:
        UNIVERSE.update({s:{"symbol":s,"effective_date":None,"source":"seed"} for s in UNIVERSE_SEED})
        STATE["universe_count"]=len(UNIVERSE)
    STATE["universe_error"]=last_error or "StockAnalysis feed unavailable"; STATE["universe_source"]="seed" if STATE["last_universe_sync"] is None else STATE["universe_source"]

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
                        changed+=1; add_event(sym,"available_down",f"Available {oa:g} -> {na:g}",{"old":oa,"new":na})
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
    asyncio.create_task(heartbeat_loop()); asyncio.create_task(universe_loop()); asyncio.create_task(delayed_market_start()); asyncio.create_task(delayed_borrow_start()); asyncio.create_task(analytics_loop()); asyncio.create_task(halt_loop())

@app.get("/")
async def root():
    return {"service":"snipelab-engine","message":"SnipeLab Engine is alive","version":"0.6.0",**STATE,
        "prices_ready":len(QUOTES),"borrow_ready":len(BORROW),"events":len(EVENTS),
        "uptime_seconds":int(time.time()-BOOTED_AT.timestamp()),
        "endpoints":["/dashboard","/health","/universe","/prices","/borrow","/snapshot","/signals","/ready","/zero-short","/momentum","/top","/halts","/news","/events"]}

DASHBOARD = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>SnipeLab</title><style>
*{box-sizing:border-box}body{margin:0;background:#05090c;color:#edf2f1;font-family:Tahoma,Arial;padding-bottom:66px}.w{max-width:760px;margin:auto;padding:10px}.head{background:linear-gradient(145deg,#0b1418,#081014);border:1px solid #17272d;border-radius:13px;padding:10px 12px;margin-bottom:8px}.h1{display:flex;justify-content:space-between;align-items:center}.logo{font-size:24px;font-weight:900}.logo i{font-style:normal;color:#39dfa0}.clock{text-align:left;font-size:11px;color:#91a0a4}.server{font-size:10px;color:#39dfa0;margin-top:3px}.marks{display:flex;gap:5px;margin-top:8px}.mark{font-size:9px;background:#0d1b20;border:1px solid #173138;border-radius:6px;padding:4px 6px;color:#8da0a4}.mark.ok{color:#4be2a5}.title{font-size:11px;color:#75868a;margin:10px 2px 6px}.filters{display:flex;gap:5px;overflow:auto;padding-bottom:7px;scrollbar-width:none}.f{border:1px solid #1b2b31;background:#0a1216;color:#9aabad;border-radius:7px;padding:6px 9px;font-size:10px;white-space:nowrap}.f.on{color:#58e4a7;border-color:#286148;background:#10251d}.topdeck{display:flex;gap:7px;overflow:auto;padding:2px 0 6px}.hero{min-width:185px;background:linear-gradient(135deg,#17140c,#0b1114);border:1px solid #59471e;border-radius:11px;padding:9px}.hero .rise{color:#f1c765;font-weight:900;font-size:15px}.card{background:#0a1115;border:1px solid #16242a;border-radius:9px;padding:8px 10px;margin-bottom:6px}.top{display:flex;justify-content:space-between;align-items:center}.sym{font-size:16px;font-weight:900}.score{font-weight:900;color:#50e2a4}.tag{font-size:8px;padding:3px 5px;border-radius:5px;background:#132228;color:#8da1a5;margin-right:4px}.gold{color:#f0c568}.red{color:#ff8177}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-top:7px}.kv small{display:block;color:#62757a;font-size:8px;margin-bottom:2px}.kv b{font-size:10.5px}.low b{color:#58e4a7}.more{display:none;border-top:1px solid #17252a;margin-top:7px;padding-top:7px}.card.open .more{display:grid}.empty{color:#65777b;text-align:center;padding:25px}.events .card{font-size:11px}.time{font-size:9px;color:#65777b}.nav{position:fixed;bottom:0;left:0;right:0;background:#080e11f5;border-top:1px solid #17242a}.navin{max-width:760px;margin:auto;display:grid;grid-template-columns:1fr 1fr}.nav button{background:none;border:0;color:#6d7d81;padding:12px}.nav button.on{color:#52e1a5;font-weight:900}@media(min-width:650px){#cards{display:grid;grid-template-columns:1fr 1fr;gap:6px}.card{margin:0}.events .card{margin-bottom:6px}}</style></head><body><div class="w"><header class="head"><div class="h1"><div class="logo">◈ SnipeLab</div><div class="clock"><b id="clock">--:--:--</b><div id="date"></div></div></div><div id="server" class="server">● المحرك...</div><div class="marks"><span id="mkt" class="mark">● الأسعار</span><span id="ibkr" class="mark">● IBKR</span><span id="sig" class="mark">● الجاهزية</span><span id="haltm" class="mark">● HALT</span></div></header>
<section id="radar"><div id="topbox"></div><div class="title">🎯 الجاهزية · Available ≤ 20K · قريب من القاع · ثبات 4/4</div><div class="filters"><button class="f on" data-f="ready">الجاهزية</button><button class="f" data-f="all">الكل</button><button class="f" data-f="zero">0 شورت</button><button class="f" data-f="momentum">⚡ لحظي</button><button class="f" data-f="top">👑 TOP</button></div><div id="count" class="title"></div><div id="cards"></div></section><section id="pulse" style="display:none" class="events"><div class="title">🔔 الSnipeLab · الأحداث وHALT والأخبار</div><div id="events"></div></section></div><div class="nav"><div class="navin"><button class="on" data-p="radar">🎯 الرادار</button><button data-p="pulse">🔔 الSnipeLab</button></div></div><script>
let d={},filter='ready';const N=v=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:1}),P=v=>v==null?'—':Number(v).toFixed(1)+'%',D=v=>v==null?'—':'$'+Number(v).toFixed(Number(v)<1?4:2);async function J(){let r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error(r.status);return r.json()}function rows(){return Object.values(d.rows||{}).map(x=>({...x,...(x.signal||{}),q:x.price||{},br:x.borrow||{}}))}
function pick(){let a=rows();if(filter==='ready')a=a.filter(x=>x.ready_candidate||x.ready);if(filter==='zero')a=a.filter(x=>Number(x.br.available??x.available)===0);if(filter==='momentum')a=a.filter(x=>x.ignition?.fresh);if(filter==='top')a=a.filter(x=>x.launched);return a.sort((a,b)=>filter==='ready'?(Number(a.br.available??a.available??1e12)-Number(b.br.available??b.available??1e12)):(Number(b.readiness_pct||0)-Number(a.readiness_pct||0)))}
function card(x){let av=x.br.available??x.available,pr=x.q.price??x.price;return '<div class="card" onclick="this.classList.toggle(\'open\')"><div class="top"><div><span class="sym">'+x.symbol+'</span>'+(x.ready?'<span class="tag">جاهز</span>':x.ready_candidate?'<span class="tag gold">مرشح</span>':'')+'</div><div><b>'+D(pr)+'</b> <span class="score">'+N(x.readiness_pct)+'%</span></div></div><div class="grid"><div class="kv"><small>Available</small><b>'+N(av)+'</b></div><div class="kv low"><small>أدنى قاع</small><b>'+D(x.effective_low)+'</b></div><div class="kv"><small>البعد عن القاع</small><b>'+P(x.effective_distance_pct)+'</b></div><div class="kv"><small>الثبات</small><b>'+(x.effective_sessions??0)+'/4</b></div></div><div class="more grid"><div class="kv"><small>Rebate</small><b>'+P(x.br.rebate??x.rebate)+'</b></div><div class="kv"><small>CTB</small><b>'+P(x.br.ctb??x.ctb)+'</b></div><div class="kv"><small>النصف</small><b>'+(x.half_reached?'✓ ':'')+D(x.half_level)+'</b></div><div class="kv"><small>أعلى بعد التقسيم</small><b>'+D(x.highest_since_split)+'</b></div><div class="kv"><small>تاريخ التقسيم</small><b>'+(x.effective_date||'—')+'</b></div><div class="kv"><small>Ready Price</small><b>'+D(x.ready_price)+'</b></div></div></div>'}
function render(){let tops=rows().filter(x=>x.launched).sort((a,b)=>Number(b.max_rise_pct||0)-Number(a.max_rise_pct||0));topbox.innerHTML=tops.length?'<div class="title">👑 TOP · الأسهم المنطلقة</div><div class="topdeck">'+tops.map(x=>'<div class="hero"><div class="top"><b>'+x.symbol+'</b><span>👑</span></div><div class="rise">+'+N(x.max_rise_pct)+'%</div><small>من سعر الجاهزية '+D(x.ready_price)+'</small></div>').join('')+'</div>':'';let a=pick();count.textContent=a.length+' سهم';cards.innerHTML=a.map(card).join('')||'<div class="empty">لا توجد أسهم مطابقة حاليًا</div>';let mix=[...(d.events||[])];Object.values(d.halts||{}).forEach(h=>mix.push({symbol:h.symbol,text:'🚨 HALT · '+h.reason,at:(h.halt_date||'')+' '+(h.halt_time||'')}));Object.entries(d.news||{}).forEach(([s,z])=>(z||[]).filter(n=>n.tone==='positive').forEach(n=>mix.push({symbol:s,text:'🟢 '+n.title,at:n.published_at||''})));events.innerHTML=mix.slice(0,40).map(e=>'<div class="card"><div class="top"><b>'+e.symbol+'</b><span class="time">'+String(e.at||'').replace('T',' ').slice(0,19)+'</span></div><div>'+e.text+'</div></div>').join('')||'<div class="empty">لا توجد أحداث</div>'}
function clocker(){let n=new Date();clock.textContent=n.toLocaleTimeString('ar-SA');date.textContent=n.toLocaleDateString('ar-SA',{weekday:'short',year:'numeric',month:'short',day:'numeric'})}async function load(){try{d=await J();let h=d.health||{},age=h.heartbeat?Math.max(0,(Date.now()-Date.parse(h.heartbeat))/1000):999;server.textContent='● السيرفر شغال · آخر SnipeLabة '+Math.round(age)+'ث';server.style.color=age<30?'#39dfa0':'#ff8177';mkt.classList.toggle('ok',h.price_count>0);ibkr.classList.toggle('ok',h.borrow_count>0);sig.classList.toggle('ok',h.analytics_count>0);haltm.classList.toggle('ok',true);render()}catch(e){server.textContent='● تعذر الاتصال';server.style.color='#ff8177'}}document.querySelectorAll('.f').forEach(b=>b.onclick=()=>{document.querySelectorAll('.f').forEach(z=>z.classList.remove('on'));b.classList.add('on');filter=b.dataset.f;render()});document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button').forEach(z=>z.classList.remove('on'));b.classList.add('on');radar.style.display=b.dataset.p==='radar'?'block':'none';pulse.style.display=b.dataset.p==='pulse'?'block':'none'});clocker();setInterval(clocker,1000);load();setInterval(load,10000)</script></body></html>"""

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
    return {"server_time":utcnow().isoformat(),"health":{"ok":STATE.get("status")=="running","heartbeat":STATE.get("heartbeat"),"universe_count":len(UNIVERSE),"price_count":len(QUOTES),"borrow_count":len(BORROW),"analytics_count":len(ANALYTICS)},"rows":rows,"events":EVENTS[:40],"halts":HALTS,"news":NEWS}

@app.get("/halts")
async def halts():
    return {"last_scan":STATE["last_halt_scan"],"error":STATE["halt_error"],"count":len(HALTS),"rows":HALTS}

@app.get("/news")
async def news():
    return {"last_scan":STATE["last_news_scan"],"error":STATE["news_error"],"count":len(NEWS),"rows":NEWS}

@app.get("/events")
async def events():
    return {"count":len(EVENTS),"events":EVENTS}
