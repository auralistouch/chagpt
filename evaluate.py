"""Read-only v3 evaluation: minute-open reference points, never actual orders."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from market import fetch_index_candles
from strategy import Candle, MINUTE, validate
from pullback_strategy import pullback_features, VERSION
from trend_strategy import trend_features, WARMUP_MINUTES
from point_research import point_backtest
from replay import exact_replay


def evaluate(candles, payout=.8, ticks=None):
    validate(candles)
    if len(candles)<183:raise ValueError('评估至少需要183分钟连续指数历史')
    split=max(WARMUP_MINUTES+1,len(candles)*2//3)
    variants={}
    for version, features in [('pullback_v3.0',pullback_features(candles)),('trend_v2.0',trend_features(candles))]:
        segments={}
        for name,start,stop in [('early',WARMUP_MINUTES,split),('holdout',split,len(candles))]:
            points=point_backtest(candles,features,start,stop,payout)
            daily={};busy_until=-1
            for i in range(start,stop):
                stamp=candles[i].time+MINUTE
                day=datetime.fromtimestamp(stamp/1000,timezone.utc).date().isoformat()
                row=daily.setdefault(day,dict(evaluated_minutes=0,qualified_minutes=0,opportunities=0))
                row['evaluated_minutes']+=1
                if features[i]['qualified']:
                    row['qualified_minutes']+=1
                    if stamp>=busy_until:row['opportunities']+=1;busy_until=stamp+601000
            for row in daily.values():row['partial_day']=row['evaluated_minutes']!=1440
            segments[name]=dict(reference=points,daily_utc=daily)
        variants[version]=segments
    result=dict(strategy_version=VERSION,payout_assumption=payout,exact_contract_replay=False,
                actual_order_results=False,production_push_backtest=False,
                note='相同时间切分比较v2/v3固定规则；延后1分钟以指数分钟open为研究入场，再600秒用另一分钟open。并非任意秒位合约结算或实际推送结果。没有用此报告自动选参，未证明v3提高成功率。',
                variants=variants,**variants[VERSION])
    if ticks is not None:
        result['exact_candidate_replay']=exact_replay(candles,ticks,features=pullback_features(candles),payout=payout)
        result['exact_candidate_replay']['strategy_version']=VERSION
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30);p.add_argument('--input')
    p.add_argument('--ticks',help='可选精确指数端点CSV：time_ms,index_price；缺失不补价')
    p.add_argument('--payout',type=float,default=.8)
    p.add_argument('--output',default='work/pullback-evaluation.json')
    args=p.parse_args()
    if not 3<=args.days<=90 or not .01<=args.payout<=2:p.error('days须3–90，payout须.01–2')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    if args.input:
        source=Path(args.input)
        with source.open(encoding='utf-8',newline='') as stream:
            candles=[Candle(int(r['time_ms']),*[float(r[k]) for k in ('open','high','low','close')],float(r.get('spot_volume',0))) for r in csv.DictReader(stream)]
    else:
        print('只读获取指数；不发通知、不下单。首次30天可能需要几分钟。',flush=True)
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
    for version,variant in result['variants'].items():
        s=variant['holdout']['reference']
        print(version,'后段研究样本',s['total'],'研究胜/亏/平',s['wins'],s['losses'],s['ties'],'未验证',s['unverified'],'研究命中率',s['win_rate_pct'])
    print('不是实际订单胜率。报告：',output.resolve())


if __name__=='__main__':main()
