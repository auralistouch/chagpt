"""Read-only current-strategy evaluation: minute-open reference points, never actual orders."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import io
import sys
from pathlib import Path
from market import MarketError
from history_download import download_index_history, atomic_text
from strategy import Candle, MINUTE, validate
from point_research import point_backtest
from replay import exact_replay
from frequency_research import study_frequency


def evaluate(candles, payout=.8, ticks=None):
    validate(candles)
    if len(candles)<183:raise ValueError('评估至少需要183分钟连续指数历史')
    from latest_strategy import latest_features, VERSION, VALIDATION_START_MS, validation_from_rows
    features=latest_features(candles)
    split=max(61,len(candles)*2//3)
    segments={}
    for name,start,stop in [('early',60,split),('holdout',split,len(candles))]:
        points=point_backtest(candles,features,start,stop,payout,keep_all=True)
        frequency=study_frequency(candles,points,start,stop)
        # Frequency uses all rows; keep the visible result payload bounded.
        points['trades']=points['trades'][-30:]
        points['unverified_trades']=points['unverified_trades'][-30:]
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
        segments[name]=dict(reference=points,daily_utc=daily,frequency_research=frequency)
    from bisect import bisect_left
    fresh_start=bisect_left([c.time+MINUTE for c in candles],VALIDATION_START_MS)
    if fresh_start<len(candles):
        fresh=point_backtest(candles,features,fresh_start,len(candles),payout,keep_all=True)
        future_rows=fresh['trades'];pending=fresh['unverified']
    else:future_rows=[];pending=0
    validation=validation_from_rows(future_rows,None,payout,pending)
    result=dict(strategy_version=VERSION,payout_assumption=payout,exact_contract_replay=False,
                actual_order_results=False,production_push_backtest=False,research_validation=validation,
                research_mode=True,frequency_target=dict(window_hours=12,target=20,approximate=True,guaranteed=False),
                prior_data_exposure=True,independent_validation=False,
                note='只评估当前研究策略；65%是累计获胜比例目标，已看过数据不能独立证明它。用户已选择未达到65%也接收研究方向；约20次/12h不是每窗保证。分钟参考不是实际订单。',**segments)
    if ticks is not None:
        result['exact_candidate_replay']=exact_replay(candles,ticks,features=features,payout=payout)
        result['exact_candidate_replay']['strategy_version']=VERSION
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30);p.add_argument('--input')
    p.add_argument('--refresh',action='store_true',help='创建新的下载时间窗口，原断点缓存保留')
    p.add_argument('--cache-dir',help='默认在报告同目录index-history-cache保存断点')
    p.add_argument('--ticks',help='可选精确指数端点CSV：time_ms,index_price；缺失不补价')
    p.add_argument('--payout',type=float,default=.8)
    p.add_argument('--output',default='work/pullback-evaluation.json')
    args=p.parse_args()
    if not 3<=args.days<=90 or not .01<=args.payout<=2:p.error('days须3–90，payout须.01–2')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    download_metadata=None
    print('评估下载工具 v5.0：每个请求最多尝试4次，完整页面自动缓存；不下单、不推送。',flush=True)
    if args.input:
        source=Path(args.input)
        with source.open(encoding='utf-8',newline='') as stream:
            candles=[Candle(int(r['time_ms']),*[float(r[k]) for k in ('open','high','low','close')],float(r.get('spot_volume',0))) for r in csv.DictReader(stream)]
    else:
        print('只读获取指数；不发通知、不下单。首次30天可能需要几分钟。',flush=True)
        cache_dir=Path(args.cache_dir) if args.cache_dir else output.parent/'index-history-cache'
        candles,download_metadata=download_index_history(args.days*1440,cache_dir,refresh=args.refresh,progress=lambda text:print(text,flush=True))
        source=output.with_suffix('.csv')
        stream=io.StringIO(newline='')
        writer=csv.writer(stream);writer.writerow(['time_ms','open','high','low','close','spot_volume'])
        writer.writerows((c.time,c.open,c.high,c.low,c.close,c.volume) for c in candles)
        atomic_text(source,stream.getvalue())
    ticks=None
    if args.ticks:
        ticks={};previous=-1
        with open(args.ticks,encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                stamp=int(row['time_ms'])
                if stamp<=previous:raise ValueError('指数时间必须严格递增')
                ticks[stamp]=row['index_price'];previous=stamp
    result=evaluate(candles,args.payout,ticks)
    result['generated_at_utc']=datetime.now(timezone.utc).isoformat()
    result['download_window']=download_metadata
    result['inputs_sha256']={'candles':hashlib.sha256(source.read_bytes()).hexdigest()}
    if args.ticks:result['inputs_sha256']['ticks']=hashlib.sha256(Path(args.ticks).read_bytes()).hexdigest()
    atomic_text(output,json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    stats=result['holdout']['reference']
    print(result['strategy_version'],'后段研究样本',stats['total'],'研究胜/亏/平',stats['wins'],stats['losses'],stats['ties'],'未验证',stats['unverified'],'研究命中率',stats['win_rate_pct'])
    print('65%为研究目标；冻结规则后的新样本：',result['research_validation']['reason'])
    frequency=result['holdout']['frequency_research']
    print('后段完整滚动12h候选次数：最少',frequency['minimum'],'平均',frequency['average'],'最多',frequency['maximum'],'；不是实际推送次数或未来保证')
    print('不是实际订单胜率。报告：',output.resolve())


if __name__=='__main__':
    try:main()
    except (MarketError,ValueError,OSError) as error:
        print('\n本次评估未完成：'+str(error),file=sys.stderr,flush=True)
        print('本次未生成新报告。完整下载页已保留；网络恢复后双击evaluate.command会接着下载。若原报告存在，它不是本次结果。',file=sys.stderr,flush=True)
        sys.exit(1)
