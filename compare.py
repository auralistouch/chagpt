"""Read-only, chronological comparison; never changes live thresholds or pushes."""
import argparse
import csv
import json
from pathlib import Path
from audit import audit
from market import fetch_index_candles
from strategy import MINUTE, WARMUP, indicators, backtest


def compare(candles, payout=.8):
    features = indicators(candles)
    split = len(candles)*2//3
    variants = {
        'original_five': features,
        # Research only: retain trend, volatility and non-chasing momentum;
        # require either RSI or volume confirmation. Not used for live alerts.
        'research_four': [{**f, 'qualified': i >= WARMUP and
            all(f['checks'][j]['passed'] for j in (0, 3, 4)) and
            any(f['checks'][j]['passed'] for j in (1, 2))}
            for i, f in enumerate(features)]}
    result = {}
    for name, values in variants.items():
        result[name] = {}
        for segment, start, stop in [('early', WARMUP, split), ('holdout', split, len(candles))]:
            stats = backtest(candles, values, start, stop, payout)
            daily = {}
            # Minute-boundary hypothetical submissions, 601s cooldown.
            # These are opportunities, not historical production pushes.
            busy_until = -1
            for i in range(start, stop):
                stamp = candles[i].time+MINUTE
                day = stamp//86400000
                row = daily.setdefault(str(day), dict(evaluated_minutes=0, qualified_minutes=0, cooldown_opportunities=0))
                row['evaluated_minutes'] += 1
                if values[i]['qualified']:
                    row['qualified_minutes'] += 1
                    if stamp >= busy_until:
                        row['cooldown_opportunities'] += 1
                        busy_until = stamp+601000
            result[name][segment] = dict(bounds=stats, daily_utc_epoch_day=daily)
    return dict(payout_assumption=payout, exact=False, production_push_backtest=False,
                note='两组固定条件使用相同时间切分；分钟范围只给上下界，未计真实手动下单延迟。四项版本仅研究，未自动上线。信号多不等于胜率高，不能仅按此报告宣称提升。',
                variants=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--input')
    parser.add_argument('--payout', type=float, default=.8)
    parser.add_argument('--output', default='work/comparison.json')
    args = parser.parse_args()
    if not 3 <= args.days <= 90 or not .01 <= args.payout <= 2: parser.error('days:3–90; payout:.01–2')
    if args.input:
        with open(args.input, encoding='utf-8', newline='') as stream:
            from strategy import Candle
            candles = [Candle(int(r['time_ms']), *[float(r[k]) for k in ('open','high','low','close','spot_volume')]) for r in csv.DictReader(stream)]
    else:
        print('只读获取指数及现货量能，不推送、不下单；首次30天可能需要几分钟。', flush=True)
        candles, _ = fetch_index_candles(count=args.days*1440)
    from strategy import validate
    validate(candles)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    result = compare(candles, args.payout)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    for name, segments in result['variants'].items():
        stats = segments['holdout']['bounds']
        print(name, '后段样本', stats['total'], '确定胜', stats['wins'], '确定亏', stats['losses'], '不确定', stats['unresolved'], '净收益范围', stats['profit_units'], stats['profit_upper_units'])
    print('结果：', output.resolve())


if __name__ == '__main__': main()
