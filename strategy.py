"""Fixed, causal rules. Scores are agreement counts, never probabilities."""
import math
from dataclasses import dataclass

MINUTE = 60_000
HORIZON = 10
WARMUP = 60


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def validate(candles):
    if len(candles) < WARMUP + HORIZON + 1:
        raise ValueError("行情不足，至少需要 71 根已收盘的一分钟 K 线")
    for i, c in enumerate(candles):
        if not all(math.isfinite(v) for v in (c.open, c.high, c.low, c.close, c.volume)):
            raise ValueError("行情包含非有限数值")
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            raise ValueError("行情价格或成交量无效")
        if c.low > min(c.open, c.close) or c.high < max(c.open, c.close) or c.low > c.high:
            raise ValueError("K 线高低价格无效")
        if c.time % MINUTE or (i and c.time - candles[i-1].time != MINUTE):
            raise ValueError("行情存在缺失、重复或未按一分钟连续排列，请重新获取")


def indicators(candles):
    fast, slow = candles[0].close, candles[0].close
    gains, losses, ranges, volumes = [], [], [], []
    output = []
    for i, c in enumerate(candles):
        previous = candles[i-1].close if i else c.close
        change = c.close - previous
        gains.append(max(change, 0)); losses.append(max(-change, 0))
        ranges.append(max(c.high-c.low, abs(c.high-previous), abs(c.low-previous)))
        fast += 2/21 * (c.close-fast)
        slow += 2/51 * (c.close-slow)
        gain, loss = sum(gains[-14:]), sum(losses[-14:])
        rsi = 100*gain/(gain+loss) if gain+loss else 50
        atr = sum(ranges[-14:])/len(ranges[-14:])
        prior_volume = sum(volumes[-20:])/len(volumes[-20:]) if volumes else 0
        ratio = c.volume/prior_volume if prior_volume else 0
        momentum = c.close-candles[max(0, i-3)].close
        direction = "UP" if fast > slow else "DOWN"
        reasons = []
        if abs(fast-slow)/c.close >= 0.0004:
            reasons.append("EMA20 与 EMA50 的趋势分离达标")
        if (direction == "UP" and 52 <= rsi <= 68) or (direction == "DOWN" and 32 <= rsi <= 48):
            reasons.append("RSI 动量与方向一致，未进入极端区间")
        if ratio >= 1.1:
            reasons.append("成交量超过此前 20 分钟均量的 1.1 倍")
        if 0.0003 <= atr/c.close <= 0.006:
            reasons.append("ATR 波动率处于预设范围")
        if abs(c.close-fast) <= 1.5*atr and ((direction == "UP" and momentum > 0 and c.close > fast) or (direction == "DOWN" and momentum < 0 and c.close < fast)):
            reasons.append("短线价格方向一致，且未过度偏离均线")
        output.append(dict(direction=direction, qualified=i >= WARMUP and len(reasons) == 5,
                           reasons=reasons, score=len(reasons), rsi=round(rsi, 2),
                           atr_pct=round(100*atr/c.close, 4), volume_ratio=round(ratio, 2),
                           ema20=round(fast, 2), ema50=round(slow, 2)))
        volumes.append(c.volume)
    return output


def wilson_lower(wins, total):
    if not total:
        return 0.0
    z = 1.96
    p = wins/total
    return (p+z*z/(2*total)-z*math.sqrt(p*(1-p)/total+z*z/(4*total*total)))/(1+z*z/total)


def backtest(candles, features, start, stop, payout):
    trades = []
    i = max(start, WARMUP)
    while i+HORIZON < stop:
        f = features[i]
        if not f['qualified']:
            i += 1
            continue
        entry = candles[i+1].open
        exit_price = candles[i+HORIZON].close
        difference = exit_price-entry
        tie = difference == 0
        won = (difference > 0 if f['direction'] == 'UP' else difference < 0)
        trades.append(dict(time=candles[i].time+MINUTE, direction=f['direction'],
                           entry=entry, exit=exit_price, won=won, tie=tie,
                           profit=payout if won else -1))
        # Never hold overlapping simulated contracts.
        i += HORIZON+1
    n = len(trades)
    wins = sum(t['won'] for t in trades)
    equity = peak = drawdown = 0.0
    for t in trades:
        equity += t['profit']; peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return dict(total=n, wins=wins, losses=n-wins, ties=sum(t['tie'] for t in trades),
                win_rate=round(100*wins/n, 2) if n else None,
                lower_bound=round(100*wilson_lower(wins, n), 2),
                profit_units=round(equity, 2), max_drawdown_units=round(drawdown, 2),
                from_time=candles[start].time, to_time=candles[stop-1].time+MINUTE,
                trades=trades[-30:])


def analyze(candles, now_ms, payout=0.8, demo=False):
    validate(candles)
    if not math.isfinite(payout) or not 0.01 <= payout <= 2:
        raise ValueError("净收益比例必须在 1% 至 200% 之间")
    if candles[-1].time+MINUTE > now_ms:
        raise ValueError("行情包含尚未收盘的 K 线")
    features = indicators(candles)
    split = max(WARMUP+HORIZON+1, int(len(candles)*2/3))
    split = min(split, len(candles)-1)
    early = backtest(candles, features, WARMUP, split, payout)
    holdout = backtest(candles, features, split, len(candles), payout)
    current = features[-1]
    breakeven = 1/(1+payout)
    age = now_ms-(candles[-1].time+MINUTE)
    eligible = holdout['total'] >= 30 and wilson_lower(holdout['wins'], holdout['total']) > breakeven+0.02
    action = current['direction'] if current['qualified'] and eligible and 0 <= age <= 90_000 and not demo else 'WAIT'
    blockers = []
    if demo: blockers.append("当前为合成演示行情，禁止用于交易")
    if not current['qualified']: blockers.append("当前未同时满足全部五项指标条件")
    if holdout['total'] < 30: blockers.append("后段验证样本不足 30 笔，暂不能评估可靠性")
    if not eligible and holdout['total'] >= 30: blockers.append("历史胜率的保守下界未超过盈亏平衡点再加 2 个百分点")
    if age > 90_000: blockers.append("行情过期，暂停提示")
    return dict(action=action, demo=demo, signal_time=candles[-1].time+MINUTE,
                reference_price=candles[-1].close, estimated_expiry=candles[-1].time+MINUTE+HORIZON*MINUTE,
                data_age_seconds=round(age/1000, 1), metrics=current, blockers=blockers,
                breakeven_pct=round(100*breakeven, 2), payout=payout,
                early=early, holdout=holdout,
                chart=[dict(time=c.time, price=c.close) for c in candles[-180:]])
