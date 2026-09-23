from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
import asyncio
import time
import os
import httpx

def calculate(effective, candles):
    """Daily Yahoo OHLC; high must occur on/after low for ten-session TOP."""
    bars=sorted((b for b in candles if b["date"]>=effective and b["low"]>0 and b["high"]>=b["low"]),key=lambda b:b["date"])
    if not bars:return {"verified":False,"error":"Missing post-split bars"}
    first=bars[0]; gap=(date.fromisoformat(first["date"])-date.fromisoformat(effective)).days
    verified=0<=gap<=4
    ny_today=datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    completed=[b for b in bars if b["date"]<ny_today]
    # A TOP wave stays visible for TEN COMPLETED trading sessions after its
    # peak, not ten calendar days and not until its low falls out of a window.
    # For each peak in the latest ten sessions, compare with earlier lows
    # in at most ten trading sessions (inclusive). This permits a new wave.
    top=None
    for peak_i in range(max(0,len(completed)-10),len(completed)):
        peak=completed[peak_i]
        for low_i in range(max(0,peak_i-9),peak_i+1):
            low=completed[low_i]
            rise=(peak["high"]/low["low"]-1)*100
            sessions_since_peak=len(completed)-1-peak_i
            if top is None or rise>top["top_10_gain_pct"]:
                top={"top_10_gain_pct":round(rise,2),
                     "top_10_low":low["low"],"top_10_high":peak["high"],
                     "top_10_low_date":low["date"],"top_10_high_date":peak["date"],
                     "top_10_sessions_since_peak":sessions_since_peak}
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
        "top_10_verified":bool(top and verified and top["top_10_gain_pct"]>=40),
        "top_calculated_at":datetime.now(timezone.utc).isoformat(),
        "top_calculator_version":2,**(top or {}),
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

async def twelve_data_split_day(client, symbol, effective):
    """Optional independent 1h source, requested ONLY for missing split-day 4H.
    Requires TWELVEDATA_API_KEY. Never treat daily high as a 4H candle.
    """
    key=os.getenv("TWELVEDATA_API_KEY")
    if not key:
        return {"split_day_4h_high":None,"split_day_4h_status":"twelvedata_key_not_configured"}
    r=await client.get("https://api.twelvedata.com/time_series",params={
        "symbol":symbol,"interval":"1h","start_date":effective+" 04:00:00",
        "end_date":effective+" 20:00:00","timezone":"America/New_York",
        "prepost":"true","adjust":"none","apikey":key})
    r.raise_for_status()
    data=r.json()
    if data.get("status")=="error" or not data.get("values"):
        return {"split_day_4h_high":None,"split_day_4h_status":
                "twelvedata_"+str(data.get("code") or "no_intraday_bars")}
    buckets={}; lows=[]; highs=[]
    for bar in data["values"]:
        try:
            local=datetime.fromisoformat(bar["datetime"])
            if local.date().isoformat()!=effective or not 4<=local.hour<20:continue
            hi=float(bar["high"]);lo=float(bar["low"])
            if lo<=0 or hi<lo:continue
            bucket=(local.hour-4)//4
            buckets[bucket]=max(hi,buckets.get(bucket,0))
            highs.append(hi);lows.append(lo)
        except (KeyError,TypeError,ValueError):continue
    if not buckets:
        return {"split_day_4h_high":None,"split_day_4h_status":"twelvedata_no_split_day_intraday_bars"}
    return {"split_day_4h_high":max(buckets.values()),
            "split_day_4h_status":"twelvedata_1h_aggregated_extended_4h",
            "split_day_4h_candles":len(buckets),
            "extended_history_complete":False,
            "extended_post_split_high":max(highs),
            "extended_post_split_low":min(lows),
            "extended_post_split_high_date":effective,
            "extended_post_split_low_date":effective,
            "extended_history_first_date":effective,
            "extended_history_last_date":effective}

