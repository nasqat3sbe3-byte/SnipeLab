"""Cached breadth of the tracked universe, never an all-market claim.
Daily high and close returns use the previous split-adjusted daily close.
SPY's observed session dates supply the shared holiday-aware trading window.
Only post-action sessions are comparable: action-day returns and all paths
crossing a split are excluded, even if the vendor silently changes units.
"""
import asyncio
import csv
import io
import hashlib
import re
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import httpx
import storage

CACHE = {}
SNAPSHOT = {"status": "starting", "rows": [], "sessions": []}
NY = ZoneInfo('America/New_York')
CACHE_SCHEMA = 2


def number(v):
    try:
        v = float(v)
        return v if math.isfinite(v) and v > 0 else None
    except (ValueError, TypeError):
        return None


def parse_chart(result):
    q = (result.get('indicators', {}).get('quote') or [{}])[0]
    bars = {}
    for i, ts in enumerate(result.get('timestamp') or []):
        try:
            bar = {k: number(q[k][i]) for k in ('high', 'close', 'volume')}
            if not bar['high'] or not bar['close'] or bar['close'] > bar['high']: continue
            day = datetime.fromtimestamp(ts, NY).date().isoformat()
            bars[day] = dict(date=day, **bar)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return sorted(bars.values(), key=lambda b: b['date'])[-32:]


def parse_splits(result):
    """Retain corporate actions instead of discarding the requested split feed."""
    events=(result.get('events') or {}).get('splits') or {}
    if not isinstance(events,dict): raise ValueError('Invalid split event data')
    days=[]
    for key,event in events.items():
        stamp=event.get('date',key)
        day=datetime.fromtimestamp(int(stamp),NY).date().isoformat()
        days.append(day)
    return sorted(set(days))


def comparable_bars(record, known_date=None):
    """Never guess adjustment factors from price jumps or adjust data twice.

    New schema proves that the split event feed was inspected. Use both the
    independent trading-date calendar and vendor events. Discard pre-action
    history so peaks, volume ratios, paths and next-day returns share units.
    A split session itself has no comparable previous close and cannot count.
    """
    if record.get('schema') != CACHE_SCHEMA: return []
    bars=record.get('bars') or []
    if not bars:return []
    boundaries=[d for d in record.get('split_dates',[]) if d<=bars[-1]['date']]
    if known_date and known_date<=bars[-1]['date']:boundaries.append(known_date)
    boundary=max(boundaries,default='')
    return [b for b in bars if b['date']>=boundary]


