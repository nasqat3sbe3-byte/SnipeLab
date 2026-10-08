"""Cache-only personal half-of-split-candle screen; independent of readiness."""
import asyncio
from datetime import datetime, timedelta, timezone
from opportunities import NY, trading, number, usable_reading

SNAPSHOT={'status':'starting','rows':[],'pending':0,'excluded':0,'generated_at':None}

def window(at):
    local=at.astimezone(NY)
    end=local.date()
    if local.hour<4:end-=timedelta(days=1)
    days=[]
    while len(days)<10:
        if trading(end):days.append(end.isoformat())
        end-=timedelta(days=1)
    return sorted(days)

def evaluate(meta,h,q,b,a,at):
    high=number(h.get('split_day_4h_high'));price=number(q.get('price'))
    if not high or high<=0 or not price or price<=0:return 'pending',None
    if not h.get('verified') or h.get('effective_date')!=meta.get('effective_date') or h.get('partial_exchange_coverage'):return 'pending',None
    half=high/2; discount=(1-price/half)*100
    if not 20-1e-9<=discount<=35+1e-9:return 'outside',None
    fresh={'status':'fresh' if recent(q.get('received_at'),at,900) else 'stale','timestamp':q.get('market_timestamp')}
    if not usable_reading(fresh,at) or not recent(h.get('updated_at'),at,172800):return 'pending',None
    dates=[d for d in window(at) if d>=meta['effective_date']]
    bars={x['date']:x for x in h.get('hunt_daily_bars',[]) if x.get('date') in dates}
    # A current quote may safely extend today's high/low, but cannot fill missing history.
    try:
        day=datetime.fromisoformat(q['market_timestamp'].replace('Z','+00:00')).astimezone(NY).date().isoformat()
        lo,hi=number(q.get('day_low')),number(q.get('day_high'))
        if day in dates and lo and hi and 0<lo<=hi:
            old=bars.get(day,{})
            bars[day]={'date':day,'low':min(lo,old.get('low',lo)),'high':max(hi,old.get('high',hi))}
    except (KeyError,TypeError,ValueError):pass
    if not dates or any(d not in bars for d in dates):return 'pending',None
    low=None;max_rise=0;ambiguous=False
    for d in dates:
        lo,hi=number(bars[d].get('low')),number(bars[d].get('high'))
        if not lo or not hi or not 0<lo<=hi:return 'pending',None
        if low is not None:max_rise=max(max_rise,(hi/low-1)*100)
        # Daily OHLC cannot prove low preceded high within one session.
        # Hold uncertain >=70% same-session ranges for intraday verification.
        if (hi/lo-1)*100>=70-1e-9:ambiguous=True
        low=min(low,lo) if low is not None else lo
    if max_rise>=70-1e-9:return 'excluded',None
    if ambiguous:return 'pending',None
    return 'eligible',{'symbol':meta['symbol'],'price':price,'split_high':high,'half':half,
        'range_low':half*.65,'range_high':half*.8,'discount_pct':round(discount,2),
        'recent_rise_pct':round(max_rise,2),'split_date':meta['effective_date'],
        'available':b.get('available'),'rsi':a.get('rsi_daily'),'sessions':a.get('effective_sessions'),
        'price_at':q.get('market_timestamp'),'history_at':h.get('updated_at')}

def recent(value,at,seconds):
    try:return 0<=(at-datetime.fromisoformat(value.replace('Z','+00:00'))).total_seconds()<=seconds
    except (AttributeError,TypeError,ValueError):return False

def build(universe,history,quotes,borrow,analytics,at):
    rows=[];pending=excluded=0
    for sym,meta in list(universe.items()):
        if not meta.get('effective_date') or meta['effective_date']>at.astimezone(NY).date().isoformat():continue
        state,row=evaluate({**meta,'symbol':sym},history.get(sym) or {},quotes.get(sym) or {},borrow.get(sym) or {},analytics.get(sym) or {},at)
        if row:rows.append(row)
        pending+=state=='pending';excluded+=state=='excluded'
    rows.sort(key=lambda x:(-x['discount_pct'],number(x['available']) if number(x['available']) is not None else float('inf'),x['symbol']))
    return {'status':'ready','rows':rows,'pending':pending,'excluded':excluded,'generated_at':at.isoformat()}

async def worker(universe,history,quotes,borrow,analytics):
    global SNAPSHOT
    while True:
        try:SNAPSHOT=build(universe,history,quotes,borrow,analytics,datetime.now(timezone.utc))
        except Exception:SNAPSHOT={**SNAPSHOT,'status':'error','rows':[]}
        await asyncio.sleep(30)
