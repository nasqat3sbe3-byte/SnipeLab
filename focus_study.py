"""Recent observed +100% episodes and comparison windows. No market I/O.

Features end before the first subsequent 40% expansion from the episode low.
The episode is a retrospective label; future prices never enter the feature
vector. Negative labels require ten subsequent completed observed sessions.
Similarity is not a forecast or a probability. Sparse windows fail closed.
"""
import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

FEATURES=('return3','return6','range3','range_ratio','low_trend','close_location','distance_low','reclaim')
MIN_SCALES=(.08,.12,.10,.20,.06,.20,.08,.08)


def clean_bars(bars, at):
    today=at.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    out=[]
    for b in bars or []:
        try:
            day=str(b['date']);datetime.fromisoformat(day)
            vals=[float(b[k]) for k in ('low','high','close')]
            if day>=today:continue
            if not all(math.isfinite(v) and v>0 for v in vals) or not vals[0]<=vals[2]<=vals[1]:return []
            if out and day<=out[-1]['date']:return []
            out.append({'date':day,**dict(zip(('low','high','close'),vals))})
        except (KeyError,ValueError,TypeError):return []
    return out[-120:]


def vector(bars):
    if len(bars)<6:return None
    b=bars[-6:];p,r=b[:3],b[3:];last=b[-1]
    floor=min(x['low'] for x in r)
    span=lambda a:math.log(max(x['high'] for x in a)/min(x['low'] for x in a))
    a,z=span(p),span(r)
    if a<=0 or last['high']<=last['low']:return None
    vals=(math.log(last['close']/b[2]['close']),math.log(last['close']/b[0]['close']),z,
          math.log(max(z,.001)/max(a,.001)),math.log(last['low']/b[3]['low']),
          (last['close']-last['low'])/(last['high']-last['low']),math.log(last['close']/floor),
          math.log(last['close']/max(x['high'] for x in b[-4:-1])))
    return [round(max(-3,min(3,x)),6) for x in vals]


def recent_episode(bars):
    """Earlier SESSION low to later high; no assumed intraday ordering."""
    best=None
    for j in range(max(1,len(bars)-10),len(bars)):
        for i in range(max(0,j-9),j):
            gain=bars[j]['high']/bars[i]['low']-1
            if gain<1-1e-9:continue
            if best is None or gain>best['gain']:
                best={'i':i,'j':j,'gain':gain}
    return best


def build(corpus,at):
    positives=[];negatives=[];winners=[];skipped=[]
    for symbol,raw in sorted(corpus.items()):
        bars=clean_bars(raw,at)
        if len(bars)<6:continue
        if (at.astimezone(ZoneInfo('America/New_York')).date()-datetime.fromisoformat(bars[-1]['date']).date()).days>5:continue
        e=recent_episode(bars)
        if e:
            i,j=e['i'],e['j'];floor=bars[i]['low']
            trigger=next((k for k in range(i+1,j+1) if bars[k]['high']>=floor*1.4),j)
            prefix=bars[:trigger];v=vector(prefix)
            record={'symbol':symbol,'low_date':bars[i]['date'],'peak_date':bars[j]['date'],
                    'gain_pct':round(e['gain']*100,2),'trigger_date':bars[trigger]['date']}
            winners.append(record)
            if v:
                positives.append({**record,'observed_at':prefix[-1]['date'],'vector':v,
                                  'from_observation_pct':round((bars[j]['high']/prefix[-1]['close']-1)*100,2)})
            else:skipped.append({'symbol':symbol,'reason':'less_than_six_pre_trigger_sessions'})
        # Comparison windows are sampled deterministically and never use an
        # uncompleted forward horizon. One window per five sessions per symbol.
        comparisons=[]
        for end in range(max(5,len(bars)-35),len(bars)-10,5):
            prefix=bars[:end+1];future=bars[end+1:end+11];v=vector(prefix)
            if v is None or len(future)!=10:continue
            gains=[future[j]['high']/future[i]['low']-1 for j in range(1,10) for i in range(j)]
            close_gain=max(x['high'] for x in future)/prefix[-1]['close']-1
            if max(gains+[close_gain])>=1-1e-9:continue
            comparisons.append({'symbol':symbol,'observed_at':prefix[-1]['date'],'resolved_at':future[-1]['date'],
                              'vector':v,'max_rise_pct':round(close_gain*100,2)})
        negatives.extend(comparisons[-2:])
    all_rows=positives+negatives
    scales=[]
    for i,floor in enumerate(MIN_SCALES):
        values=[r['vector'][i] for r in all_rows]
        med=statistics.median(values) if values else 0
        mad=statistics.median(abs(v-med) for v in values) if values else 0
        scales.append(max(floor,mad*1.4826))
    model={'version':1,'generated_at':at.isoformat(),'positives':positives,'negatives':negatives,
           'scales':scales,'winners':winners,'skipped':skipped,'features':list(FEATURES),
           'status':'ready' if len(positives)>=5 and len({x['symbol'] for x in negatives})>=5 else 'insufficient',
           'definition':'قاع جلسة إلى قمة جلسة لاحقة خلال عشر جلسات، والقمة ضمن آخر عشر جلسات مكتملة',
           'limitations':['البيانات اليومية لا تثبت ترتيب الحركة داخل اليوم نفسه','الحجم وAvailable التاريخيان لا يدخلان التشابه إذا لم تتوفر بياناتهما','التشابه ليس نسبة نجاح أو ضمان صعود']}
    model['validation']=validate(model)
    return model


