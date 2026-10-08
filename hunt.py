"""Cache-only personal half-of-split-candle screen; independent of readiness."""
import asyncio
from copy import deepcopy
import storage
from datetime import datetime, timedelta, timezone
from opportunities import NY, trading, number, usable_reading

RULE_VERSION=2
EXCLUSIONS={}
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

def ordered_chart(chart,dates,at,*,require_fresh=True,require_complete=True):
    """Keep intraday time order; sparse stocks need session slots, not six trades."""
    if require_fresh and not recent(chart.get('updated_at'),at,21600):return []
    today=at.astimezone(NY).date().isoformat()
    candles=sorted([c for c in chart.get('candles',[]) if c.get('date') in dates and (c.get('closed') or c.get('date')==today)],key=lambda c:c.get('time',0))
    for d in dates if require_complete else []:
        slots={str(c.get('local_time',''))[-5:] for c in candles if c.get('date')==d}
        if not slots or (d<today and not {'09:30','13:30'}<=slots):return []
    fine=[]
    for candle in candles:
        fine.extend(candle.get('hourly_parts') or [candle])
    return sorted(fine,key=lambda x:x.get('time',0))

def post_split_dates(effective,at):
    end=window(at)[-1];day=datetime.fromisoformat(effective).date();dates=[]
    while day.isoformat()<=end:
        if trading(day):dates.append(day.isoformat())
        day+=timedelta(days=1)
    return dates

def waves80(sequence):
    """Count >80% waves on distinct NY sessions, resetting at the original base."""
    base=None;base_at=None;active=False;events=[];uncertain=[]
    for bar in sequence:
        lo,hi=number(bar.get('low')),number(bar.get('high'))
        if not lo or not hi or not 0<lo<=hi:return {'events':events,'uncertain':['invalid_bar']}
        stamp=bar.get('local_time') or bar.get('date')
        if hi/lo>1.8+1e-9:uncertain.append(stamp)
        if active:
            if hi>events[-1]['peak']:
                events[-1].update(peak=hi,peak_at=stamp,gain_pct=round((hi/base-1)*100,2))
            if lo<=base:
                active=False;base=lo;base_at=stamp
            continue
        if base is not None and hi/base>1.8+1e-9:
            events.append({'base':base,'base_at':base_at,'peak':hi,'peak_at':stamp,'gain_pct':round((hi/base-1)*100,2)})
            active=True
            # A return in this same OHLC bar has unknown order. Do not invent a reset.
        elif base is None or lo<base:
            base=lo;base_at=stamp
    # Multiple independent rebounds in one NY session count only once.
    by_session={}
    for event in events:
        day=str(event['peak_at']).split(' ')[0]
        event['peak_session']=day
        if day not in by_session or event['gain_pct']>by_session[day]['gain_pct']:
            by_session[day]=event
    return {'events':list(by_session.values()),'uncertain':uncertain}

def evaluate(meta,h,q,b,a,at,chart=None,detail=None,exclusion=None):
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
    # Historical disqualification never expires or falls back to regular-session daily bars.
    # A new reverse split changes its key and requires an independent verification.
    if exclusion and len(exclusion.get('events') or [])>=2 and exclusion.get('version')==RULE_VERSION and exclusion.get('effective_date')==meta['effective_date']:
        if detail is not None:detail.update(waves80_count=len(exclusion['events']),waves80=exclusion['events'],wave_proof_saved=True,movement_source='saved_extended_wave_evidence')
        return fail('excluded','طلعتان فوق 80% موثقتان في يومين مختلفين بعد هذا التقسيم')
    chart=chart or {}
    if chart.get('split_date') not in (None,meta['effective_date']):chart={}
    full_dates=post_split_dates(meta['effective_date'],at)
    observed=ordered_chart(chart,full_dates,at,require_fresh=False,require_complete=False)
    wave_result=waves80(observed) if observed else {'events':[],'uncertain':[]}
    if detail is not None and observed:detail.update(waves80_count=len(wave_result['events']),waves80=wave_result['events'],waves80_uncertain=wave_result['uncertain'],wave_source='extended_intraday')
    if len(wave_result['events'])>=2:
        return fail('excluded','طلعتان فوق 80% في جلستين مختلفتين بعد التقسيم، وبينهما رجوع لقاع البداية')
    full_sequence=ordered_chart(chart,full_dates,at,require_fresh=False)
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
    if not intraday:
        return fail('pending','بانتظار شموع الساعات الممتدة؛ اليومية وحدها لا تثبت اجتياز شروط الحركة')
    if not full_sequence:
        return fail('pending','فحص طلعتين فوق 80% ينتظر شموع الساعات الممتدة بعد التقسيم كاملة')
    if wave_result['uncertain']:return fail('pending','فحص طلعات 80% يحتاج ترتيبًا أدق داخل الشمعة')
    if detail is not None:detail.update(state='eligible',reason='مطابق للشروط')
    return 'eligible',{'symbol':meta['symbol'],'price':price,'split_high':high,'half':half,
        'range_low':half*.65,'range_high':half*.8,'discount_pct':round(discount,2),
        'recent_rise_pct':round(max_rise,2),'split_date':meta['effective_date'],
        'available':b.get('available'),'rsi':a.get('rsi_daily'),'sessions':a.get('effective_sessions'),
        'price_at':q.get('market_timestamp'),'history_at':h.get('updated_at'),'waves80_count':len(wave_result['events'])}

