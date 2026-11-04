"""Read-only evaluation for trend_v2.0. No Bark config, notifications, or orders."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from market import fetch_index_candles
from strategy import Candle, MINUTE, backtest, validate
from trend_strategy import trend_features, WARMUP_MINUTES, VERSION
from replay import exact_replay


def evaluate(candles, payout=.8, ticks=None):
    validate(candles)
    if len(candles)<183: raise ValueError('评估至少需要183分钟连续指数历史')
    features=trend_features(candles)
    split=max(WARMUP_MINUTES+1,len(candles)*2//3)
    if split >= len(candles): raise ValueError('评估至少需要183分钟连续指数历史')
    segments={}
    for name,start,stop in [('early',WARMUP_MINUTES,split),('holdout',split,len(candles))]:
        stats=backtest(candles,features,start,stop,payout)
        daily={};busy_until=-1
        for i in range(start,stop):
            stamp=candles[i].time+MINUTE
            day=datetime.fromtimestamp(stamp/1000,timezone.utc).date().isoformat()
            row=daily.setdefault(day,dict(evaluated_minutes=0,qualified_minutes=0,opportunities=0))
            row['evaluated_minutes']+=1
            if features[i]['qualified']:
                row['qualified_minutes']+=1
                if stamp>=busy_until:
                    row['opportunities']+=1;busy_until=stamp+601000
        for row in daily.values(): row['partial_day']=row['evaluated_minutes']!=1440
        segments[name]=dict(bounds=stats,daily_utc=daily)
    result=dict(strategy_version=VERSION,payout_assumption=payout,exact=False,
                production_push_backtest=False,
                note='规则预先固定，前2/3描述、后1/3验证；分钟范围只给盈亏界限。机会不是实际推送；未重演请求失败、实时价格偏离、通知及人工延迟。部分日不外推频次。未调参，未证实盈利优势。',
                **segments)
    if ticks is not None:
        result['exact_candidate_replay']=exact_replay(candles,ticks,features=features,payout=payout)
        result['exact_candidate_replay']['strategy_version']=VERSION
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30)
    p.add_argument('--input',help='time_ms,open,high,low,close[,spot_volume]；必须为指数OHLC')
    p.add_argument('--ticks',help='可选精确指数端点CSV：time_ms,index_price；缺失不补价')
    p.add_argument('--payout',type=float,default=.8)
    p.add_argument('--output',default='work/trend-evaluation.json')
    args=p.parse_args()
    if not 3<=args.days<=90 or not .01<=args.payout<=2:p.error('days须3–90，payout须.01–2')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    if args.input:
        source=Path(args.input)
        with source.open(encoding='utf-8',newline='') as stream:
            candles=[Candle(int(r['time_ms']),*[float(r[k]) for k in ('open','high','low','close')],float(r.get('spot_volume',0))) for r in csv.DictReader(stream)]
    else:
        print('只读获取指数数据，不发送通知、不下单。首次30天可能需要几分钟。',flush=True)
        candles,_=fetch_index_candles(count=args.days*1440,include_spot_volume=False)
        source=output.with_suffix('.csv')
        with source.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['time_ms','open','high','low','close','spot_volume'])
            writer.writerows((c.time,c.open,c.high,c.low,c.close,c.volume) for c in candles)
    ticks=None
    if args.ticks:
        ticks={};previous=-1
        with open(args.ticks,encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                stamp=int(row['time_ms'])
                if stamp<=previous:raise ValueError('指数时间必须严格递增')
                ticks[stamp]=row['index_price'];previous=stamp
    result=evaluate(candles,args.payout,ticks)
    result['inputs_sha256']={'candles':hashlib.sha256(source.read_bytes()).hexdigest()}
    if args.ticks:result['inputs_sha256']['ticks']=hashlib.sha256(Path(args.ticks).read_bytes()).hexdigest()
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    b=result['holdout']['bounds']
    print('后段分钟范围研究：样本',b['total'],'确定胜',b['wins'],'确定亏',b['losses'],'未知',b['unresolved'])
    print('盈亏界限',b['profit_units'],b['profit_upper_units'],'；不是实际交易胜率。报告：',output.resolve())


if __name__=='__main__':main()
