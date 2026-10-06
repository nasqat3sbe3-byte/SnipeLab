"""Wilder RSI and guarded repairs for verified Yahoo split-scale defects.

Repairs affect RSI input only, never displayed prices or historical extrema.
MGN: Nasdaq ECA2026-635 cancelled the September 8 1:40 action;
ECA2026-661 confirms September 17 1:30. Yahoo retains the cancelled factor.
UCAR: Nasdaq ECA2026-647 confirms September 9 1:20. Yahoo's daily
series contains mostly unadjusted earlier bars and isolated adjusted bars.
"""
import math
from statistics import median


def normalized_closes(symbol, observations):
    rows=sorted((str(day),float(value)) for day,value in observations
                if value is not None and math.isfinite(float(value)) and float(value)>0)
    repairs=[]
    if symbol=='UCAR':
        boundary='2026-09-09'
        before=[v for d,v in rows if d<boundary]
        after=[v for d,v in rows if d>=boundary]
        # Already adjusted data must pass through unchanged. Require a large
        # unit discontinuity across the known split, not just its event record.
        if len(before)>=5 and after and 5<median(after[:3])/median(before[-5:])<30:
            original=list(rows)
            out=[]
            for i,(day,value) in enumerate(original):
                if day<boundary:
                    factor=20.0
                    # Both adjacent sessions must corroborate an isolated bar
                    # already expressed in post-split units. No interpolation.
                    if 0<i<len(original)-1 and original[i+1][0]<boundary:
                        a,b=original[i-1][1],original[i+1][1]
                        if 12<value/a<30 and 12<value/b<30:
                            factor=1.0
                    value*=factor
                out.append((day,value))
            rows=out
            repairs.append('UCAR_yahoo_mixed_2026_09_09_split_units')
    elif symbol=='MGN':
        boundary='2026-09-08'
        before=[v for d,v in rows if d<boundary]
        after=[v for d,v in rows if d>=boundary]
        # The cancelled 1:40 action left a 40x discontinuity. Check its exact
        # neighborhood so a later vendor repair does not get adjusted twice.
        if before and after and 32<before[-1]/after[0]<48:
            rows=[(day,value/40 if day<boundary else value) for day,value in rows]
            repairs.append('MGN_cancelled_2026_09_08_1_for_40')
    return rows,repairs


def wilder_rsi(closes, period=14):
    if len(closes)<period+1:return None
    changes=[float(b)-float(a) for a,b in zip(closes,closes[1:])]
    gain=sum(max(v,0) for v in changes[:period])/period
    loss=sum(max(-v,0) for v in changes[:period])/period
    for value in changes[period:]:
        gain=(gain*(period-1)+max(value,0))/period
        loss=(loss*(period-1)+max(-value,0))/period
    return 100.0 if loss==0 else 100.0-100.0/(1.0+gain/loss)
