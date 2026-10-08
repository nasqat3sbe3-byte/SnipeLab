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

def ordered_chart(chart,dates,at):
    """Keep intraday time order; sparse stocks need session slots, not six trades."""
    if not recent(chart.get('updated_at'),at,21600):return []
    today=at.astimezone(NY).date().isoformat()
    candles=sorted([c for c in chart.get('candles',[]) if c.get('date') in dates and (c.get('closed') or c.get('date')==today)],key=lambda c:c.get('time',0))
    for d in dates:
        slots={str(c.get('local_time',''))[-5:] for c in candles if c.get('date')==d}
        if not slots or (d<today and not {'09:30','13:30'}<=slots):return []
    return candles

def evaluate(meta,h,q,b,a,at,chart=None,detail=None):
    def fail(state,reason):
        if detail is not None:detail.update(state=state,reason=reason)
        return state,None
    high=number(h.get('split_day_4h_high'));price=number(q.get('price'))
    if not high or high<=0 or not price or price<=0:return fail('pending','بيانات السعر أو شمعة التقسيم غير مكتملة')
    if not h.get('verified') or h.get('effective_date')!=meta.get('effective_date') or h.get('partial_exchange_coverage'):return fail('pending','تاريخ التقسيم غير متحقق أو تغطية الشمعة جزئية')
    half=high/2; discount=(1-price/half)*100
    if detail is not None:detail.update(price=price,split_high=high,half=half,discount_pct=round(discount,2),range_low=half*.65,range_high=half*.8)
    if not 20-1e-9<=discount<=35+1e-9:return fail('outside','السعر خارج نطاق 20% إلى 35% تحت النصف')
    available=number(b.get('available'))
    if detail is not None:detail['available']=available
    if available is None or available<0:return fail('pending','بيانات Available غير مكتملة')
    if available>40000:return fail('excluded','Available فوق 40,000 سهم')
    borrow_fresh={'status':'fresh' if recent(b.get('received_at'),at,1200) else 'stale','timestamp':b.get('received_at')}
    if not usable_reading(borrow_fresh,at):return fail('pending','بيانات Available تحتاج تحديثًا')
    fresh={'status':'fresh' if recent(q.get('received_at'),at,900) else 'stale','timestamp':q.get('market_timestamp')}
    if not usable_reading(fresh,at) or not recent(h.get('updated_at'),at,172800):return fail('pending','السعر أو التاريخ يحتاج تحديثًا')
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
    intraday=ordered_chart(chart or {},dates,at)
    if intraday:
        sequence=intraday
        if detail is not None:detail['movement_source']='chronological_4h_extended'
    elif dates and all(d in bars for d in dates):
        sequence=[bars[d] for d in dates]
        if detail is not None:detail['movement_source']='daily_ordered_sessions'
    else:return fail('pending','سجل آخر 10 جلسات غير مكتمل؛ ينتظر تحديث التاريخ')
    low=None;max_rise=0;ambiguous=False
    for bar in sequence:
        lo,hi=number(bar.get('low')),number(bar.get('high'))
        if not lo or not hi or not 0<lo<=hi:return fail('pending','شمعة غير صالحة في سجل الحركة')
        if low is not None:max_rise=max(max_rise,(hi/low-1)*100)
        # Daily OHLC cannot prove low preceded high within one session.
        # Hold uncertain >=70% same-session ranges for intraday verification.
        if (hi/lo-1)*100>=70-1e-9:ambiguous=True
        low=min(low,lo) if low is not None else lo
    if intraday and low is not None:
        # Latest price is chronologically later; daily high is not.
        max_rise=max(max_rise,(price/low-1)*100)
    if detail is not None:detail['recent_rise_pct']=round(max_rise,2)
    if max_rise>=70-1e-9:return fail('excluded','طلعة 70% أو أكثر داخل آخر 10 جلسات')
    if ambiguous:return fail('pending','ترتيب القاع والقمة داخل شمعة واحدة يحتاج تحققًا')
    if detail is not None:detail.update(state='eligible',reason='مطابق للشروط')
    return 'eligible',{'symbol':meta['symbol'],'price':price,'split_high':high,'half':half,
        'range_low':half*.65,'range_high':half*.8,'discount_pct':round(discount,2),
        'recent_rise_pct':round(max_rise,2),'split_date':meta['effective_date'],
        'available':b.get('available'),'rsi':a.get('rsi_daily'),'sessions':a.get('effective_sessions'),
        'price_at':q.get('market_timestamp'),'history_at':h.get('updated_at')}

def recent(value,at,seconds):
    try:return 0<=(at-datetime.fromisoformat(value.replace('Z','+00:00'))).total_seconds()<=seconds
    except (AttributeError,TypeError,ValueError):return False

def build(universe,history,quotes,borrow,analytics,at,charts=None):
    rows=[];pending=excluded=0;diagnostics=[]
    for sym,meta in list(universe.items()):
        if not meta.get('effective_date') or meta['effective_date']>at.astimezone(NY).date().isoformat():continue
        detail={'symbol':sym}
        state,row=evaluate({**meta,'symbol':sym},history.get(sym) or {},quotes.get(sym) or {},borrow.get(sym) or {},analytics.get(sym) or {},at,(charts or {}).get(sym),detail)
        diagnostics.append(detail)
        if row:rows.append(row)
        pending+=state=='pending';excluded+=state=='excluded'
    rows.sort(key=lambda x:(-x['discount_pct'],number(x['available']) if number(x['available']) is not None else float('inf'),x['symbol']))
    return {'status':'ready','rows':rows,'pending':pending,'excluded':excluded,'generated_at':at.isoformat(),'diagnostics':diagnostics}

async def worker(universe,history,quotes,borrow,analytics,charts=None):
    global SNAPSHOT
    while True:
        try:SNAPSHOT=build(universe,history,quotes,borrow,analytics,datetime.now(timezone.utc),charts)
        except Exception:SNAPSHOT={**SNAPSHOT,'status':'error','rows':[]}
        await asyncio.sleep(30)
