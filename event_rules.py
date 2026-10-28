"""Pure transition rules for SnipeLab event center."""
def borrow_events(symbol, previous, current):
    """Only important threshold crossings between two confirmed readings."""
    if not previous or not current:
        return []
    old=previous.get("available")
    new=current.get("available")
    if old is None or new is None or old == new:
        return []
    result=[]
    if old>0 and new==0:
        result.append((symbol,"available_zero",
                       "أصبح الشورت المتاح 0",{"old":old,"new":new}))
    elif old>10000 and 0<new<=10000:
        result.append((symbol,"available_10k",
                       "دخل الشورت المتاح 10K وأقل",{"old":old,"new":new}))
    return result

def ready_event(symbol,previous,current,price,available):
    if current and not previous:
        return (symbol,"ready","Entered ready list",{"price":price,"available":available})
    return None

def worker_health(now, last, max_age_seconds):
    if not last:return {"state":"waiting","age_seconds":None}
    try:
        from datetime import datetime
        age=max(0,(now-datetime.fromisoformat(last)).total_seconds())
        return {"state":"active" if age<=max_age_seconds else "stale","age_seconds":round(age,1)}
    except (ValueError,TypeError):
        return {"state":"invalid","age_seconds":None}


def strip_items(events, strict_alerts, calendar, now):
    """One entry per stock, confirmed transitions only, bounded lifetime."""
    from datetime import datetime
    import re
    labels={'available_zero':'المتاح صفر','ready':'دخل الأجهز',
            'strict_rsi':'RSI تحت 30 · اكتملت الشروط'}
    stocks={}
    for event in sorted([*events,*strict_alerts],key=lambda e:e.get('at',''),reverse=True):
        kind=event.get('kind');symbol=event.get('symbol')
        if kind not in labels or not symbol:continue
        try:age=(now-datetime.fromisoformat(event['at'].replace('Z','+00:00'))).total_seconds()
        except (KeyError,ValueError,TypeError):continue
        if not 0<=age<86400:continue
        item=stocks.setdefault(symbol,{'id':symbol+':'+event['at'],'symbol':symbol,'at':event['at'],'parts':[]})
        if labels[kind] not in item['parts']:item['parts'].append(labels[kind])
    result=[{'id':x['id'],'symbol':x['symbol'],'at':x['at'],
             'text':x['symbol']+' · '+' · '.join(x['parts'])} for x in stocks.values()]
    macro=[]
    for event in calendar.get('events',[]) if calendar.get('status')=='ok' else []:
        if not re.search(r'FOMC|Federal Funds|CPI|PCE|Non-Farm|Unemployment Rate|GDP',event['title'],re.I):continue
        try:seconds=(datetime.fromisoformat(event['time'])-now).total_seconds()
        except (ValueError,TypeError):continue
        if seconds>=-3600:macro.append({'id':event['title']+event['time'],'title':event['title'],'time':event['time']})
    # Reserve one place for the nearest important macro event.
    return result[:2]+macro[:1] if macro else result[:3]
