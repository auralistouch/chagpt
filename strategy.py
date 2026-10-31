"""Fixed, causal rules. Scores are agreement counts, never probabilities."""
import math
from dataclasses import dataclass

MINUTE = 60_000
HORIZON = 10
CONTRACT_MS = 600_000
NOTIFY_COOLDOWN_MS = 601_000
FRESH_MS = 75_000
WARMUP = 60
RULES = ('ema_separation', 'rsi_direction', 'spot_volume', 'atr_range', 'price_momentum')
RULE_LABELS = ('EMA 分离至少 0.04%', 'RSI 看涨52–68 / 看跌32–48',
               '现货成交量至少为此前20分钟均量的1.1倍', 'ATR/价格在0.03%–0.6%',
               '3分钟动量、价格方向一致且距EMA20不超过1.5 ATR')
RULE_REASONS = ('EMA20 与 EMA50 的趋势分离达标', 'RSI 动量与方向一致，未进入极端区间',
                '成交量超过此前 20 分钟均量的 1.1 倍', 'ATR 波动率处于预设范围',
                '短线价格方向一致，且未过度偏离均线')


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
        passed = (abs(fast-slow)/c.close >= .0004,
                  (direction == 'UP' and 52 <= rsi <= 68) or (direction == 'DOWN' and 32 <= rsi <= 48),
                  ratio >= 1.1, .0003 <= atr/c.close <= .006,
                  abs(c.close-fast) <= 1.5*atr and ((direction == 'UP' and momentum > 0 and c.close > fast) or (direction == 'DOWN' and momentum < 0 and c.close < fast)))
        reasons = [message for ok, message in zip(passed, RULE_REASONS) if ok]
        checks = [dict(code=code, label=label, passed=bool(ok))
                  for code, label, ok in zip(RULES, RULE_LABELS, passed)]
        output.append(dict(direction=direction, qualified=i >= WARMUP and all(passed),
                           checks=checks, failed_rules=[c['code'] for c in checks if not c['passed']],
                           reasons=reasons, score=len(reasons), rsi=round(rsi, 2),
                           atr_pct=round(100*atr/c.close, 4), volume_ratio=round(ratio, 2),
                           ema_separation_pct=round(100*abs(fast-slow)/c.close, 6),
                           momentum_3m=round(momentum, 8),
                           price_ema_atr=round(abs(c.close-fast)/atr, 4) if atr else None,
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
    """Next-second open + 600s conservative OHLC envelopes, NOT exact replay.

    For any submission in minute i+1, next-second open can reach minute i+2,
    and expiry can reach minute i+12. Include these boundaries explicitly.
    Entry and exit intervals cover every such phase. Count a win only if ALL
    possible endpoint prices win; unresolved outcomes count as losses solely
    for the existing statistical gate. No interpolation or candle-close proxy.
    """
    trades = []
    i = max(start, WARMUP)
    while i+12 < stop:
        f = features[i]
        if not f['qualified']:
            i += 1
            continue
        entry = candles[i+1]
        entry_low = min(entry.low, candles[i+2].open)
        entry_high = max(entry.high, candles[i+2].open)
        exits = candles[i+11:i+13]
        low, high = min(c.low for c in exits), max(c.high for c in exits)
        won = low > entry_high if f['direction'] == 'UP' else high < entry_low
        lost = high < entry_low if f['direction'] == 'UP' else low > entry_high
        tie = entry_low == entry_high == low == high
        unresolved = not (won or lost or tie)
        trades.append(dict(time=candles[i].time+MINUTE, direction=f['direction'],
                           expiry=candles[i].time+MINUTE+1000+CONTRACT_MS,
                           entry=None, exit=None, entry_range=[entry_low, entry_high],
                           exit_range=[low, high], won=won, tie=tie, lost=lost,
                           unresolved=unresolved, profit=payout if won else 0 if tie else -1,
                           profit_upper=payout if won or unresolved else 0 if tie else -1))
        # Cover even the latest possible submission; no overlapping envelopes.
        i += 12
    n = len(trades)
    wins = sum(t['won'] for t in trades)
    equity = peak = drawdown = 0.0
    for t in trades:
        equity += t['profit']; peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    by_direction = {}
    for direction in ('UP', 'DOWN'):
        subset = [t for t in trades if t['direction'] == direction]
        direction_wins = sum(t['won'] for t in subset)
        by_direction[direction] = dict(total=len(subset), wins=direction_wins,
                                       unresolved=sum(t['unresolved'] for t in subset),
                                       lower_bound=round(100*wilson_lower(direction_wins, len(subset)), 2))
    return dict(total=n, wins=wins, losses=sum(t['lost'] for t in trades), ties=sum(t['tie'] for t in trades),
                by_direction=by_direction,
                unresolved=sum(t['unresolved'] for t in trades),
                method='index_ohlc_next_second_600s_bounds', exact=False, horizon_seconds=600,
                win_rate=round(100*wins/n, 2) if n else None,
                lower_bound=round(100*wilson_lower(wins, n), 2),
                profit_units=round(equity, 2), max_drawdown_units=round(drawdown, 2),
                profit_upper_units=round(sum(t['profit_upper'] for t in trades), 2),
                from_time=candles[start].time, to_time=candles[stop-1].time+MINUTE,
                trades=trades[-30:])


def analyze(candles, now_ms, payout=0.8, demo=False, price_kind='spot', quote=None):
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
    blockers = []
    codes = []
    def block(code, message):
        codes.append(code); blockers.append(message)
    if price_kind != 'index': block('not_index', '当前来源不是币安 BTCUSDT 指数，仅供研究，不发送合约观察提醒')
    if payout != .8: block('payout_mismatch', '当前合约验证固定为赢净赚80%；其他比例仅供研究')
    if demo: block('demo', '当前为合成演示行情，禁止用于交易')
    if not current['qualified']:
        block('indicators', '当前未同时满足全部五项指标条件')
        blockers.extend(c['label']+'：未通过' for c in current.get('checks', []) if not c['passed'])
        codes.extend('rule_'+c for c in current.get('failed_rules', []))
    if holdout['total'] < 30: block('sample_size', f"后段样本 {holdout['total']}/30，暂不能评估可靠性")
    if not eligible and holdout['total'] >= 30: block('performance', '官方结算规则保守情形的 Wilson 下界未超过盈亏平衡点再加2个百分点')
    if not 0 <= age < FRESH_MS: block('stale_candles', '已收盘指标行情过期，暂停提示')
    quote_age = now_ms-quote['time'] if quote else None
    if price_kind == 'index' and (quote_age is None or not 0 <= quote_age < 15_000):
        block('stale_index_quote', '当前指数快照缺失或超过15秒，暂停提示')
    counts = {code: sum(code in f.get('failed_rules', []) for f in features[WARMUP:]) for code in RULES}
    action = current['direction'] if not codes else 'WAIT'
    return dict(action=action, demo=demo, signal_time=candles[-1].time+MINUTE,
                observation_time=now_ms, reference_price=quote['price'] if quote else candles[-1].close,
                price_time=quote['time'] if quote else candles[-1].time+MINUTE,
                estimated_open_time=(now_ms//1000+1)*1000,
                estimated_expiry=(now_ms//1000+1)*1000+CONTRACT_MS, horizon_seconds=600,
                timing_rule='next_second_open_then_600s',
                price_kind=price_kind, quote_age_seconds=round(quote_age/1000, 3) if quote_age is not None else None,
                freshness_limit_seconds=75, blocker_codes=codes,
                diagnostics=dict(candles=len(candles), evaluated_minutes=len(features)-WARMUP,
                                 qualified_minutes=sum(f['qualified'] for f in features[WARMUP:]),
                                 failed_rule_counts=counts, gate_sample_required=30,
                                 gate_lower_required_pct=round(100*(breakeven+.02), 4),
                                 gate_passed=eligible, validation='下一秒开仓再600秒到期；指数分钟区间保守界限，非精确结算回测'),
                data_age_seconds=round(age/1000, 1), metrics=current, blockers=blockers,
                breakeven_pct=round(100*breakeven, 2), payout=payout,
                early=early, holdout=holdout,
                chart=[dict(time=c.time, price=c.close) for c in candles[-180:]])
