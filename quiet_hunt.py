"""Strict, isolated split-stock screen; calculations use existing cached feeds."""
import asyncio
from datetime import datetime, timezone
import storage
import hunt
from opportunities import usable_reading

SNAPSHOT={'status':'starting','rows':[],'pending':0}

def evaluate(meta,h,q,b,rsi,rsi_at,chart,at):
    number=hunt.number
    price,high,available,rs=map(number,(q.get('price'),h.get('split_day_4h_high'),b.get('available'),rsi))
    if not h.get('verified') or h.get('effective_date')!=meta.get('effective_date') or h.get('partial_exchange_coverage') or not high or not price or available is None or rs is None:
        return 'pending',None
    if not (0<=rs<=100 and available>=0):return 'pending',None
    if rs>=30 or available>=15000 or price>high*.5*1.2:return 'excluded',None
    if not usable_reading({'status':'fresh' if hunt.recent(q.get('received_at'),at,900) else 'stale','timestamp':q.get('market_timestamp')},at) or not usable_reading({'status':'fresh' if hunt.recent(b.get('received_at'),at,1200) else 'stale','timestamp':b.get('received_at')},at) or not hunt.recent(h.get('updated_at'),at,172800) or not hunt.recent(rsi_at,at,172800):
        return 'pending',None
    dates=hunt.window(at)
    # Pre-split bars cannot be compared with split-adjusted post-split candles.
    if len(dates)!=10 or dates[0]<meta['effective_date']:return 'pending',None
    if not chart or chart.get('split_date')!=meta['effective_date']:return 'pending',None
    local=at.astimezone(hunt.NY)
    if hunt.trading(local.date()) and 4 <= local.hour < 20 and not hunt.recent(chart.get('updated_at'),at,900):return 'pending',None
    candles=chart.get('candles') or []
    if any(not c.get('hourly_parts') for c in candles if c.get('date') in dates):return 'pending',None
    for day in dates:
        if day < local.date().isoformat():
            slots={str(c.get('local_time',''))[-5:] for c in candles if c.get('date')==day}
            if not {'04:00','16:00'}.issubset(slots):return 'pending',None
    bars=hunt.ordered_chart(chart,dates,at)
    if not bars:return 'pending',None
    # For each session, the low must occur before the high. A single hourly
    # candle cannot prove ordering; an ambiguous >=50% candle stays pending.
    lows={}; maxima={}; ambiguous=False
    for bar in bars:
        day=bar.get('date');lo=number(bar.get('low'));hi=number(bar.get('high'))
        if day not in dates or not lo or not hi or hi<lo:return 'pending',None
        if day in lows:
            gain=(hi/lows[day]-1)*100
            maxima[day]=max(maxima.get(day,0),gain)
            if gain>=50-1e-9:return 'excluded',None
        if (hi/lo-1)*100>=50-1e-9:ambiguous=True
        lows[day]=min(lows.get(day,lo),lo)
    if ambiguous:return 'pending',None
    half=high/2
    return 'eligible',{'symbol':meta['symbol'],'price':price,'half':half,'split_high':high,'distance_pct':round((price/half-1)*100,2),'available':available,'rsi':rs,'max_rise_pct':round(max(maxima.values(),default=0),2),'split_date':meta['effective_date']}

async def worker(universe,history,quotes,borrow,rsi_lookup,charts):
    global SNAPSHOT
    while True:
        try:
            at=datetime.now(timezone.utc);rows=[];pending=0;candidate=[]
            for sym,meta in list(universe.items()):
                if not meta.get('effective_date') or meta['effective_date']>at.astimezone(hunt.NY).date().isoformat():continue
                h=history.get(sym) or {};q=quotes.get(sym) or {};b=borrow.get(sym) or {};rsi,rsi_at=rsi_lookup(sym)
                chart=charts.get(sym)
                if not chart:
                    price=hunt.number(q.get('price'));hi=hunt.number(h.get('split_day_4h_high'));av=hunt.number(b.get('available'));rs=hunt.number(rsi)
                    if price and hi and price<=hi*.6 and av is not None and 0<=av<15000 and rs is not None and 0<=rs<30:
                        chart=(await asyncio.to_thread(storage.load,('support_chart:'+sym,))).get('support_chart:'+sym)
                state,row=evaluate({**meta,'symbol':sym},h,q,b,rsi,rsi_at,chart,at)
                if state=='eligible':rows.append(row)
                elif state=='pending':
                    pending+=1
                    price=hunt.number(q.get('price'));hi=hunt.number(h.get('split_day_4h_high'));av=hunt.number(b.get('available'));rs=hunt.number(rsi)
                    if price and hi and price<=hi*.6 and av is not None and 0<=av<15000 and rs is not None and 0<=rs<30 and (not chart or not hunt.recent(chart.get('updated_at'),at,900 if hunt.trading(at.astimezone(hunt.NY).date()) and 4 <= at.astimezone(hunt.NY).hour < 20 else 21600)):candidate.append(sym)
            rows.sort(key=lambda x:(abs(x['distance_pct']),x['available'],x['symbol']))
            SNAPSHOT={'status':'ready','rows':rows,'pending':pending,'generated_at':at.isoformat()}
            from support_chart import PRIORITY,WAKE
            for sym in candidate:
                if len(PRIORITY)>=50:break
                PRIORITY.setdefault(sym,0)
            if candidate:WAKE.set()
        except Exception:
            SNAPSHOT={'status':'error','rows':[],'pending':0}
        await asyncio.sleep(30)