def recent(value,at,seconds):
    try:return 0<=(at-datetime.fromisoformat(value.replace('Z','+00:00'))).total_seconds()<=seconds
    except (AttributeError,TypeError,ValueError):return False

def build(universe,history,quotes,borrow,analytics,at,charts=None,exclusions=None):
    rows=[];pending=excluded=0;diagnostics=[]
    for sym,meta in list(universe.items()):
        if not meta.get('effective_date') or meta['effective_date']>at.astimezone(NY).date().isoformat():continue
        detail={'symbol':sym}
        state,row=evaluate({**meta,'symbol':sym},history.get(sym) or {},quotes.get(sym) or {},borrow.get(sym) or {},analytics.get(sym) or {},at,(charts or {}).get(sym),detail,(exclusions or {}).get(sym))
        diagnostics.append(detail)
        if row:rows.append(row)
        pending+=state=='pending';excluded+=state=='excluded'
    rows.sort(key=lambda x:(-x['discount_pct'],number(x['available']) if number(x['available']) is not None else float('inf'),x['symbol']))
    return {'status':'ready','rows':rows,'pending':pending,'excluded':excluded,'generated_at':at.isoformat(),'diagnostics':diagnostics}

def remember_exclusions(snapshot,universe,proofs,at):
    changed=False
    for d in snapshot.get('diagnostics',[]):
        if d.get('wave_source')!='extended_intraday' or d.get('waves80_count',0)<2:continue
        sym=d['symbol'];effective=(universe.get(sym) or {}).get('effective_date')
        previous=proofs.get(sym) or {}
        if previous.get('version')==RULE_VERSION and previous.get('effective_date')==effective:continue
        proofs[sym]={'version':RULE_VERSION,'effective_date':effective,'checked_at':at.isoformat(),
                     'events':deepcopy(d['waves80'][:2]),'source':'extended_intraday'}
        changed=True
    return changed

async def worker(universe,history,quotes,borrow,analytics,charts=None):
    global SNAPSHOT
    try:
        EXCLUSIONS.update((await asyncio.to_thread(storage.load,('hunt_wave_exclusions_v2',))).get('hunt_wave_exclusions_v2') or {})
    except Exception:pass
    saved_proofs=deepcopy(EXCLUSIONS)
    while True:
        try:
            at=datetime.now(timezone.utc)
            SNAPSHOT=build(universe,history,quotes,borrow,analytics,at,charts,EXCLUSIONS)
            remember_exclusions(SNAPSHOT,universe,EXCLUSIONS,at)
            if EXCLUSIONS!=saved_proofs:
                await asyncio.to_thread(storage.save,{'hunt_wave_exclusions_v2':deepcopy(EXCLUSIONS)})
                saved_proofs=deepcopy(EXCLUSIONS)
            # Only candidates in the price/borrow zone request optional chart refreshes.
            # Share the existing single chart worker and its cooldown, without HTTP-path I/O.
            from support_chart import PRIORITY, WAKE
            for d in SNAPSHOT.get('diagnostics',[]):
                if d.get('state') not in ('pending','eligible') or d.get('available') is None or d['available']>40000 or not 20<=d.get('discount_pct',0)<=35 or len(PRIORITY)>=50:continue
                cached=(charts or {}).get(d['symbol']) or {}
                if cached.get('hourly_version',0)<1 or cached.get('split_date')!=(universe.get(d['symbol']) or {}).get('effective_date') or not recent(cached.get('updated_at'),at,21600):
                    PRIORITY.setdefault(d['symbol'],0)
            if PRIORITY:WAKE.set()
        except Exception:
            SNAPSHOT={**SNAPSHOT,'status':'error'}
        await asyncio.sleep(30)
