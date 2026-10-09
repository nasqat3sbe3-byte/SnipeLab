"""Optional, isolated 4H chart cache. Never writes core prices/history/analytics."""
import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import storage

NY = ZoneInfo('America/New_York')
CACHE = {}
META = {}
MAX_CACHED_CHARTS = 16
_HOT = set()
PRIORITY = {}
OBSERVATIONS = {}
WAKE = asyncio.Event()
REFRESH_SECONDS = 21600
WORKER_STATE = {"state":"starting","successes":0,"failures":0}

def aggregate_hourly(result, now=None):
    now = now or datetime.now(timezone.utc)
    q = result['indicators']['quote'][0]
    tz = ZoneInfo(result.get('meta', {}).get('exchangeTimezoneName') or 'America/New_York')
    groups = {}
    for i, stamp in enumerate(result.get('timestamp', [])):
        try:
            values = {k: float(q[k][i]) for k in ('open', 'high', 'low', 'close')}
            if not all(math.isfinite(v) and v > 0 for v in values.values()):
                continue
            if values['low'] > min(values['open'], values['close']) or values['high'] < max(values['open'], values['close']):
                continue
            dt = datetime.fromtimestamp(stamp, tz)
            if not 4 <= dt.hour < 20:
                continue
            # Hour bars in the regular session start at 09:30, not :00.
            # Keep pre/regular/post sessions separate so a source bar never
            # straddles a synthetic 4H boundary. End-of-session bars are shorter.
            minutes = dt.hour * 60 + dt.minute
            if minutes < 570:
                anchor, end_minutes = (240,480) if minutes < 480 else (480,570)
            elif minutes < 960:
                anchor, end_minutes = (570,810) if minutes < 810 else (810,960)
            else:
                anchor, end_minutes = 960,1200
            start = dt.replace(hour=anchor//60, minute=anchor%60, second=0, microsecond=0)
            key = int(start.timestamp())
            groups.setdefault(key, []).append((stamp, values, dt))
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
    candles = []
    for stamp, parts in sorted(groups.items()):
        parts.sort(key=lambda v: v[0])
        start = datetime.fromtimestamp(stamp, tz)
        start_minutes = start.hour * 60 + start.minute
        duration = 90 if start_minutes == 480 else 150 if start_minutes == 810 else 240
        extended = any(d.hour < 9 or (d.hour == 9 and d.minute < 30) or d.hour >= 16 for _, _, d in parts)
        candles.append({'time': stamp, 'date': start.date().isoformat(),
                        'local_time': start.strftime('%Y-%m-%d %H:%M'),
                        'open': parts[0][1]['open'], 'high': max(v['high'] for _, v, _ in parts),
                        'low': min(v['low'] for _, v, _ in parts), 'close': parts[-1][1]['close'],
                        'extended': extended, 'samples': len(parts),
                        'session_partial': duration < 240,
                        'hourly_parts': [{'time':t,'date':d.date().isoformat(),'local_time':d.strftime('%Y-%m-%d %H:%M'),**v} for t,v,d in parts],
                        'closed': now.timestamp() >= stamp + duration * 60})
    return candles

def describe(candles, support, support_date, today=None):
    """Closed-candle Retest against unchanged core support; no core mutations."""
    today = today or datetime.now(NY).date().isoformat()
    out = {'support': support, 'support_date': support_date, 'touch_tolerance_pct': 5,
           'basis': '4h_available_extended_data', 'formation_time': None,
           'age_sessions': None, 'retest': {'status': 'unavailable'}}
    if not candles or not support or not support_date:
        return out
    sessions = sorted({b['date'] for b in candles if support_date < b['date'] < today})
    out['age_sessions'] = len(sessions) if candles[0]['date'] <= support_date else None
    formation = [b for b in candles if b['date'] == support_date and abs(b['low'] / support - 1) <= .005]
    # The low date comes from core data. Its exact intraday time must be observed.
    formed = min(formation, key=lambda b: b['low']) if formation else None
    if formed:
        out['formation_time'] = formed['local_time']
    later = [b for b in candles if b['closed'] and
             (b['time'] > formed['time'] if formed else b['date'] > support_date)]
    # Return must follow a completed candle that left the 5% support zone.
    # A close below support invalidates success until the core support changes.
    out['touch_tolerance_pct'] = 5
    out['touch_count'] = 0
    escaped = False
    out['retest'] = {'status': 'not_tested'}
    for bar in later:
        if bar['close'] < support:
            status = 'close_breach'
        elif escaped and bar['low'] <= support * 1.05 and bar['high'] >= support:
            status = 'wick_reclaim' if bar['low'] < support else 'touch_held'
            out['touch_count'] += 1
        else:
            if bar['close'] > support * 1.05:
                escaped = True
            continue
        out['retest'] = {'status': status, 'time': bar['local_time'], 'low': bar['low'],
                         'close': bar['close'], 'extended': bar['extended'],
                         'partial': bar['samples'] < 3, 'candle_time': bar['time']}
        if status == 'close_breach':
            break

    return out

def cache_info(symbol):
    stored=CACHE.get(symbol) or META.get(symbol) or {}
    return {**stored,**{k:v for k,v in (META.get(symbol) or {}).items() if k in ('error','attempted_at')}}

def support_key(h):
    return (h.get('effective_date'),h.get('verified'),h.get('post_split_low'),h.get('post_split_low_date'))

def compact_summary(stored,h):
    support=h.get('post_split_low') if h.get('verified') else None
    try:
        support=float(support)
        if not math.isfinite(support) or support<=0:support=None
    except (TypeError,ValueError):support=None
    bars=stored.get('candles') or []
    if stored.get('split_date')!=h.get('effective_date'):bars=[]
    result=describe(bars,support,h.get('post_split_low_date'))
    return {**{k:stored.get(k) for k in ('fetched_epoch','updated_at','split_date','hourly_version','error')},
            'support_key':support_key(h),'observation':result,
            'sessions':sorted({b['date'] for b in bars}),
            'coverage_start':bars[0]['date'] if bars else None}

def retain(symbol,stored):
    CACHE.pop(symbol,None);CACHE[symbol]=stored
    while len(CACHE)>MAX_CACHED_CHARTS:
        victim=next((s for s in CACHE if s!=symbol and s not in _HOT),None)
        if victim is None:victim=next(s for s in CACHE if s!=symbol)
        CACHE.pop(victim,None)
        OBSERVATIONS.pop(victim,None)

def restore_summaries(universe,history,hot):
    summaries={};charts={}
    names=tuple('support_chart:'+s for s in universe)
    for name,stored in storage.iter_snapshots(names):
        symbol=name.split(':',1)[1]
        summaries[symbol]=compact_summary(stored,history.get(symbol) or {})
        if symbol in hot and len(charts)<MAX_CACHED_CHARTS:charts[symbol]=stored
    return summaries,charts

async def load_cached(symbol,history):
    """A disk cache read only: opening an evicted chart makes no provider request."""
    stored=CACHE.get(symbol)
    if stored:
        retain(symbol,stored);return
    saved=await asyncio.to_thread(storage.load,('support_chart:'+symbol,))
    stored=saved.get('support_chart:'+symbol)
    if stored:
        META[symbol]=compact_summary(stored,history.get(symbol) or {})
        retain(symbol,stored)

def observation(symbol, history):
    """Compact retest facts remain available even when full candles leave RAM."""
    h=history.get(symbol) or {};stored=cache_info(symbol)
    today=datetime.now(NY).date().isoformat()
    key=(stored.get('fetched_epoch'),stored.get('split_date'),*support_key(h),today)
    previous=OBSERVATIONS.get(symbol)
    if previous and previous[0]==key:return previous[1]
    full=CACHE.get(symbol)
    if full and full.get('candles'):
        compact=compact_summary(full,h);META[symbol]=compact
    else:compact=META.get(symbol) or {}
    if compact.get('support_key')==support_key(h):
        result=dict(compact['observation']);support_date=h.get('post_split_low_date')
        if support_date and compact.get('coverage_start') and compact['coverage_start']<=support_date:
            result['age_sessions']=sum(support_date<d<today for d in compact.get('sessions',[]))
    else:result=describe([],None,None)
    OBSERVATIONS[symbol]=(key,result)
    return result

def retest_signal(symbol, history):
    details = observation(symbol, history)
    result = details['retest']
    status = result['status']
    return {'support_retest_status': 'success' if status in ('touch_held', 'wick_reclaim') else
            'failed' if status == 'close_breach' else 'waiting',
            'support_retest_time': result.get('time'),
            'support_retest_observation': status,
            'support_retest_tolerance_pct': 5,
            'support_retest_basis': 'closed_4h_extended',
            'support_retest_updated_at': cache_info(symbol).get('updated_at')}

def get(symbol, history):
    h = history.get(symbol) or {}
    support = h.get('post_split_low') if h.get('verified') else None
    support_date = h.get('post_split_low_date') if h.get('verified') else None
    stored = CACHE.get(symbol) or {}
    now = time.time()
    if h.get('verified') and now - stored.get('fetched_epoch', 0) > 300 and symbol not in PRIORITY:
        if len(PRIORITY) < 8:
            PRIORITY[symbol] = now
            WAKE.set()
    bars = stored.get('candles') or []
    split_same = stored.get('split_date') == h.get('effective_date')
    if not split_same:
        bars = []
    try:
        support = float(support) if support is not None and float(support) > 0 else None
    except (ValueError, TypeError):
        support = None
    return {'symbol': symbol, 'interval': '4h', 'timezone': 'America/New_York',
            'source': 'Yahoo hourly with includePrePost=true', 'candles': bars,
            'updated_at': stored.get('updated_at'), 'status': 'ready' if bars else 'pending',
            'refresh_error': cache_info(symbol).get('error'), 'last_refresh_attempt':cache_info(symbol).get('attempted_at'), 'stale': now - stored.get('fetched_epoch', 0) > REFRESH_SECONDS,
            'extended_observed': any(b.get('extended') for b in bars),
            'coverage_note': 'الشموع مبنية من بيانات الساعة المتاحة، بما فيها الساعات الممتدة التي يوفرها المصدر. بعض الفترات قد تكون ناقصة.',
            'details': observation(symbol, history),
            'split_date': h.get('effective_date')}

async def fetch(client, symbol, h):
    base = h.get('effective_date') or h.get('post_split_low_date')
    if not base:
        return
    start = datetime.fromisoformat(base).replace(tzinfo=NY) - timedelta(days=10)
    now = datetime.now(timezone.utc)
    start = max(start, now - timedelta(days=720))
    r = await client.get('https://query1.finance.yahoo.com/v8/finance/chart/' + symbol,
                         params={'interval': '60m', 'period1': int(start.timestamp()),
                                 'period2': int(now.timestamp()), 'includePrePost': 'true'}, timeout=15)
    r.raise_for_status()
    result = (r.json().get('chart', {}).get('result') or [None])[0]
    if not result:
        raise ValueError('chart data unavailable')
    candles = aggregate_hourly(result, now)
    if not candles:
        raise ValueError('no valid hourly candles')
    stored={'candles':candles[-1600:],'split_date':h.get('effective_date'),
            'updated_at':now.isoformat(),'fetched_epoch':time.time(),'error':None,'hourly_version':1}
    META[symbol]=compact_summary(stored,h)
    retain(symbol,stored)
    # Save only this optional cache entry, not any core collection.
    await asyncio.to_thread(storage.save, {'support_chart:' + symbol: stored})

def hunt_candidates(universe,history,quotes,borrow):
    hot=set()
    for symbol in universe:
        try:
            half=float((history.get(symbol) or {})['split_day_4h_high'])/2
            price=float((quotes.get(symbol) or {})['price']);available=float((borrow.get(symbol) or {})['available'])
            if half>0 and 0<=available<=40000 and half*.65<=price<=half*.8:hot.add(symbol)
        except (KeyError,ValueError,TypeError):pass
    return hot

async def worker(universe, history,quotes=None,borrow=None):
    await asyncio.sleep(5)
    try:
        _HOT.update(hunt_candidates(universe,history,quotes or {},borrow or {}))
        summaries,charts=await asyncio.to_thread(restore_summaries,list(universe),dict(history),set(_HOT))
        META.update(summaries)
        for symbol,stored in charts.items():retain(symbol,stored)
    except Exception:
        pass
    attempts = {}
    async with httpx.AsyncClient(headers={'User-Agent': 'Mozilla/5.0'}, follow_redirects=True) as client:
        while True:
            try:
                now = time.time()
                _HOT.clear();_HOT.update(hunt_candidates(universe,history,quotes or {},borrow or {}))
                # Load qualifying persisted charts into RAM without re-downloading them.
                for symbol in sorted(_HOT)[:MAX_CACHED_CHARTS]:
                    if symbol not in CACHE:await load_cached(symbol,history)
                eligible = [s for s in universe if history.get(s, {}).get('verified') and
                            now - attempts.get(s, 0) > 120 and
                            (s in PRIORITY or cache_info(s).get('hourly_version',0)<1 or now - cache_info(s).get('fetched_epoch', 0) > REFRESH_SECONDS
                             or cache_info(s).get('split_date') != history[s].get('effective_date'))]
                eligible.sort(key=lambda s: (s not in PRIORITY, attempts.get(s, 0)))
                if eligible:
                    symbol = eligible[0]
                    PRIORITY.pop(symbol, None)
                    attempts[symbol] = now
                    stamp=datetime.now(timezone.utc).isoformat()
                    WORKER_STATE.update(state='fetching',symbol=symbol,attempted_at=stamp,queued=len(PRIORITY))
                    META.setdefault(symbol,{})['attempted_at']=stamp
                    try:
                        await fetch(client, symbol, dict(history[symbol]))
                        WORKER_STATE.update(state='waiting',last_success=stamp,error=None)
                        WORKER_STATE['successes']+=1
                    except Exception as exc:
                        error=f'{type(exc).__name__}: {str(exc)[:180]}'
                        META.setdefault(symbol,{})['error']=error
                        WORKER_STATE.update(state='waiting',error=error)
                        WORKER_STATE['failures']+=1
                    # One request at a time with a cooldown. Chart opening never
                    # triggers synchronous data retrieval or historical analysis.
                    await asyncio.sleep(5)
                    continue
            except Exception as exc:
                WORKER_STATE.update(state='error',error=f'{type(exc).__name__}: {str(exc)[:180]}')
            WAKE.clear()
            try:
                await asyncio.wait_for(WAKE.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
