"""Offline exact-index validation, never places orders or sends notifications.

python3 replay.py --candles joined.csv --ticks index-seconds.csv --output result.json
Input ticks must be the BTCUSDT price INDEX, not spot, mark or futures last price.
"""
import argparse
from bisect import bisect_right
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from strategy import Candle, CONTRACT_MS, MINUTE, WARMUP, indicators, validate, wilson_lower


def contract_times(submitted_ms):
    if type(submitted_ms) is not int or submitted_ms < 0:
        raise ValueError('提交时间必须是非负整数毫秒')
    opened = (submitted_ms//1000+1)*1000
    return opened, opened+CONTRACT_MS


def settle(direction, entry, exit_price, payout=Decimal('.8')):
    if direction not in ('UP', 'DOWN'): raise ValueError('方向无效')
    entry, exit_price, payout = Decimal(str(entry)), Decimal(str(exit_price)), Decimal(str(payout))
    if not all(v.is_finite() for v in (entry, exit_price, payout)) or min(entry, exit_price) <= 0 or not 0 < payout <= 2:
        raise ValueError('结算价格或赔率无效')
    if entry == exit_price: return 'TIE', Decimal(0)
    won = exit_price > entry if direction == 'UP' else exit_price < entry
    return ('WIN', payout) if won else ('LOSS', Decimal(-1))


def exact_replay(candles, ticks, decisions=None, features=None, payout=.8):
    """Replay fixed indicator candidates, NOT historical production pushes.

    Match both exact exchange timestamps. Missing endpoints stay unscored and
    remain in the coverage denominator. No nearest-tick or future interpolation.
    decisions can be ANY submission instants; minute boundaries are just a
    default sampling schedule, not a contract entry restriction.
    """
    payout = Decimal(str(payout))
    if not payout.is_finite() or not Decimal('.01') <= payout <= 2:
        raise ValueError('净收益比例必须在1%至200%之间')
    validate(candles)
    features = indicators(candles) if features is None else features
    if len(features) != len(candles): raise ValueError('指标长度不一致')
    closed = [c.time+MINUTE for c in candles]
    if decisions is None: decisions = closed[WARMUP:]
    if any(type(t) is not int or t < 0 for t in ticks): raise ValueError('指数时间无效')
    prices = {t: Decimal(str(p)) for t, p in ticks.items()}
    if any(not p.is_finite() or p <= 0 for p in prices.values()): raise ValueError('指数价格无效')
    decisions = list(decisions)
    if decisions != sorted(set(decisions)): raise ValueError('提交时间必须递增且不重复')
    split_time = closed[min(len(candles)-1, max(WARMUP+1, int(len(candles)*2/3)))]
    trades = []
    busy_until = -1
    for submitted in decisions:
        opened, expiry = contract_times(submitted)
        i = bisect_right(closed, submitted)-1
        if i < WARMUP or i >= len(features) or submitted-closed[i] >= 75_000:
            continue
        if not features[i]['qualified'] or submitted < busy_until: continue
        # Reserve the same non-overlap period even when a price is missing.
        busy_until = expiry
        segment = 'holdout' if submitted >= split_time else 'early'
        row = dict(submitted=submitted, opened=opened, expiry=expiry,
                   direction=features[i]['direction'], segment=segment)
        if segment == 'early' and expiry >= split_time:
            row.update(outcome='CROSS_SPLIT', profit=None)
        elif opened not in prices or expiry not in prices:
            row.update(outcome='MISSING_INDEX', profit=None)
        else:
            outcome, profit = settle(row['direction'], prices[opened], prices[expiry], payout)
            row.update(outcome=outcome, profit=float(profit), entry=str(prices[opened]), exit=str(prices[expiry]))
        trades.append(row)
    def summary(rows, begin, end):
        complete = [t for t in rows if t['profit'] is not None]
        wins = sum(t['outcome'] == 'WIN' for t in complete)
        losses = sum(t['outcome'] == 'LOSS' for t in complete)
        days = {}
        day = begin//86_400_000*86_400_000
        while day <= end:
            days[datetime.fromtimestamp(day/1000, timezone.utc).date().isoformat()] = dict(candidates=0, scored=0, wins=0)
            day += 86_400_000
        for row in rows:
            day = datetime.fromtimestamp(row['submitted']/1000, timezone.utc).date().isoformat()
            daily = days.setdefault(day, dict(candidates=0, scored=0, wins=0))
            daily['candidates'] += 1
            daily['scored'] += row['profit'] is not None
            daily['wins'] += row['outcome'] == 'WIN'
        equity = peak = drawdown = Decimal(0)
        for row in complete:
            equity += Decimal(str(row['profit'])); peak = max(peak, equity)
            drawdown = max(drawdown, peak-equity)
        return dict(candidates=len(rows), scored=len(complete), missing_or_cross_split=len(rows)-len(complete),
                    coverage_pct=round(100*len(complete)/len(rows), 2) if rows else None,
                    wins=wins, losses=losses, ties=len(complete)-wins-losses,
                    win_rate_pct=round(100*wins/len(complete), 2) if complete else None,
                    decisive_win_rate_pct=round(100*wins/(wins+losses), 2) if wins+losses else None,
                    profit_units=float(equity), max_drawdown_units=float(drawdown),
                    conservative_wilson_pct=round(100*wilson_lower(wins, len(rows)), 4),
                    daily_utc=days)
    early = [t for t in trades if t['segment'] == 'early']
    holdout = [t for t in trades if t['segment'] == 'holdout']
    return dict(timing_rule='next_second_open_then_600s', payout=float(payout), tie_profit=0,
                exact_endpoints=True, source_claim='caller supplied Binance BTCUSDT index; verify provenance',
                production_push_backtest=False,
                note='固定规则候选方向验证；未重演逐次滚动的生产统计门槛，不等于正式提醒次数。缺失端点不补价。部分UTC日单独保留，不外推每天频次。',
                split_time=split_time, early=summary(early, closed[WARMUP], split_time-1),
                holdout=summary(holdout, split_time, closed[-1]), trades=trades)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candles', required=True, help='time_ms,open,high,low,close,spot_volume；OHLC必须为指数')
    parser.add_argument('--ticks', required=True, help='time_ms,index_price；必须为精确交易所时间的指数样本')
    parser.add_argument('--decisions', help='可选 time_ms 列，任意时刻提交时间')
    parser.add_argument('--payout', type=Decimal, default=Decimal('.8'), help='假设净收益比例，例如0.75；非官方固定赔率')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with open(args.candles, newline='', encoding='utf-8') as stream:
        candles = [Candle(int(r['time_ms']), *[float(r[k]) for k in ('open','high','low','close','spot_volume')]) for r in csv.DictReader(stream)]
    ticks = {}
    with open(args.ticks, newline='', encoding='utf-8') as stream:
        previous = -1
        for row in csv.DictReader(stream):
            stamp = int(row['time_ms'])
            if stamp <= previous: raise ValueError('指数样本时间必须递增且不重复')
            ticks[stamp] = Decimal(row['index_price']); previous = stamp
    decisions = None
    if args.decisions:
        with open(args.decisions, newline='', encoding='utf-8') as stream:
            decisions = [int(r['time_ms']) for r in csv.DictReader(stream)]
    result = exact_replay(candles, ticks, decisions, payout=args.payout)
    result['inputs_sha256'] = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                               for name, path in (('candles', args.candles), ('ticks', args.ticks))}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('early', 'holdout')}, ensure_ascii=False))


if __name__ == '__main__': main()

