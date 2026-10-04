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

def ready_list_status(row):
    """Mirror dashboard.html readyListCandidate, including its fallback gate."""
    s=row.get('signal') or {};b=row.get('borrow') or {}
    av=number(b.get('available'));rsi=number(s.get('rsi_daily'))
    dist=number(s.get('effective_distance_pct'));sessions=number(s.get('stability_sessions'))
    missing=[]
    if not s.get('history_verified'):missing.append('بيانات القاع غير مؤكدة')
    if av is None:missing.append('قراءة Available غير متوفرة')
    elif av>=15000:missing.append('Available أقل من 15 ألف')
    if rsi is None:missing.append('قراءة RSI غير متوفرة')
    elif rsi>35:missing.append('RSI عند 35 أو أقل')
    if s.get('half_reached') is not True:missing.append('لمس نصف القمة')
    if sessions is None or sessions<2:missing.append('الثبات جلستين على الأقل')
    # Main list explicitly admits its near-low fallback up to 30%.
    if dist is None or not 0<=dist<=30:missing.append('البعد عن القاع 30% أو أقل')
    for prefix in ('surge70','top_10'):
        gain=number(s.get(prefix+'_gain_pct'));since=number(s.get(prefix+'_sessions_since_peak'))
        if s.get(prefix+'_verified') and gain is not None and gain>=70 and since is not None and 0<=since<10:
            missing.append('انتهاء متابعة حركة +70%');break
    return not missing,missing