def _build_view(cache, groups, now=None, known_splits=None):
    now = now or datetime.now(timezone.utc)
    known_splits=known_splits or {}
    calendar = cache.get('_calendar', {})
    session_dates = [b['date'] for b in calendar.get('bars', []) if b['date'] <= now.astimezone(NY).date().isoformat()]
    window = session_dates[-10:]
    base = dict(generated_at=now.isoformat(), scope='أسهم التقسيم وقليلة الفلوت المتابعة في SnipeLab',
                source='Yahoo Daily · الجلسة العادية', total=len(groups), rows=[], sessions=[], counts={},
                covered=0, status='collecting', label='جارٍ جمع البيانات')
    if len(session_dates) < 11: return base
    age = now.timestamp() - calendar.get('fetched_at', 0)
    if age > 1800:
        return dict(base, status='stale', label='بيانات الجلسات قديمة؛ التقييم معلّق')
    sessions = {d: dict(date=d, covered=0, counts={str(n): 0 for n in (10,25,50,100)},
                        return_sum=0, mean_return=None, advancing=0, holding=0, continued=0, prior_movers=0, volume_samples=0, elevated_volume=0) for d in window}
    rows = []
    covered = 0
    for sym, group in sorted(groups.items()):
        record = cache.get(sym, {})
        if now.timestamp() - record.get('fetched_at', 0) > 1800: continue
        bars = comparable_bars(record,known_splits.get(sym))
        if not bars or bars[-1]['date'] != window[-1]: continue
        by_day = {b['date']: (i, b) for i, b in enumerate(bars)}
        # Common cohort for all breadth comparisons: a missing session is not a zero.
        cohort = all(d in by_day and by_day[d][0] > 0 and bars[by_day[d][0]-1]['date'] == session_dates[session_dates.index(d)-1] for d in window)
        covered += cohort
        movements = []
        for day in window:
            if day not in by_day: continue
            i, b = by_day[day]
            if i == 0 or bars[i-1]['date'] != session_dates[session_dates.index(day)-1]: continue
            prev = bars[i-1]['close']
            high = (b['high']/prev-1)*100
            close = (b['close']/prev-1)*100
            earlier_volumes = [v['volume'] for v in bars[max(0,i-20):i] if v.get('volume')]
            ratio = b.get('volume')/(sum(earlier_volumes)/len(earlier_volumes)) if b.get('volume') and len(earlier_volumes)>=5 else None
            if cohort:
                s = sessions[day]; s['covered'] += 1; s['return_sum'] += close; s['mean_return']=round(s['return_sum']/s['covered'],2)
                s['advancing'] += close > 0
                for n in (10,25,50,100): s['counts'][str(n)] += high >= n
                s['holding'] += high >= 10 and close >= high*.5
                if ratio is not None:
                    s['volume_samples'] += 1; s['elevated_volume'] += ratio >= 2
                if i >= 2 and (bars[i-1]['high']/bars[i-2]['close']-1)*100 >= 25:
                    s['prior_movers'] += 1; s['continued'] += close > 0
            if high >= 25:
                latest = bars[-1]['close']
                after = (latest/b['close']-1)*100
                movements.append(dict(date=day, gain=round(high,2), close_gain=round(close,2),
                    reference=prev, peak=b['high'], close=b['close'], volume_ratio=round(ratio,2) if ratio else None,
                    since=len(window)-1-window.index(day), provisional=day==now.astimezone(NY).date().isoformat() and now.astimezone(NY).hour<16,
                    after_gain=round(after,2), current_gain=round((latest/prev-1)*100,2),
                    retention=round(close/high*100,1),
                    quality='strong' if close>=high*.7 else 'fade' if close<=high*.25 else 'partial',
                    next_gain=round((bars[i+1]['close']/b['close']-1)*100,2) if i+1<len(bars) else None,
                    next_provisional=bool(i+1<len(bars) and bars[i+1]['date']==now.astimezone(NY).date().isoformat() and now.astimezone(NY).hour<16),
                    path=[dict(date=x['date'],gain=round((x['close']/prev-1)*100,2)) for x in bars[i:]],
                    behavior='واصل الصعود' if after>5 else 'متماسك' if after>=-5 else 'تراجع'))
        if movements:
            movements.sort(key=lambda e: e['date'], reverse=True)
            rows.append(dict(symbol=sym, group=group, price=bars[-1]['close'], price_date=bars[-1]['date'],
                             moves=movements, **movements[0]))
    series = list(sessions.values())
    today = series[-1]
    baseline = series[:-3]
    avg = sum(s['counts']['25'] for s in baseline)/len(baseline)
    coverage = covered/max(1,len(groups))
    status, label = 'ready', 'نشاط محدود'
    reasons = []
    if coverage < .7 or covered < 10:
        status, label = 'collecting', 'تغطية غير كافية للحكم'
    else:
        strong = [s['counts']['25'] >= max(3, avg*1.4)
                  and s['holding'] >= max(1,s['counts']['10']*.5)
                  and s['advancing']/covered>=.5
                  and s['elevated_volume'] >= max(2,covered*.05)
                  and (not s['prior_movers'] or s['continued']/s['prior_movers']>=.5)
                  for s in series[-3:]]
        if all(strong): label='نشاط واسع ومستمر'
        elif strong[-1]: label='بداية تحسّن'
        elif today['counts']['25'] > max(2,avg*1.25): label='حركة تزيد؛ التماسك لم يتأكد'
        reasons.append(f"{today['counts']['25']} سهم وصل +25% مقابل متوسط {avg:.1f} في أول 7 جلسات")
        reasons.append(f"{today['holding']} من {today['counts']['10']} احتفظوا بنصف صعودهم على الأقل")
        reasons.append(f"{today['continued']} من {today['prior_movers']} متحركي الجلسة السابقة واصلوا عند الإغلاق")
    for session in series:
        session['advance_pct']=round(100*session['advancing']/max(1,covered),1)
        session['holding_pct']=round(100*session['holding']/session['counts']['10'],1) if session['counts']['10'] else None
        session['continuation_pct']=round(100*session['continued']/session['prior_movers'],1) if session['prior_movers'] else None
    return dict(base, status=status, label=label, covered=covered, coverage=round(coverage*100),
                rows=sorted(rows,key=lambda r:(r['date'],r['gain']),reverse=True), sessions=series,
                counts=today['counts'], baseline=round(avg,2), reasons=reasons, as_of=window[-1],
                provisional=window[-1]==now.astimezone(NY).date().isoformat() and now.astimezone(NY).hour<16,
                note='تقييم وصفي للنطاق المتابع؛ النسب من الإغلاق السابق إلى أعلى الجلسة، والأعداد تراكمية. الأسعار اليومية وليست أسعارًا لحظية.')


