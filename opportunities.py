"""Isolated, cache-only readiness diary. Never changes screening collections."""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import storage

NY=ZoneInfo('America/New_York')
ROWS={}
META={'updated_at':None,'error':None}

def nth(y,m,w,n):
    d=date(y,m,1)
    return d+timedelta(days=(w-d.weekday())%7+7*(n-1))

def observed(d):
    return d+timedelta(days=1) if d.weekday()==6 else d-timedelta(days=1) if d.weekday()==5 else d

def holidays(y):
    # NYSE recurring holidays. Saturday New Year's does not close Friday.
    a=y%19;b=y//100;c=y%100;d=b//4;e=b%4;f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30;i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7;m=(a+11*h+22*l)//451
    easter=date(y,(h+l-7*m+114)//31,(h+l-7*m+114)%31+1)
    ny=date(y,1,1)
    result={ny+timedelta(days=1) if ny.weekday()==6 else ny,nth(y,1,0,3),nth(y,2,0,3),easter-timedelta(days=2),nth(y,9,0,1),nth(y,11,3,4),observed(date(y,7,4)),observed(date(y,12,25))}
    memorial=date(y,5,31);result.add(memorial-timedelta(days=memorial.weekday()))
    if y>=2022:result.add(observed(date(y,6,19)))
    if y==2025:result.add(date(2025,1,9))
    return result

def trading(d):
    return d.weekday()<5 and d not in holidays(d.year)

def advance(d,n):
    for _ in range(n):
        d+=timedelta(days=1)
        while not trading(d):d+=timedelta(days=1)
    return d

def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

def review_date(now):
    local=now.astimezone(NY)
    if trading(local.date()) and local.hour<16:
        return local.date().isoformat()
    return advance(local.date(),1).isoformat()

def usable_reading(freshness, now):
    if freshness.get('status')=='fresh':return True
    local=now.astimezone(NY)
    # Keep last-session readings usable while the exchange is closed.
    # Older sessions never become current just because today is a weekend.
    if trading(local.date()) and 4<=local.hour<20:return False
    last=local.date()-timedelta(days=1)
    while not trading(last):last-=timedelta(days=1)
    if trading(local.date()) and local.hour>=20:last=local.date()
    try:
        stamp=datetime.fromisoformat(freshness['timestamp'].replace('Z','+00:00')).astimezone(NY)
        return stamp.date()>=last and stamp<=local
    except (KeyError,TypeError,ValueError):return False

def evaluate(row,previous=None,now=None):
    now=now or datetime.now(timezone.utc);today=now.astimezone(NY).date()
    previous=previous or {};s=row.get('signal') or {};q=row.get('price') or {};b=row.get('borrow') or {}
    av=number(b.get('available'));rsi=number(s.get('rsi_daily'));dist=number(s.get('effective_distance_pct'))
    sessions=int(number(s.get('stability_sessions')) or 0);support=number(s.get('post_split_low'))
    # Diary forecasts 4/4 maturity; existing 2/4 screener stays untouched.
    target=4
    missing=[]
    if not s.get('history_verified') or support is None:missing.append('تحديد الدعم — بيانات ناقصة')
    if av is None or av>=15000:missing.append('Available')
    if rsi is None or rsi>35:missing.append('RSI')
    if s.get('half_reached') is not True:missing.append('نصف القمة')
    if dist is None or not 0<=dist<=25:missing.append('القرب من الدعم')
    if s.get('support_retest_status')!='success':missing.append('إعادة اختبار الدعم')
    if s.get('surge70_verified') and (number(s.get('surge70_sessions_since_peak')) or 0)<10:
        missing.append('انتهاء فترة متابعة الحركة السابقة')
    if s.get('new_low_today'):
        sessions=0
        missing.append('تأكيد الدعم الجديد')
    if sessions<target:missing.append('الثبات')
    candidate=(len(missing)<=(3 if s.get('new_low_today') else 2) and av is not None and av<15000 and rsi is not None and rsi<=35 and s.get('half_reached') is True and dist is not None and 0<=dist<=25)
    if not candidate and not previous:return None
    remaining=max(0,target-sessions)
    state='waiting';expected=None;reason='بانتظار '+ '، '.join(missing)
    waiting_label='بانتظار '+ '، '.join(missing)
    data_issue=None
    fresh=all(usable_reading(s.get(key) or {},now) for key in ('quote_freshness','borrow_freshness'))
    if s.get('new_low_today'):
        reason='قاع جديد — بانتظار تأكيد الدعم وإعادة حساب الثبات'
        waiting_label='بانتظار تأكيد الدعم الجديد'
    elif s.get('support_retest_status')=='failed':reason='كُسر الدعم — بانتظار إعادة التقييم'
    elif fresh and all(x=='الثبات' for x in missing):
        try:anchor=date.fromisoformat(str(s.get('post_split_low_date'))[:10])
        except ValueError:anchor=None
        if anchor:
            expected=advance(anchor,target)
            if expected<=today and remaining:
                # Never backdate a forecast using delayed histories.
                earliest=today if trading(today) else advance(today,1)
                if now.astimezone(NY).hour>=16:earliest=advance(today,1)
                expected=advance(earliest,remaining-1)
            if not remaining:
                expected=date.fromisoformat(previous['expected_date']) if previous.get('state')=='ready' and previous.get('expected_date') else today
                state='ready';reason='اكتملت الشروط الحالية'
            else:state='scheduled';reason='إذا استمرت الشروط وحافظ على الدعم'
            expected=expected.isoformat()
    elif not fresh:
        state='data_pending';reason='المتابعة مستمرة — بانتظار تحديث القراءة القديمة'
        stale=[]
        if not usable_reading(s.get('quote_freshness') or {},now):stale.append('السعر')
        if not usable_reading(s.get('borrow_freshness') or {},now):stale.append('Available')
        data_issue='بانتظار تحديث '+ ' و'.join(stale)
    # Earlier 2/4 window is separate from the diary's 4/4 maturity.
    early_date=None;early_state='waiting'
    if fresh and all(x=='الثبات' for x in missing) and not s.get('new_low_today'):
        try:
            anchor=date.fromisoformat(str(s.get('post_split_low_date'))[:10])
            early=advance(anchor,2)
            early_remaining=max(0,2-sessions)
            if early_remaining and early<=today:
                early=advance(date.fromisoformat(review_date(now)),early_remaining-1)
            early_date=early.isoformat()
            early_state='ready' if sessions>=2 else 'scheduled'
        except ValueError:pass
    plan=[{'label':f'المحافظة على الدعم ${support:.4f}' if support is not None else 'تأكيد مستوى الدعم','met':support is not None and not s.get('new_low_today') and s.get('support_retest_status')!='failed'},
          {'label':'Available أقل من 15,000','met':av is not None and av<15000},
          {'label':'RSI اليومي 35 أو أقل','met':rsi is not None and rsi<=35},
          {'label':'تحقق نصف القمة','met':s.get('half_reached') is True},
          {'label':'البعد عن الدعم 25% أو أقل','met':dist is not None and 0<=dist<=25},
          {'label':'إعادة اختبار ناجحة ضمن 5% فوق الدعم','met':s.get('support_retest_status')=='success' and not s.get('new_low_today')},
          {'label':'جاهزية 2/4','met':sessions>=2 and not s.get('new_low_today')},
          {'label':'اكتمال الثبات 4/4','met':sessions>=4 and not s.get('new_low_today')}]
    invalidators=['كسر الدعم أو تكوين قاع جديد','ارتفاع Available إلى 15,000 أو أكثر','ارتفاع RSI اليومي فوق 35','ابتعاد السعر عن الدعم بأكثر من 25%','فشل إعادة اختبار الدعم','غياب قراءة حديثة كافية لحساب الموعد']
    events=list(previous.get('history') or [])
    changed=False
    if previous and (previous.get('expected_date')!=expected or previous.get('state')!=state or previous.get('support')!=support or ('retest_status' in previous and previous.get('retest_status')!=s.get('support_retest_status'))):
        changed=True
        change_reason='قاع جديد — أُعيد حساب الثبات' if previous.get('support') is not None and support is not None and support<previous['support'] else reason
        kind='completed' if state=='ready' and previous.get('state')!='ready' else 'support_broken' if s.get('support_retest_status')=='failed' and previous.get('retest_status')!='failed' else 'advanced' if previous.get('expected_date') and expected and expected<previous['expected_date'] else 'delayed' if previous.get('expected_date') and expected and expected>previous['expected_date'] else 'suspended' if previous.get('expected_date') and not expected else 'changed'
        events.append({'at':now.isoformat(),'kind':kind,'old_date':previous.get('expected_date'),'new_date':expected,'reason':change_reason})
    if previous and 'early_state' in previous and early_state=='ready' and previous.get('early_state')!='ready':
        events.append({'at':now.isoformat(),'kind':'early_ready','old_date':previous.get('early_date'),'new_date':early_date,'reason':'اكتملت شروط جاهزية 2/4'})
    last_change=events[-1] if events else None
    change_kind=None
    if last_change:
        old,new=last_change.get('old_date'),last_change.get('new_date')
        change_kind='تأجل' if old and new and new>old else 'تقدّم' if old and new and new<old else 'عُلّق الموعد' if old and not new else 'حُدد الموعد' if new and not old else 'تغيّرت الحالة'
    return {'early_date':early_date,'early_state':early_state,'retest_status':s.get('support_retest_status'),'plan':plan,'invalidators':invalidators,'retest_zone':{'low':support,'high':round(support*1.05,6)} if support is not None and support>0 else None,'borrow_read_at':(s.get('borrow_freshness') or {}).get('timestamp'),'using_last_session':fresh and any((s.get(k) or {}).get('status')!='fresh' for k in ('quote_freshness','borrow_freshness')),'next_review_date':review_date(now),'support_distance_pct':dist,'waiting_label':waiting_label,'data_issue':data_issue,'last_change':last_change,'change_kind':change_kind,'symbol':row['symbol'],'company':row.get('company_name'),'price':q.get('price'),'available':av,'rsi':rsi,'support':support,'sessions':sessions,'target_sessions':target,'remaining_sessions':remaining,'expected_date':expected,'state':state,'reason':reason,'missing':missing,'history':events[-20:],'rescheduled':changed or bool(previous.get('rescheduled')),'added_at':previous.get('added_at') or now.isoformat(),'reviewed_at':now.isoformat(),'four_session_date':advance(date.fromisoformat(expected),max(0,4-max(target,sessions))).isoformat() if expected else None}

async def worker(snapshot):
    try:ROWS.update(storage.load(['opportunity_diary']).get('opportunity_diary') or {})
    except Exception as exc:META['error']=type(exc).__name__
    await asyncio.sleep(20)
    while True:
        try:
            data=await snapshot()
            for sym,row in data.get('rows',{}).items():
                result=evaluate(row,ROWS.get(sym))
                if result:ROWS[sym]=result
            storage.save({'opportunity_diary':ROWS})
            META.update(updated_at=datetime.now(timezone.utc).isoformat(),error=None)
        except Exception as exc:META['error']=type(exc).__name__
        await asyncio.sleep(60)

def daily_changes(now=None):
    now=now or datetime.now(timezone.utc)
    local_day=now.astimezone(ZoneInfo('Asia/Riyadh')).date()
    result=[]
    for row in ROWS.values():
        for event in row.get('history') or []:
            try:
                if datetime.fromisoformat(event['at']).astimezone(ZoneInfo('Asia/Riyadh')).date()!=local_day:continue
            except (KeyError,ValueError):continue
            result.append({**event,'symbol':row['symbol']})
    result.sort(key=lambda x:x['at'],reverse=True)
    counts={k:len({e['symbol'] for e in result if e.get('kind')==k}) for k in ('completed','early_ready','advanced','delayed','support_broken','suspended','changed')}
    return {'date':local_day.isoformat(),'counts':counts,'events':result[:50]}

def payload():
    return {**META,'daily_changes':daily_changes(),'rows':sorted(ROWS.values(),key=lambda x:(x['expected_date'] is None,x['expected_date'] or '9999',x['available'] if x['available'] is not None else math.inf)),'target_sessions':4,'next_session_date':advance(datetime.now(NY).date(),1).isoformat()}
