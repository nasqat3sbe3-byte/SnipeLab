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
