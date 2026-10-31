"""Read-only market audit. No Bark configuration is read and no orders are sent."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from market import fetch_index_candles
from strategy import Candle, MINUTE, WARMUP, RULES, backtest, indicators, validate


def audit(candles):
    validate(candles)
    features = indicators(candles)
    split = min(len(candles)-1, max(WARMUP+11, len(candles)*2//3))
    segments = {}
    for name, start, stop in (('early', WARMUP, split), ('holdout', split, len(candles))):
        stats = backtest(candles, features, start, stop, .8)
        daily = {}
        for i in range(start, stop):
            submitted = candles[i].time+MINUTE
            day = datetime.fromtimestamp(submitted/1000, timezone.utc).date().isoformat()
            row = daily.setdefault(day, dict(evaluated_minutes=0, candidate_minutes=0))
            row['evaluated_minutes'] += 1
            row['candidate_minutes'] += features[i]['qualified']
        segments[name] = dict(bounds=stats, daily_utc=daily,
                              failed_rule_counts={code:sum(code in f['failed_rules'] for f in features[start:stop]) for code in RULES})
    return dict(payout=.8, horizon_seconds=600, timing_rule='next_second_open_then_600s',
                exact=False, production_push_backtest=False,
                note='指数分钟区间验证；候选分钟未经生产滚动统计门槛及去重，不是买卖提醒次数。不能据此声称胜率提高或保证每天几次。规则和门槛未调参。',
                **segments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30, help='只读下载3–90天指数及现货量能，默认30天')
    parser.add_argument('--input', help='已有指数OHLC+现货量能CSV；提供时不联网')
    parser.add_argument('--output', default='work/index-audit.json')
    args = parser.parse_args()
    if not 3 <= args.days <= 90: parser.error('--days 必须为3–90')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.input:
        with open(args.input, newline='', encoding='utf-8') as stream:
            candles = [Candle(int(r['time_ms']), *[float(r[k]) for k in ('open','high','low','close','spot_volume')]) for r in csv.DictReader(stream)]
        source_file = Path(args.input)
    else:
        print('只读获取币安BTCUSDT指数及现货量能，不发送通知、不下单。', flush=True)
        candles, _ = fetch_index_candles(count=args.days*1440)
        source_file = output.with_suffix('.csv')
        with source_file.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(('time_ms','open','high','low','close','spot_volume'))
            writer.writerows((c.time,c.open,c.high,c.low,c.close,c.volume) for c in candles)
    result = audit(candles)
    result['input_sha256'] = hashlib.sha256(source_file.read_bytes()).hexdigest()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(f'报告已写入 {output}；分钟数据只给保守界限，非精确胜率。')


if __name__ == '__main__': main()
