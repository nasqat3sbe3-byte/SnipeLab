"""Cached breadth of the tracked universe, never an all-market claim.
Daily high and close returns use the previous split-adjusted daily close.
SPY's observed session dates supply the shared holiday-aware trading window.
"""
import asyncio
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import httpx
import storage

CACHE = {}
SNAPSHOT = {"status": "starting", "rows": [], "sessions": []}
NY = ZoneInfo('America/New_York')


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
            if not bar['high'] or not bar['close']: continue
            day = datetime.fromtimestamp(ts, NY).date().isoformat()
            bars[day] = dict(date=day, **bar)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return sorted(bars.values(), key=lambda b: b['date'])[-32:]


def build(cache, groups, now=None):
    now = now or datetime.now(timezone.utc)
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
                        advancing=0, holding=0, continued=0, prior_movers=0, volume_samples=0, elevated_volume=0) for d in window}
    rows = []
    covered = 0
    for sym, group in sorted(groups.items()):
        record = cache.get(sym, {})
        if now.timestamp() - record.get('fetched_at', 0) > 1800: continue
        bars = record.get('bars', [])
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
                s = sessions[day]; s['covered'] += 1
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
                    after_gain=round(after,2), behavior='واصل الصعود' if after>5 else 'متماسك' if after>=-5 else 'تراجع'))
        if movements:
            movements.sort(key=lambda e: e['date'], reverse=True)
            rows.append(dict(symbol=sym, group=group, price=bars[-1]['close'], price_date=bars[-1]['date'],
                             moves=movements, **movements[0]))
    series = list(sessions.values())
    today = series[-1]
    avg = sum(s['counts']['25'] for s in series[:-1])/9
    coverage = covered/max(1,len(groups))
    status, label = 'ready', 'نشاط محدود'
    reasons = []
    # Explicit heuristic; enough coverage and repeated breadth/retention required.
    if coverage < .7 or covered < 10:
        status, label = 'collecting', 'تغطية غير كافية للحكم'
    else:
        strong = [s['counts']['25'] >= max(3, avg*1.5) and s['holding'] >= max(1,s['counts']['10']*.5) and s['advancing']/covered>=.5 and s['elevated_volume'] >= max(2,covered*.05) for s in series[-3:]]
        if all(strong): label='نشاط واسع ومستمر'
        elif strong[-1]: label='بداية تحسّن'
        reasons.append(f"{today['counts']['25']} سهم وصل +25% مقابل متوسط {avg:.1f} في 9 جلسات سابقة")
        reasons.append(f"{today['holding']} سهم احتفظ بنصف صعوده على الأقل من المتحركين +10%")
    return dict(base, status=status, label=label, covered=covered, coverage=round(coverage*100),
                rows=sorted(rows,key=lambda r:(r['date'],r['gain']),reverse=True), sessions=series,
                counts=today['counts'], baseline=round(avg,2), reasons=reasons, as_of=window[-1],
                provisional=window[-1]==now.astimezone(NY).date().isoformat() and now.astimezone(NY).hour<16,
                note='تقييم وصفي للنطاق المتابع؛ النسب من الإغلاق السابق إلى أعلى الجلسة، والأعداد تراكمية. الأسعار اليومية وليست أسعارًا لحظية.')


async def worker(get_groups):
    global SNAPSHOT
    try: CACHE.update(storage.load(('market_pulse',)).get('market_pulse') or {})
    except Exception: pass
    await asyncio.sleep(8)
    async with httpx.AsyncClient(timeout=12, headers={'User-Agent':'Mozilla/5.0 SnipeLab'}) as client:
        sem=asyncio.Semaphore(2)
        async def fetch(sym):
            async with sem:
                try:
                    ticker='SPY' if sym=='_calendar' else sym
                    r=await client.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',params={'range':'3mo','interval':'1d','includePrePost':'false','events':'splits'})
                    r.raise_for_status()
                    bars=parse_chart(r.json()['chart']['result'][0])
                    if bars: CACHE[sym]=dict(bars=bars,fetched_at=time.time())
                except Exception: pass  # Retain cache, and expire its coverage in build().
        while True:
            try:
                groups=get_groups()
                due=sorted(groups,key=lambda s:CACHE.get(s,{}).get('fetched_at',0))
                due=[s for s in due if time.time()-CACHE.get(s,{}).get('fetched_at',0)>600][:8]
                if time.time()-CACHE.get('_calendar',{}).get('fetched_at',0)>300:
                    await fetch('_calendar')
                await asyncio.gather(*(fetch(s) for s in due))
                SNAPSHOT=build(CACHE,groups)
                # Drop removed symbols and persist independently from the main dashboard.
                for s in list(CACHE):
                    if s!='_calendar' and s not in groups: del CACHE[s]
                await asyncio.to_thread(storage.save, {'market_pulse':CACHE})
            except Exception:
                SNAPSHOT={**SNAPSHOT,'status':'error','label':'تعذر تحديث نبض السوق'}
            await asyncio.sleep(15)
