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
        "effective_date":effective,
        "post_split_low":min(b["low"] for b in bars) if verified else None,
        "post_split_high":max(b["high"] for b in bars) if verified else None,
        "split_day_high":first["high"] if verified else None,
        "split_day_open":first["open"] if verified else None,
        "post_split_high_date":max(bars,key=lambda b:b["high"])["date"] if verified else None,
        "post_split_low_date":min(bars,key=lambda b:b["low"])["date"] if verified else None,
        "rsi_daily":rsi,"first_bar":first["date"],"bar_count":len(bars),
        "top_10_verified":bool(top and verified),**(top or {}),
        "updated_at":datetime.now(timezone.utc).isoformat()}

async def split_day_4h_high(client, yahoo, symbol, effective):
    """Extended-hours hourly extrema from latest split through today.

    Aggregate 60m Yahoo bars into 4H buckets anchored at 04:00 ET.
    The day-one 4H high and period extrema use the same intraday source.
    Do not claim complete extended-hours coverage beyond Yahoo's retention.
    """
    ny=ZoneInfo("America/New_York")
    # Yahoo 60m often exposes a longer window than 1m/15m; request it and
    # report upstream rejection explicitly instead of hiding older split dates.
    if (datetime.now(ny).date()-date.fromisoformat(effective)).days>729:
        return {"split_day_4h_high":None,"split_day_4h_status":"intraday_history_out_of_range",
                "extended_history_complete":False}
    start=int(datetime.combine(date.fromisoformat(effective),datetime.min.time(),timezone.utc).timestamp())-86400
    r=await client.get(yahoo.format(symbol=symbol),params={
        "period1":start,"period2":int(time.time())+86400,
        "interval":"60m","includePrePost":"true"})
    r.raise_for_status()
    data=(r.json().get("chart",{}).get("result") or [None])[0]
    if not data:return {"split_day_4h_high":None,"split_day_4h_status":"no_intraday_bars","extended_history_complete":False}
    tz=ZoneInfo((data.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York")
    q=((data.get("indicators") or {}).get("quote") or [{}])[0]
    buckets={}; period_high=None;period_low=None;first_day=False;dates=set();high_date=None;low_date=None
    for i,t in enumerate(data.get("timestamp") or []):
        local=datetime.fromtimestamp(t,tz)
        day=local.date().isoformat()
        if day<effective or local.hour<4 or local.hour>=20:continue
        try:
            high=float(q["high"][i]);low=float(q["low"][i])
            if low<=0 or high<low:continue
        except (IndexError,TypeError,ValueError,KeyError):continue
        dates.add(day)
        if period_high is None or high>period_high:period_high=high;high_date=day
        if period_low is None or low<period_low:period_low=low;low_date=day
        if day==effective:
            first_day=True
            bucket=(local.hour-4)//4
            buckets[bucket]=max(high,buckets.get(bucket,0))
    if not buckets:
        return {"split_day_4h_high":None,"split_day_4h_status":"no_split_day_intraday_bars",
                "extended_history_complete":False}
    return {"split_day_4h_high":max(buckets.values()),
            "split_day_4h_status":"yahoo_60m_aggregated_extended_4h",
            "split_day_4h_candles":len(buckets),
            "extended_post_split_high":period_high,"extended_post_split_low":period_low,
            "extended_post_split_high_date":high_date,"extended_post_split_low_date":low_date,
            "extended_history_first_date":min(dates),"extended_history_last_date":max(dates),
            "extended_history_complete":False}

async def worker(universe,history,yahoo,save):
    await asyncio.sleep(8)
    async with httpx.AsyncClient(timeout=15,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}) as client:
        while True:
            today=datetime.now(timezone.utc).date().isoformat()
            todo=[(s,m) for s,m in universe.items() if m.get("effective_date") and m["effective_date"]<=today]
            # Backfill only incomplete records. Never re-fetch an already
            # complete ticker unless a NEW effective split date supersedes it.
            def complete(h,meta):
                return (h.get("effective_date")==meta["effective_date"]
                    and h.get("verified") is True
                    and all(h.get(k) is not None for k in
                        ("split_day_open","split_day_4h_high",
                         "post_split_high","post_split_low")))
            todo=[(sym,meta) for sym,meta in todo
                  if not complete(history.get(sym,{}),meta)]
            # Retry failures after 30 minutes, not continuously.
            now_epoch=time.time()
            def retry_due(sym,meta):
                h=history.get(sym,{})
                if h.get("effective_date")!=meta["effective_date"]:return True
                try:
                    age=now_epoch-datetime.fromisoformat(h["attempted_at"]).timestamp()
                except (ValueError,KeyError,TypeError):return True
                return age>=1800
            todo=[(sym,meta) for sym,meta in todo if retry_due(sym,meta)]
            todo.sort(key=lambda item:(
                -date.fromisoformat(item[1]["effective_date"]).toordinal(),
                history.get(item[0],{}).get("attempted_at","")))
            for sym,meta in todo[:16]:
                try:
                    eff=meta["effective_date"]
                    start=int(datetime.combine(date.fromisoformat(eff),datetime.min.time(),timezone.utc).timestamp())-86400
                    r=await client.get(yahoo.format(symbol=sym),params={"period1":start,"period2":int(time.time())+86400,"interval":"1d","events":"history"})
                    r.raise_for_status()
                    result=(r.json().get("chart",{}).get("result") or [None])[0]
                    if not result:raise ValueError("Yahoo returned no chart result")
                    q=((result.get("indicators") or {}).get("quote") or [{}])[0]
                    bars=[]
                    tz=ZoneInfo((result.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York")
                    for i,t in enumerate(result.get("timestamp") or []):
                        try:
                            v={k:float(q[k][i]) for k in ("open","high","low","close")}
                            if min(v.values())<=0:continue
                            bars.append({"date":datetime.fromtimestamp(t,tz).date().isoformat(),**v})
                        except (IndexError,TypeError,ValueError,KeyError):continue
                    result_data=calculate(eff,bars)
                    try:
                        result_data.update(await split_day_4h_high(client,yahoo,sym,eff))
                        if result_data.get("verified") and result_data.get("split_day_4h_high") is not None:
                            # A post-split maximum cannot be below the split-day 4H high.
                            daily_high=result_data["post_split_high"]
                            extended_high=result_data.get("extended_post_split_high")
                            if extended_high is not None and extended_high>daily_high:
                                result_data["post_split_high_date"]=result_data.get("extended_post_split_high_date")
                            result_data["post_split_high"]=max(daily_high,result_data["split_day_4h_high"],extended_high or 0)
                            if result_data.get("extended_post_split_low") is not None:
                                if result_data["extended_post_split_low"]<result_data["post_split_low"]:
                                    result_data["post_split_low_date"]=result_data.get("extended_post_split_low_date")
                                result_data["post_split_low"]=min(result_data["post_split_low"],result_data["extended_post_split_low"])
                            result_data["extrema_source"]="Yahoo daily plus available extended-hours 60m"
                            result_data["split_adjustment_requires_validation"]=True
                    except Exception as exc:
                        result_data.update({"split_day_4h_high":None,"split_day_4h_status":"fetch_error:"+type(exc).__name__})
                    result_data["quality_warnings"]=[]
                    if result_data.get("verified"):
                        hi=result_data.get("post_split_high");lo=result_data.get("post_split_low")
                        if not hi or not lo or hi<lo:result_data["quality_warnings"].append("invalid_extrema")
                        if result_data.get("split_day_4h_high") is None:result_data["quality_warnings"].append("4h_unavailable")
                        if result_data.get("top_10_verified") and (result_data["top_10_low"]<lo-1e-5 or result_data["top_10_high"]>hi+1e-5):
                            result_data["quality_warnings"].append("rolling_extrema_inconsistent")
                            result_data["top_10_verified"]=False
                        if result_data.get("split_adjustment_requires_validation"):
                            result_data["quality_warnings"].append("split_adjustment_unverified")
                    if not result_data.get("verified") and not result_data.get("error"):
                        result_data["error"]="First post-split daily bar could not be validated"
                    if universe.get(sym,{}).get("effective_date")!=eff:continue
                    history[sym]={**result_data,"attempted_at":datetime.now(timezone.utc).isoformat()}
                except Exception as exc:
                    previous=history.get(sym,{})
                    history[sym]={**previous,"verified":bool(previous.get("verified")),"error":f"{type(exc).__name__}: {str(exc)[:150]}","attempted_at":datetime.now(timezone.utc).isoformat()}
                await asyncio.sleep(1)
                if sym==todo[0][0]:save(force=True)
            save(force=True)
            await asyncio.sleep(15)
