"""Observed-state focus ranking. No forecasts, new market requests or core writes."""
import asyncio
import math
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import storage

MEMORY = {}
EVENTS = []
SNAPSHOT = {'picks': [], 'watching': [], 'status': 'starting', 'events': []}
VERSION = 2


def number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def fresh(stamp, at, seconds=1800):
    try:
        age = (at-datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds()
        return 0 <= age <= seconds
    except (ValueError, TypeError, AttributeError):
        return False


def evaluate(row, memory, risk, at):
    sym = row['symbol']
    price, support, av, rsi = [number(row.get(k)) for k in ('price','support','available','rsi')]
    known = all(x is not None for x in (price,support,av,rsi))
    if not known or price <= 0 or support <= 0:
        return None
    if not 0 < price < 5 or not 0 <= av <= 25000 or rsi > 40:
        return None
    if not row.get('verified') or not fresh(row.get('price_at'),at) or not fresh(row.get('borrow_at'),at):
        return None
    entry = memory.setdefault(sym, {'first_seen': at.isoformat(), 'first_price': price,
                                    'previous_support': support, 'borrow_samples': []})
    previous_support = number(entry.get('previous_support'))
    if previous_support and support < previous_support:
        entry['broken_support'] = previous_support
    entry['previous_support'] = support
    entry['last_seen'] = at.isoformat()
    samples = entry.setdefault('borrow_samples', [])
    stamp = row.get('borrow_at')
    if not samples or samples[-1]['at'] != stamp:
        samples.append({'at': stamp, 'available': av})
    samples[:] = [s for s in samples if fresh(s['at'],at,3*86400)][-100:]
    recent = samples[-3:]
    drying = len(recent)==3 and recent[0]['available']>recent[1]['available']>recent[2]['available']
    sessions = int(row.get('sessions') or 0)
    broken = bool(row.get('broken') or price < support)
    if broken:
        entry['broken_support'] = max(number(entry.get('broken_support')) or 0, support)
    reclaim_level = number(entry.get('broken_support'))
    # Retest is a dated, independently observed confirmation, not a price touch.
    try:
        retest_age = (at.date()-datetime.fromisoformat(str(row.get('retest_day'))[:10]).date()).days
    except (ValueError,TypeError): retest_age = 999
    retest = bool(row.get('retest') and 0 <= retest_age <= 14 and fresh(row.get('confirmation_at'),at,21600) and not broken)
    higher_low = row.get('higher_low') is True and not broken
    reclaimed = bool(reclaim_level and price >= reclaim_level and sessions >= 2 and not broken)
    distance = (price/support-1)*100
    volume = number(row.get('up_down_volume_ratio'))
    volume_ok = volume is not None and volume > 1
    confirmations = sum((retest,higher_low,reclaimed))
    ready = not broken and sessions >= 2 and 0 <= distance <= 20 and confirmations > 0
    reasons = ['ثبات '+str(sessions)+' جلسات فوق القاع'] if not broken else ['كسر الدعم؛ الجاهزية السابقة ملغاة']
    for ok, label in ((retest,'إعادة اختبار ناجحة'),(higher_low,'قاع الجلسة الأخيرة أعلى من السابقة'),
                      (reclaimed,'استرجع الدعم المكسور'),(drying,'Available يتناقص عبر 3 قراءات'),
                      (volume_ok,'متوسط حجم جلسات الصعود أعلى من الهبوط')):
        if ok: reasons.append(label)
    missing = []
    if volume is None: missing.append('مقارنة أحجام الصعود والهبوط غير متاحة')
    if not drying: missing.append('تناقص Available المتتالي لم يتأكد')
    if not ready: missing.append('تحتاج تأكيدًا سعريًا مع ثبات جلستين وقرب من الدعم')
    risk_state = 'blocked' if risk.get('blocked') else 'checked' if risk.get('checked') else 'pending'
    stage = 'broken' if broken else 'confirmed' if ready and retest else 'recovering' if ready else 'watch'
    if risk_state == 'blocked': stage='risk'
    priority = (25 if retest else 0)+(20 if higher_low else 0)+(20 if reclaimed else 0)+(15 if drying else 0)+(10 if volume_ok else 0)+min(10,max(0,sessions)*2.5)
    first_price = number(entry.get('first_price'))
    return {'symbol':sym,'price':price,'support':support,'available':av,'rsi':rsi,
            'sessions':0 if broken else sessions,'distance_pct':round(distance,2),
            'priority':round(priority,1),'stage':stage,'risk_state':risk_state,
            'eligible':ready and risk_state=='checked','reasons':reasons,'missing':missing,
            'first_seen':entry['first_seen'],'first_price':first_price,
            'observed_change_pct':round((price/first_price-1)*100,2) if first_price else None,
            'confirmation_waiting':'الحفاظ على الدعم وإعادة اختبار ناجحة' if not retest else 'استمرار الثبات دون كسر الدعم',
            'invalidation':'كسر الدعم '+str(round(support,4))+' أو ظهور خطر رسمي موثق',
            'source':row.get('source'),'market':row.get('market') or ('low_float' if row.get('source')=='الفري فلوت المنخفض' else 'split'),'href':row.get('href','/dashboard'),
            'price_at':row.get('price_at'),'borrow_at':row.get('borrow_at')}


def update(rows, risks, at=None):
    global SNAPSHOT
    at = at or datetime.now(timezone.utc)
    ranked=[]
    for row in rows:
        result=evaluate(row,MEMORY,risks(row['symbol']),at)
        if result is not None:ranked.append(result)
    ranked.sort(key=lambda x:(-x['priority'],x['available'],x['symbol']))
    picks=[x for market in ('split','low_float') for x in ranked if x['eligible'] and x['market']==market]
    picks=[x for market in ('split','low_float') for x in [v for v in picks if v['market']==market][:5]]
    selected={x['symbol'] for x in picks}
    for x in ranked:
        entry=MEMORY[x['symbol']]
        state=(x['stage'],x['risk_state'],x['symbol'] in selected)
        previous=entry.get('state')
        if (previous is not None and list(state)!=previous) or (previous is None and x['symbol'] in selected):
            EVENTS.insert(0,{'symbol':x['symbol'],'at':at.isoformat(),'stage':x['stage'],
                             'selected':x['symbol'] in selected,'risk_state':x['risk_state'],'market':x['market']})
        entry['state']=list(state)
        entry['last_result']=x
    cutoff=at-timedelta(days=30)
    for sym in list(MEMORY):
        try: old=datetime.fromisoformat(MEMORY[sym].get('last_seen',MEMORY[sym]['first_seen']))<cutoff
        except (ValueError,KeyError): old=True
        if old: MEMORY.pop(sym)
    active={x['symbol'] for x in ranked}
    for sym,entry in MEMORY.items():
        if sym not in active and entry.get('state') and entry['state'][2]:
            EVENTS.insert(0,{'symbol':sym,'at':at.isoformat(),'stage':'stale','selected':False,'risk_state':'pending','market':(entry.get('last_result') or {}).get('market') or ('low_float' if (entry.get('last_result') or {}).get('source')=='الفري فلوت المنخفض' else 'split')})
            entry['state']=['stale','pending',False]
    EVENTS[:]=EVENTS[:50]
    # Keep remembered broken/ineligible symbols visible, never pretend old data is live.
    dormant=[{**v['last_result'],'eligible':False,'stage':'stale'} for s,v in MEMORY.items()
             if s not in {x['symbol'] for x in ranked} and v.get('last_result')]
    pools={}
    for market in ('split','low_float'):
        def belongs(x):
            remembered=(MEMORY.get(x.get('symbol')) or {}).get('last_result') or {}
            origin=x.get('market') or remembered.get('market')
            source=x.get('source') or remembered.get('source')
            return (origin or ('low_float' if source=='الفري فلوت المنخفض' else 'split'))==market
        market_picks=[x for x in picks if belongs(x)]
        pools[market]={'version':VERSION,'market':market,'generated_at':at.isoformat(),
                      'status':'ready' if market_picks else 'watching',
                      'picks':market_picks,
                      'watching':[x for x in ranked if belongs(x) and x['symbol'] not in selected][:30],
                      'remembered':[x for x in dormant if belongs(x)][:30],
                      'events':[x for x in EVENTS if belongs(x)][:20],
                      'risk_pending':[x['symbol'] for x in ranked if belongs(x) and x['risk_state']=='pending'][:15],
                      'note':'ترتيب متابعة تجريبي حسب بيانات مرصودة؛ الدرجة ليست احتمال صعود. لم يُختبر تاريخيًا بعد. لا نملأ الخمسة بأسهم ناقصة التأكيد أو الفحص.'}
    SNAPSHOT={**pools['split'],'pools':pools}
    return SNAPSHOT


async def worker(collect, risks, queue):
    try:
        saved=await asyncio.to_thread(storage.load,('focus_v1',))
        value=saved.get('focus_v1') or {}
        MEMORY.update(value.get('memory') or {});EVENTS[:]=value.get('events') or []
    except Exception: pass
    await asyncio.sleep(5)
    count=0
    while True:
        try:
            result=update(collect(),risks)
            queue([s for pool in result.get('pools',{}).values() for s in pool['risk_pending']])
            if count%4==0:
                await asyncio.to_thread(storage.save,{'focus_v1':{'memory':deepcopy(MEMORY),'events':list(EVENTS)}})
            count+=1
        except Exception as exc:
            SNAPSHOT.update(status='error',error=type(exc).__name__,picks=[])
            for pool in SNAPSHOT.get('pools',{}).values():pool.update(status='error',picks=[])
        await asyncio.sleep(15)