REFERENCE_URL='https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/latest-holdings.csv'
REFERENCE_GROUP='عينة الأسهم الصغيرة'


def reference_sample(text, excluded=(), limit=120):
    """Stable hash sample, independent of price moves, from the issuer's equities."""
    rows=list(csv.reader(io.StringIO(text.lstrip('\ufeff'))))
    header=next((i for i,r in enumerate(rows) if 'Ticker' in r and 'Asset Class' in r),None)
    if header is None: raise ValueError('Missing holdings header')
    fields=rows[header]; symbols=set()
    for values in rows[header+1:]:
        row=dict(zip(fields,values))
        symbol=(row.get('Ticker') or '').strip().replace(' ','-').replace('.','-')
        if row.get('Asset Class')=='Equity' and re.fullmatch(r'[A-Z][A-Z0-9-]{0,9}',symbol) and symbol not in excluded:
            symbols.add(symbol)
    if len(symbols)<limit: raise ValueError('Incomplete holdings file')
    return sorted(symbols,key=lambda s:hashlib.sha256(s.encode()).hexdigest())[:limit]


def build(cache, groups, now=None, known_splits=None):
    now=now or datetime.now(timezone.utc)
    tracked={s:g for s,g in groups.items() if g!=REFERENCE_GROUP}
    result=_build_view(cache,tracked,now,known_splits)
    views={'tracked':{k:v for k,v in result.items() if k not in ('rows',)}}
    all_rows=list(result['rows'])
    for key,label in [('split','أسهم التقسيم'),('low_float','قليلة الفلوت'),('reference',REFERENCE_GROUP)]:
        view=_build_view(cache,{s:g for s,g in groups.items() if g==label},now,known_splits)
        views[key]={k:v for k,v in view.items() if k!='rows'}
        if key=='reference': all_rows.extend(view['rows'])
    ref=views['reference']; ours=views['tracked']
    # Reference success is independent from tracked coverage. Never infer breadth from absent data.
    reference_meta=cache.get('_reference',{})
    reference_fresh=now.timestamp()-reference_meta.get('fetched_at',0)<=7*86400
    if not reference_fresh:
        ref.update(status='collecting',label='العينة المرجعية غير متاحة')
    comparison='نجمع بيانات العينة المرجعية للمقارنة بأسهمنا.'
    if ref['status']=='ready' and ours['status']=='ready':
        rs,ts=ref['sessions'][-1],ours['sessions'][-1]
        gap=rs['advance_pct']-ts['advance_pct']
        if gap>15: comparison='الصعود أوسع في العينة الصغيرة؛ أسهمنا تتأخر عنها.'
        elif gap< -15: comparison='النشاط أكثر تركّزًا في أسهمنا من العينة الصغيرة.'
        else: comparison='انتشار الصعود متقارب بين أسهمنا والعينة الصغيرة.'
    result.update(views=views, rows=all_rows, comparison=comparison, reference=dict(
        size=sum(g==REFERENCE_GROUP for g in groups.values()), available=reference_fresh,
        fetched_at=reference_meta.get('fetched_at'),
        source_url=REFERENCE_URL, method='عينة ثابتة من 120 سهمًا من مكونات IWM؛ اختيار مستقل عن الصعود، بأوزان متساوية. ليست السوق كاملًا ولا عائد المؤشر.'))
    return result


