from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
import asyncio
import time
import httpx

def calculate(effective, candles):
    """Daily Yahoo OHLC; high must occur on/after low for ten-session TOP."""
    bars=sorted((b for b in candles if b["date"]>=effective and b["low"]>0 and b["high"]>=b["low"]),key=lambda b:b["date"])
    if not bars:return {"verified":False,"error":"Missing post-split bars"}
    first=bars[0]; gap=(date.fromisoformat(first["date"])-date.fromisoformat(effective)).days
    verified=0<=gap<=4
    ny_today=datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    completed=[b for b in bars if b["date"]<ny_today]
    ten=completed[-10:]
    top=None
    if len(ten)==10:
        for i,lo in enumerate(ten):
            for hi in ten[i:]:
                rise=(hi["high"]/lo["low"]-1)*100
                if top is None or rise>top["top_10_gain_pct"]:
                    top={"top_10_gain_pct":round(rise,2),"top_10_low":lo["low"],"top_10_high":hi["high"],"top_10_low_date":lo["date"],"top_10_high_date":hi["date"]}
    closes=[b["close"] for b in completed]
    rsi=None
    if len(closes)>=15:
        diff=[closes[i]-closes[i-1] for i in range(len(closes)-14,len(closes))]
        gain=sum(max(x,0) for x in diff)/14
        loss=sum(max(-x,0) for x in diff)/14
        rsi=round(100 if loss==0 else 100-100/(1+gain/loss),2)
    return {"verified":verified,"source":"Yahoo 1d; split adjustment requires validation",
        "post_split_low":min(b["low"] for b in bars) if verified else None,
        "post_split_high":max(b["high"] for b in bars) if verified else None,
        "split_day_high":first["high"] if verified else None,
        "split_day_open":first["open"] if verified else None,
        "rsi_daily":rsi,"first_bar":first["date"],"bar_count":len(bars),
        "top_10_verified":bool(top and verified),**(top or {}),
        "updated_at":datetime.now(timezone.utc).isoformat()}

async def worker(universe,history,yahoo,save):
    await asyncio.sleep(80)
    async with httpx.AsyncClient(timeout=15,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}) as client:
        while True:
            today=datetime.now(timezone.utc).date().isoformat()
            todo=[(s,m) for s,m in universe.items() if m.get("effective_date") and m["effective_date"]<=today]
            todo.sort(key=lambda z:history.get(z[0],{}).get("updated_at",""))
            for sym,meta in todo[:6]:
                try:
                    eff=meta["effective_date"]
                    start=int(datetime.combine(date.fromisoformat(eff),datetime.min.time(),timezone.utc).timestamp())
                    r=await client.get(yahoo.format(symbol=sym),params={"period1":start,"period2":int(time.time())+86400,"interval":"1d","events":"history"})
                    r.raise_for_status()
                    result=(r.json().get("chart",{}).get("result") or [None])[0]
                    if not result:continue
                    q=((result.get("indicators") or {}).get("quote") or [{}])[0]
                    bars=[]
                    for i,t in enumerate(result.get("timestamp") or []):
                        try:
                            v={k:float(q[k][i]) for k in ("open","high","low","close")}
                            if min(v.values())<=0:continue
                            bars.append({"date":datetime.fromtimestamp(t,timezone.utc).date().isoformat(),**v})
                        except (IndexError,TypeError,ValueError,KeyError):continue
                    history[sym]=calculate(eff,bars)
                except Exception as exc:
                    if sym not in history:history[sym]={"verified":False,"error":str(exc)[:100]}
                await asyncio.sleep(1)
            save(force=True)
            await asyncio.sleep(30)