async def massive_split_day(client, symbol, effective):
    """Massive unadjusted 1-minute aggregates, 04:00–20:00 New York."""
    key=os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")
    if not key:
        return {"split_day_4h_high":None,"split_day_4h_status":"massive_key_not_configured"}
    ny=ZoneInfo("America/New_York")
    day=date.fromisoformat(effective)
    start=int(datetime.combine(day,datetime.min.time(),ny).timestamp()*1000)
    end=int(datetime.combine(day,datetime.max.time(),ny).timestamp()*1000)
    url=f"https://api.massive.com/v2/aggs/ticker/{symbol}/range/1/minute/{start}/{end}"
    r=await client.get(url,params={"adjusted":"false","sort":"asc","limit":50000,"apiKey":key})
    if r.status_code==429:return {"split_day_4h_high":None,"split_day_4h_status":"massive_rate_limited"}
    r.raise_for_status()
    data=r.json()
    if data.get("status")=="ERROR":
        return {"split_day_4h_high":None,"split_day_4h_status":"massive_api_error"}
    buckets={};highs=[];lows=[]
    for bar in data.get("results") or []:
        try:
            local=datetime.fromtimestamp(bar["t"]/1000,ny)
            if local.date()!=day or not 4<=local.hour<20:continue
            hi=float(bar["h"]);lo=float(bar["l"])
            if lo<=0 or hi<lo:continue
            bucket=(local.hour-4)//4
            buckets[bucket]=max(hi,buckets.get(bucket,0))
            highs.append(hi);lows.append(lo)
        except (KeyError,TypeError,ValueError):continue
    if not buckets:
        return {"split_day_4h_high":None,"split_day_4h_status":"massive_no_split_day_bars"}
    return {"split_day_4h_high":max(buckets.values()),
            "split_day_4h_status":"massive_unadjusted_1m_extended_4h",
            "split_day_4h_candles":len(buckets),
            "extended_post_split_high":max(highs),
            "extended_post_split_low":min(lows),
            "extended_post_split_high_date":effective,
            "extended_post_split_low_date":effective,
            "extended_history_first_date":effective,
            "extended_history_last_date":effective,
            "extended_history_complete":False}