def distance(a,b,scales):
    return math.sqrt(sum(((x-y)/s)**2 for x,y,s in zip(a,b,scales))/len(scales))


def nearest(model,v,symbol=None):
    # Never match a stock to its own past episode or comparison window.
    pos=[(distance(v,r['vector'],model['scales']),r) for r in model['positives'] if r['symbol']!=symbol]
    neg=[(distance(v,r['vector'],model['scales']),r) for r in model['negatives'] if r['symbol']!=symbol]
    if not pos or not neg:return None
    pos.sort(key=lambda x:(x[0],x[1]['symbol']));neg.sort(key=lambda x:(x[0],x[1]['symbol']))
    pd,p=pos[0];nd,n=neg[0]
    # A monitoring match must be near a positive and closer to it than to a
    # known comparison. This threshold is a transparent experimental guard.
    return {'distance':round(pd,3),'comparison_distance':round(nd,3),'matched':pd<=1.25 and pd+.10<nd,
            'closest':[{k:r.get(k) for k in ('symbol','observed_at','low_date','peak_date','gain_pct','from_observation_pct')} for _,r in pos[:3]],
            'comparison_symbol':n['symbol']}


def validate(model):
    # Leave-symbol-out diagnostic, NOT an independent temporal performance test.
    results=[];folds={}
    for label,key in [(True,'positives'),(False,'negatives')]:
        for r in model[key]:
            sym=r['symbol']
            if sym not in folds:
                fold={**model,'positives':[x for x in model['positives'] if x['symbol']!=sym],
                      'negatives':[x for x in model['negatives'] if x['symbol']!=sym]}
                rows=fold['positives']+fold['negatives'];scales=[]
                for i,floor in enumerate(MIN_SCALES):
                    values=[x['vector'][i] for x in rows];med=statistics.median(values) if values else 0
                    mad=statistics.median(abs(v-med) for v in values) if values else 0
                    scales.append(max(floor,mad*1.4826))
                fold['scales']=scales;folds[sym]=fold
            hit=nearest(folds[sym],r['vector'],sym)
            if hit is not None:results.append((label,hit['matched']))
    tp=sum(a and b for a,b in results);fp=sum(not a and b for a,b in results)
    return {'method':'leave_symbol_out_diagnostic','samples':len(results),'positive_samples':sum(a for a,b in results),
            'matched_positives':tp,'matched_comparisons':fp,'predictive_performance_established':False}


def match(model,bars,symbol,price,at):
    clean=clean_bars(bars,at);v=vector(clean)
    if model.get('status')!='ready' or v is None:return None
    if (at.astimezone(ZoneInfo('America/New_York')).date()-datetime.fromisoformat(clean[-1]['date']).date()).days>5:return None
    result=nearest(model,v,symbol)
    if result is None:return None
    floor=min(b['low'] for b in clean[-3:]);level=max(b['high'] for b in clean[-3:])
    # No chasing a move already far above the state whose behavior was matched.
    if price<floor or price>floor*1.25:result['matched']=False
    result.update(as_of=clean[-1]['date'],level=round(level,4),floor=floor,features=dict(zip(FEATURES,v)))
    return result