def evaluate(row,previous=None,now=None):
    now=now or datetime.now(timezone.utc);today=now.astimezone(NY).date()
    previous=previous or {};s=row.get('signal') or {};q=row.get('price') or {};b=row.get('borrow') or {}
    av=number(b.get('available'));rsi=number(s.get('rsi_daily'));dist=number(s.get('effective_distance_pct'))
    sessions=int(number(s.get('stability_sessions')) or 0);support=number(s.get('post_split_low'))
    # This diary monitors the low independently of the main screener.
    target=4
    gain=number(s.get('top_10_gain_pct'))
    since=number(s.get('top_10_sessions_since_peak'))
    recent_move=bool(s.get('top_10_verified') and gain is not None and gain>=60 and since is not None and 0<=since<10)
    surge_since=number(s.get('surge70_sessions_since_peak'))
    recent_move=recent_move or bool(s.get('surge70_verified') and surge_since is not None and 0<=surge_since<10)
    eligible_available=av is not None and 0<=av<20000
    half=number(s.get('half_level'));price=number(q.get('price'))
    half_met=s.get('half_reached') is True
    half_near=half_met or bool(half is not None and half>0 and price is not None and price<=half*1.15)
    near_low=dist is not None and 0<=dist<=25
    active=eligible_available and not recent_move and half_near and near_low
    if not active and not previous:return None
    support=number(s.get('effective_low')) or support
    fresh=all(usable_reading(s.get(key) or {},now) for key in ('quote_freshness','borrow_freshness'))
    new_low=bool(s.get('new_low_today'))
    try:anchor=date.fromisoformat(str(s.get('post_split_low_date'))[:10])
    except ValueError:anchor=None
    # A live lower low is captured immediately. Wait for daily history to
    # confirm completed sessions; polling itself never adds a session.
    if fresh and new_low:
        sessions=0
        stamp=q.get('market_timestamp') or s.get('last_market_day')
        try:anchor=date.fromisoformat(str(stamp)[:10])
        except ValueError:anchor=today
        if previous.get('support')==support and previous.get('sessions')==0 and previous.get('low_date'):
            anchor=date.fromisoformat(previous['low_date'])
    if previous.get('support') is not None and support is not None and support>previous['support'] and previous.get('low_date'):
        support=previous['support'];anchor=date.fromisoformat(previous['low_date']);sessions=0
    sessions=max(0,min(4,sessions))
    valid_low=bool(s.get('history_verified') and support is not None and support>0 and anchor and anchor<=today)
    missing=[]
    if not valid_low:missing.append('تحديد القاع — بيانات ناقصة')
    if not eligible_available:missing.append('Available أقل من 20,000')
    if recent_move:missing.append('انتهاء 10 جلسات منذ قمة حركة +60% أو أكثر')
    if not half_met:missing.append('لمس نصف القمة — قريب خلال 15%' if half_near else 'القرب من نصف القمة خلال 15%')
    if not near_low:missing.append('البعد عن القاع 25% أو أقل')
    if sessions<4:missing.append('الثبات')
    remaining=max(0,4-sessions)
    state='waiting';expected=None;early_date=None;early_state='waiting';data_issue=None
    waiting_label='بانتظار '+ '، '.join(missing)
    reason=waiting_label
    if not fresh:
        state='data_pending';reason='المتابعة مستمرة — بانتظار تحديث القراءة القديمة'
        stale=[name for key,name in (('quote_freshness','السعر'),('borrow_freshness','Available')) if not usable_reading(s.get(key) or {},now)]
        data_issue='بانتظار تحديث '+ ' و'.join(stale)
    elif active and valid_low:
        def milestone(n):
            d=advance(anchor,n)
            left=max(0,n-sessions)
            if left and d<=today:d=advance(date.fromisoformat(review_date(now)),left-1)
            return d.isoformat()
        expected=milestone(4);early_date=milestone(2)
        state='ready' if sessions>=4 and half_met else 'scheduled' if sessions<4 else 'waiting'
        if state=='waiting':expected=None
        early_state='ready' if sessions>=2 else 'scheduled'
        reason='اكتملت شروط هذه القائمة والثبات 4/4' if state=='ready' else 'إذا حافظ على القاع وبقي Available أقل من 20,000'+(' ولمس نصف القمة' if not half_met else '')
    stage='قاع جديد' if sessions==0 else 'تأكيد القاع' if sessions<2 else 'قاع ثابت 2/4' if sessions<4 else 'اكتمل الثبات 4/4'
    next_goal='تأكيد أول جلسة فوق القاع' if sessions==0 else 'اكتمال ثبات القاع 2/4' if sessions<2 else 'اكتمال ثبات القاع 4/4' if sessions<4 else 'استمرار المحافظة على القاع'
    plan=[{'label':f'المحافظة على القاع ${support:.4f}' if support is not None else 'تحديد القاع','met':valid_low},
          {'label':'Available أقل من 20,000','met':eligible_available},
          {'label':'لم يصعد 60% أو أكثر خلال آخر 10 جلسات','met':not recent_move},
          {'label':'نصف القمة متحقق أو قريب خلال 15%','met':half_near},
          {'label':'البعد عن القاع 25% أو أقل','met':near_low},
          {'label':'ثبات القاع 2/4','met':sessions>=2},
          {'label':'اكتمال الثبات 4/4','met':sessions>=4}]
    invalidators=['ابتعاد السعر أكثر من 25% عن القاع أو أكثر من 15% عن نصف القمة','صعود 60% أو أكثر يخفي السهم حتى انتهاء 10 جلسات من القمة','قاع أقل يعيد الثبات من الصفر','Available عند 20,000 أو أكثر يخفي السهم من القائمة ويحفظ سجله','غياب قراءة حديثة يوقف حساب الموعد حتى التحديث']
    events=list(previous.get('history') or [])
    changed=False
    if previous and (previous.get('expected_date')!=expected or previous.get('state')!=state or previous.get('support')!=support or ('retest_status' in previous and previous.get('retest_status')!=s.get('support_retest_status'))):
        changed=True
        change_reason='قاع جديد — أُعيد حساب الثبات' if previous.get('support') is not None and support is not None and support<previous['support'] else reason
        kind='support_broken' if previous.get('support') is not None and support is not None and support<previous['support'] else 'completed' if state=='ready' and previous.get('state')!='ready' else 'support_broken' if s.get('support_retest_status')=='failed' and previous.get('retest_status')!='failed' else 'advanced' if previous.get('expected_date') and expected and expected<previous['expected_date'] else 'delayed' if previous.get('expected_date') and expected and expected>previous['expected_date'] else 'suspended' if previous.get('expected_date') and not expected else 'changed'
        events.append({'at':now.isoformat(),'kind':kind,'old_date':previous.get('expected_date'),'new_date':expected,'reason':change_reason})
    if previous and 'early_state' in previous and early_state=='ready' and previous.get('early_state')!='ready':
        events.append({'at':now.isoformat(),'kind':'early_ready','old_date':previous.get('early_date'),'new_date':early_date,'reason':'ثبت القاع جلستين 2/4'})
    last_change=events[-1] if events else None
    change_kind=None
    if last_change:
        old,new=last_change.get('old_date'),last_change.get('new_date')
        change_kind='تأجل' if old and new and new>old else 'تقدّم' if old and new and new<old else 'عُلّق الموعد' if old and not new else 'حُدد الموعد' if new and not old else 'تغيّرت الحالة'
    list_ready,list_missing=ready_list_status(row)
    last_ready=previous.get('ready_current')
    ready_changes=list(previous.get('ready_changes') or [])
    entered_at=previous.get('ready_entered_at');exited_at=previous.get('ready_exited_at')
    if fresh and (last_ready is None or list_ready!=last_ready):
        if list_ready:entered_at=now.isoformat()
        elif last_ready is True:exited_at=now.isoformat()
        if list_ready or last_ready is True:
            ready_changes.append({'at':now.isoformat(),'kind':'entered' if list_ready else 'exited','reason':'دخل الأجهز' if list_ready else 'خرج من الأجهز: '+ '، '.join(list_missing)})
    current_ready=list_ready if fresh else last_ready
    completions=list(previous.get('completions') or [])
    completed_at=previous.get('completed_at') if previous.get('completed_low_date')==(anchor.isoformat() if anchor else None) else None
    if state=='ready' and not completed_at:
        completed_at=now.isoformat()
        completions.append({'symbol':row['symbol'],'company':row.get('company_name'),'price':q.get('price'),'available':av,'support':support,'sessions':sessions,'low_date':anchor.isoformat(),'completed_at':completed_at,'expected_date':expected})
    return {'ready_current':current_ready,'ready_observed_at':now.isoformat() if fresh else previous.get('ready_observed_at'),'ready_data_current':fresh,'ready_missing':list_missing,'ready_entered_at':entered_at,'ready_exited_at':exited_at,'ready_changes':ready_changes[-20:],'completions':completions[-50:],'completed_at':completed_at,'completed_low_date':anchor.isoformat() if completed_at else None,'half_near':half_near,'near_low':near_low,'half_met':half_met,'recent_move':recent_move,'recent_gain_pct':gain,'sessions_since_peak':since,'active':active,'low_date':anchor.isoformat() if anchor else None,'stage':stage,'next_goal':next_goal,'early_date':early_date,'early_state':early_state,'retest_status':s.get('support_retest_status'),'plan':plan,'invalidators':invalidators,'retest_zone':{'low':support,'high':round(support*1.05,6)} if support is not None and support>0 else None,'borrow_read_at':(s.get('borrow_freshness') or {}).get('timestamp'),'using_last_session':fresh and any((s.get(k) or {}).get('status')!='fresh' for k in ('quote_freshness','borrow_freshness')),'next_review_date':review_date(now),'support_distance_pct':dist,'waiting_label':waiting_label,'data_issue':data_issue,'last_change':last_change,'change_kind':change_kind,'symbol':row['symbol'],'company':row.get('company_name'),'price':q.get('price'),'available':av,'rsi':rsi,'support':support,'sessions':sessions,'target_sessions':target,'remaining_sessions':remaining,'expected_date':expected,'state':state,'reason':reason,'missing':missing,'history':events[-20:],'rescheduled':changed or bool(previous.get('rescheduled')),'added_at':previous.get('added_at') or now.isoformat(),'reviewed_at':now.isoformat(),'four_session_date':advance(date.fromisoformat(expected),max(0,4-max(target,sessions))).isoformat() if expected else None}

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
        if not row.get('active',False):continue
        for event in row.get('history') or []:
            try:
                if datetime.fromisoformat(event['at']).astimezone(ZoneInfo('Asia/Riyadh')).date()!=local_day:continue
            except (KeyError,ValueError):continue
            result.append({**event,'symbol':row['symbol']})
    result.sort(key=lambda x:x['at'],reverse=True)
    counts={k:len({e['symbol'] for e in result if e.get('kind')==k}) for k in ('completed','early_ready','advanced','delayed','support_broken','suspended','changed')}
    return {'date':local_day.isoformat(),'counts':counts,'events':result[:50]}