async def alpaca_split_day(client, symbol, effective):
    """Alpaca free IEX hourly bars: partial exchange coverage, label explicitly."""
    key=os.getenv("ALPACA_API_KEY_ID")
    secret=os.getenv("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        return {"split_day_4h_high":None,"split_day_4h_status":"alpaca_keys_not_configured"}
    ny=ZoneInfo("America/New_York")
    day=date.fromisoformat(effective)
    start=datetime.combine(day,datetime.min.time(),ny)
    end=datetime.combine(day+__import__("datetime").timedelta(days=1),datetime.min.time(),ny)
    r=await client.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret},
        params={"timeframe":"1Hour","start":start.isoformat(),
                "end":end.isoformat(),"feed":"iex","adjustment":"raw","limit":1000})
    if r.status_code==429:return {"split_day_4h_high":None,"split_day_4h_status":"alpaca_rate_limited"}
    r.raise_for_status()
    buckets={};highs=[];lows=[]
    for bar in r.json().get("bars") or []:
        try:
            local=datetime.fromisoformat(bar["t"].replace("Z","+00:00")).astimezone(ny)
            if local.date()!=day or not 4<=local.hour<20:continue
            hi=float(bar["h"]);lo=float(bar["l"])
            if lo<=0 or hi<lo:continue
            bucket=(local.hour-4)//4
            buckets[bucket]=max(hi,buckets.get(bucket,0))
            highs.append(hi);lows.append(lo)
        except (KeyError,TypeError,ValueError):continue
    if not buckets:
        return {"split_day_4h_high":None,"split_day_4h_status":"alpaca_iex_no_split_day_bars"}
    return {"split_day_4h_high":max(buckets.values()),
            "split_day_4h_status":"alpaca_iex_partial_exchange_1h_4h",
            "split_day_4h_candles":len(buckets),
            "extended_post_split_high":max(highs),
            "extended_post_split_low":min(lows),
            "extended_post_split_high_date":effective,
            "extended_post_split_low_date":effective,
            "extended_history_first_date":effective,
            "extended_history_last_date":effective,
            "extended_history_complete":False,
            "partial_exchange_coverage":True}


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
            # Completed histories still need periodic extrema updates as new sessions trade.
            # Keep the last good snapshot visible while a refresh is in progress.
            # Migrate persisted histories produced before TOP-wave calculation.
            # Do not wait an hour for a row with all four OHLC fields present.
            def needs_top_migration(sym,meta):
                h=history.get(sym,{})
                return (h.get("effective_date")==meta["effective_date"]
                        and h.get("top_calculator_version",0)<2
                        and (datetime.now(timezone.utc).date()-
                             date.fromisoformat(meta["effective_date"])).days<=30)
            todo=[(sym,meta) for sym,meta in todo
                  if needs_top_migration(sym,meta)
                  or not complete(history.get(sym,{}),meta)
                  or (time.time()-datetime.fromisoformat(
                      history[sym].get("attempted_at", "1970-01-01T00:00:00+00:00")
                  ).timestamp() >= 3600)]
            # Retry failures after 30 minutes, not continuously.
            now_epoch=time.time()
            def retry_due(sym,meta):
                h=history.get(sym,{})
                if h.get("effective_date")!=meta["effective_date"]:return True
                try:
                    age=now_epoch-datetime.fromisoformat(h["attempted_at"]).timestamp()
                except (ValueError,KeyError,TypeError):return True
                return age>=1800
            todo=[(sym,meta) for sym,meta in todo
                  if needs_top_migration(sym,meta) or retry_due(sym,meta)]
            # Recent split histories missing TOP-wave calculations must be
            # recalculated promptly, even when the four core fields are complete.
            # Older split histories with no rally stay on the normal refresh cycle.
            def top_recalc_due(sym,meta):
                h=history.get(sym,{})
                age=(datetime.now(timezone.utc).date()-date.fromisoformat(meta["effective_date"])).days
                return (age<=30 and h.get("verified") and
                        h.get("top_calculator_version",0)<2)
            # Fix old persisted rows whose four OHLC fields are complete but
            # whose TOP metrics predate the current calculator. Process recent
            # TOP-missing splits before other historical refreshes.
            todo.sort(key=lambda item:(
                not top_recalc_due(item[0],item[1]),
                -date.fromisoformat(item[1]["effective_date"]).toordinal()
                    if top_recalc_due(item[0],item[1]) else 0,
                complete(history.get(item[0],{}),item[1]),
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
                    result_data.setdefault("effective_date",eff)
                    try:
                        # Keep Yahoo as primary. Query the independent provider
                        # immediately when Yahoo lacks the required day-one bars.
                        try:
                            intraday=await split_day_4h_high(client,yahoo,sym,eff)
                        except Exception as yahoo_exc:
                            # Yahoo can reject old 60m windows; do not let its
                            # HTTP error prevent the other configured providers.
                            intraday={"split_day_4h_high":None,
                                      "split_day_4h_status":"yahoo_"+type(yahoo_exc).__name__,
                                      "extended_history_complete":False}
                        if intraday.get("split_day_4h_high") is None:
                            intraday["fallback_attempts"]=[]
                            for provider in (massive_split_day, twelve_data_split_day, alpaca_split_day):
                                try:
                                    alternative=await provider(client,sym,eff)
                                    intraday["fallback_attempts"].append(
                                        alternative.get("split_day_4h_status","unknown"))
                                    if alternative.get("split_day_4h_high") is not None:
                                        alternative["fallback_attempts"]=intraday["fallback_attempts"][:]
                                        intraday=alternative
                                        break
                                except Exception as alternate_exc:
                                    intraday["fallback_attempts"].append(
                                        provider.__name__+":"+type(alternate_exc).__name__)
                        result_data.update(intraday)
                        if result_data.get("partial_exchange_coverage"):
                            result_data.setdefault("quality_warnings",[]).append("partial_exchange_coverage")
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
                    result_data["quality_warnings"]=result_data.get("quality_warnings",[])
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
                    # A temporary upstream gap must not erase previously verified
                    # extrema for the SAME split; retain them until a valid refresh.
                    previous=history.get(sym,{})
                    if (previous.get("verified") and previous.get("effective_date")==eff
                            and not result_data.get("verified")):
                        history[sym]={**previous,"refresh_error":result_data.get("error") or "unverified refresh",
                                      "attempted_at":datetime.now(timezone.utc).isoformat()}
                    else:
                        history[sym]={**result_data,"attempted_at":datetime.now(timezone.utc).isoformat()}
                except Exception as exc:
                    previous=history.get(sym,{})
                    # Do not mark stale data fresh or lose a valid historical snapshot.
                    history[sym]={**previous,"effective_date":meta["effective_date"],
                                  "verified":bool(previous.get("verified") and previous.get("effective_date")==meta["effective_date"]),
                                  "error":f"{type(exc).__name__}: {str(exc)[:150]}",
                                  "attempted_at":datetime.now(timezone.utc).isoformat()}
                await asyncio.sleep(1)
                if sym==todo[0][0]:save(force=True)
            save(force=True)
            await asyncio.sleep(15)