async def worker(get_groups, get_split_dates=lambda: {}):
    global SNAPSHOT
    try: CACHE.update(storage.load(('market_pulse',)).get('market_pulse') or {})
    except Exception: pass
    initial=get_groups()
    reference=CACHE.get('_reference',{})
    if time.time()-reference.get('fetched_at',0)<7*86400:
        initial.update({s:REFERENCE_GROUP for s in reference.get('symbols',[]) if s not in initial})
    SNAPSHOT=build(CACHE,initial,known_splits=get_split_dates())
    await asyncio.sleep(8)
    saved_at=0
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={'User-Agent':'Mozilla/5.0 SnipeLab'}) as client:
        sem=asyncio.Semaphore(2)
        async def fetch(sym):
            async with sem:
                CACHE.setdefault(sym,{})['attempted_at']=time.time()
                try:
                    ticker='SPY' if sym=='_calendar' else sym
                    r=await client.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',params={'range':'3mo','interval':'1d','includePrePost':'false','events':'splits'})
                    r.raise_for_status()
                    result=r.json()['chart']['result'][0]
                    bars=parse_chart(result)
                    split_dates=parse_splits(result)
                    if bars: CACHE[sym]=dict(bars=bars,split_dates=split_dates,schema=CACHE_SCHEMA,fetched_at=time.time(),attempted_at=time.time())
                except Exception: pass
        while True:
            try:
                groups=get_groups()
                reference=CACHE.get('_reference',{})
                if time.time()-reference.get('fetched_at',0)>86400 and time.time()-reference.get('attempted_at',0)>600:
                    CACHE.setdefault('_reference',{})['attempted_at']=time.time()
                    try:
                        response=await client.get(REFERENCE_URL,timeout=15)
                        response.raise_for_status()
                        selected=reference_sample(response.text,groups)
                        CACHE['_reference']=dict(symbols=selected,fetched_at=time.time(),attempted_at=time.time())
                    except Exception: pass
                reference=CACHE.get('_reference',{})
                if time.time()-reference.get('fetched_at',0)<7*86400:
                    groups.update({s:REFERENCE_GROUP for s in reference.get('symbols',[]) if s not in groups})
                due=sorted(groups,key=lambda s:CACHE.get(s,{}).get('attempted_at',0))
                due=[s for s in due if (CACHE.get(s,{}).get('schema')!=CACHE_SCHEMA or time.time()-CACHE.get(s,{}).get('fetched_at',0)>600) and time.time()-CACHE.get(s,{}).get('attempted_at',0)>60][:10]
                if time.time()-CACHE.get('_calendar',{}).get('attempted_at',0)>300: await fetch('_calendar')
                await asyncio.gather(*(fetch(s) for s in due))
                SNAPSHOT=build(CACHE,groups,known_splits=get_split_dates())
                for s in list(CACHE):
                    if not s.startswith('_') and s not in groups: del CACHE[s]
                if time.time()-saved_at>60:
                    await asyncio.to_thread(storage.save, {'market_pulse':CACHE}); saved_at=time.time()
            except Exception:
                SNAPSHOT={**SNAPSHOT,'status':'error','label':'تعذر تحديث نبض السوق'}
            await asyncio.sleep(8)