def payload():
    upcoming=[r for r in ROWS.values() if r.get('active',False) and r.get('state')=='scheduled' and r.get('expected_date') and number(r.get('available')) is not None and 0<=r['available']<20000]
    completed=[]
    for r in ROWS.values():
        for c in r.get('completions') or []:
            current=number(r.get('price'));start=number(c.get('price'));low=number(c.get('support'))
            latest_low=number(r.get('support'))
            held=None if not r.get('ready_data_current') or latest_low is None or low is None else latest_low>=low and (current is None or current>=low)
            completed.append({**c,**{k:r.get(k) for k in ('ready_current','ready_data_current','ready_observed_at','ready_missing','ready_entered_at','ready_exited_at','ready_changes')},'current_price':current,'current_available':r.get('available'),'held_low':held,'change_since_completion_pct':round((current/start-1)*100,2) if current is not None and start and start>0 else None})
    return {**META,'daily_changes':daily_changes(),'rows':sorted(upcoming,key=lambda x:(x['expected_date'],x['remaining_sessions'],x['available'])),'completed':sorted(completed,key=lambda x:(0 if x.get('ready_current') is True else 1 if len(x.get('ready_missing') or [])==1 else 2, -(datetime.fromisoformat(x.get('ready_entered_at') or x['completed_at']).timestamp()))),'target_sessions':4,'next_session_date':advance(datetime.now(NY).date(),1).isoformat()}
