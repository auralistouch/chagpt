"""Frozen causal OHLC feature rows, with diagnostics for the local interface."""
from collections import deque
FEATURE_NAMES=tuple('momentum%s_atr'%x for x in (1,3,5,10,20,60))+('ema20_50_atr','ema50_120_atr','price_ema20_atr','rsi14_signed','efficiency20','efficiency60','atr5_30','body_atr','close_location_signed','range_atr','position20','position60')
WARMUP=120


def feature_rows(candles):
    out=[];closes=[];trs=deque(maxlen=30);gains=deque(maxlen=14);losses=deque(maxlen=14);e20=e50=e120=None
    for i,c in enumerate(candles):
        prev=candles[i-1].close if i else c.close
        change=c.close-prev;closes.append(c.close)
        trs.append(max(c.high-c.low,abs(c.high-prev),abs(c.low-prev)))
        gains.append(max(0.,change));losses.append(max(0.,-change))
        e20=c.close if e20 is None else e20+2/21*(c.close-e20)
        e50=c.close if e50 is None else e50+2/51*(c.close-e50)
        e120=c.close if e120 is None else e120+2/121*(c.close-e120)
        tr=list(trs);a14=sum(tr[-14:])/min(14,len(tr));a5=sum(tr[-5:])/min(5,len(tr));a30=sum(tr)/len(tr)
        gain,loss=sum(gains),sum(losses);rsi=100*gain/(gain+loss) if gain+loss else 50.
        location=(c.close-c.low)/(c.high-c.low) if c.high>c.low else .5
        row=dict(values=None,atr=a14,atr_pct=100*a14/c.close,rsi=rsi,
                 efficiency_20=None,close_location=location,ema20=e20,ema50=e50)
        if i<WARMUP or a14<=0 or a30<=0:
            out.append(row);continue
        efficiencies=[];positions=[]
        for n in (20,60):
            path=sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1,i+1))
            efficiencies.append(abs(closes[i]-closes[i-n])/path if path else 0.)
            low=min(x.low for x in candles[i-n+1:i+1]);high=max(x.high for x in candles[i-n+1:i+1])
            positions.append(2*(c.close-low)/(high-low)-1 if high>low else 0.)
        values=[(c.close-closes[i-n])/a14 for n in(1,3,5,10,20,60)]
        values += [(e20-e50)/a14,(e50-e120)/a14,(c.close-e20)/a14,(rsi-50)/50,*efficiencies,a5/a30,(c.close-c.open)/a14,2*location-1,(c.high-c.low)/a14,*positions]
        row.update(values=[max(-12.,min(12.,v)) for v in values],efficiency_20=efficiencies[0])
        out.append(row)
    return out


def raw_features(candles):
    return [row['values'] for row in feature_rows(candles)]
