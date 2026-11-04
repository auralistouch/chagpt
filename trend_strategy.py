"""v2.0: causal 5m trend + 1m pullback/breakout, fixed before evaluation.

Heuristic, not an estimated probability. Uses only completed index candles.
No volume, no RSI conjunction, and no historical win-rate gate for alerts.
"""
from strategy import MINUTE, analyze

VERSION = 'trend_v2.0'
WARMUP_MINUTES = 120
FRESH_SECONDS = 60
LABELS = {
    'warmup': '至少120分钟及21根完整5分钟K线',
    'trend_5m': '5分钟EMA9/21与快线斜率同向，分离至少0.1个5分钟ATR',
    'trend_1m': '1分钟EMA9/21与5分钟趋势同向',
    'efficiency': '近10分钟方向效率至少0.25，过滤往返横盘',
    'volatility': '此前14分钟ATR/价格在0.02%–0.4%',
    'no_chase': '距EMA9不超过1.8 ATR，当前振幅不超过2.5 ATR',
    'trigger': '回踩重启或近5分钟区间突破，实体至少0.25 ATR',
}


def trend_features(candles):
    fast = slow = candles[0].close
    fast5 = slow5 = None
    last_slope5 = 0
    ranges, ranges5, closes5, group, output, fast_history = [], [], [], [], [], []
    current_bucket = None
    for i, candle in enumerate(candles):
        prior = candles[i-1].close if i else candle.close
        tr = max(candle.high-candle.low, abs(candle.high-prior), abs(candle.low-prior))
        # Spike detection uses ATR known before this candle.
        atr = sum(ranges[-14:])/len(ranges[-14:]) if ranges else tr
        fast += (candle.close-fast)*2/10
        slow += (candle.close-slow)*2/22
        bucket = candle.time//(5*MINUTE)
        if bucket != current_bucket:
            group = []; current_bucket = bucket
        group.append(candle)
        if candle.time % (5*MINUTE) == 4*MINUTE and len(group) == 5:
            close5 = candle.close
            prev5 = closes5[-1] if closes5 else group[0].open
            ranges5.append(max(max(c.high for c in group)-min(c.low for c in group),
                               abs(max(c.high for c in group)-prev5),
                               abs(min(c.low for c in group)-prev5)))
            if fast5 is None: fast5 = slow5 = close5
            oldfast5 = fast5
            fast5 += (close5-fast5)*2/10
            slow5 += (close5-slow5)*2/22
            last_slope5 = fast5-oldfast5
            closes5.append(close5)
        atr5 = sum(ranges5[-14:])/len(ranges5[-14:]) if ranges5 else 0
        sign = 1 if fast5 is not None and fast5 > slow5 else -1
        direction = 'UP' if sign == 1 else 'DOWN'
        enough = i >= WARMUP_MINUTES and len(closes5) >= 21
        trend5 = bool(atr5 and abs(fast5-slow5) >= .1*atr5 and sign*last_slope5 > 0)
        trend1 = sign*(fast-slow) > 0
        steps = [abs(candles[j].close-candles[j-1].close) for j in range(max(1, i-9), i+1)]
        efficiency = abs(candle.close-candles[max(0, i-10)].close)/sum(steps) if sum(steps) else 0
        volatility = bool(atr and .0002 <= atr/candle.close <= .004)
        no_chase = bool(atr and abs(candle.close-fast) <= 1.8*atr and tr <= 2.5*atr)
        recent = candles[max(0, i-3):i]
        recent_fast = fast_history[max(0, i-3):i]
        body = sign*(candle.close-candle.open) >= .25*atr if atr else False
        resume = i > 0 and (candle.close > candles[i-1].high if sign == 1 else candle.close < candles[i-1].low)
        touched = any(c.low <= ema+.15*atr if sign == 1 else c.high >= ema-.15*atr
                      for c, ema in zip(recent, recent_fast))
        held = bool(recent) and (min(c.close for c in recent) >= slow-.5*atr if sign == 1
                                else max(c.close for c in recent) <= slow+.5*atr)
        pullback = bool(body and resume and touched and held and sign*(candle.close-fast) > 0)
        prior5 = candles[max(0,i-5):i]
        breakout = bool(body and len(prior5) == 5 and (candle.close > max(c.high for c in prior5)
                                                    if sign == 1 else candle.close < min(c.low for c in prior5)))
        checks = dict(warmup=enough, trend_5m=trend5, trend_1m=trend1,
                      efficiency=efficiency >= .25, volatility=volatility, no_chase=no_chase,
                      trigger=pullback or breakout)
        trigger_name = '回踩重启' if pullback else '区间突破' if breakout else '等待触发'
        output.append(dict(direction=direction, qualified=all(checks.values()),
            checks=[dict(code=k,label=LABELS[k],passed=bool(v)) for k,v in checks.items()],
            failed_rules=[k for k,v in checks.items() if not v],
            reasons=[LABELS[k] for k,v in checks.items() if v], score=sum(checks.values()),
            strategy_version=VERSION, warmup_minutes=WARMUP_MINUTES, setup=trigger_name, ema9=round(fast,4), ema21=round(slow,4),
            ema5_fast=round(fast5,4) if fast5 is not None else None,
            ema5_slow=round(slow5,4) if slow5 is not None else None,
            completed_5m=len(closes5), atr=atr, atr_pct=round(100*atr/candle.close,4),
            efficiency=round(efficiency,4), distance_atr=round(abs(candle.close-fast)/atr,4) if atr else None))
        ranges.append(tr); fast_history.append(fast)
    return output


def analyze_trend(candles, now_ms, payout=.8, demo=False, price_kind='spot', quote=None):
    result = analyze(candles, now_ms, payout, demo, price_kind, quote,
                     policy='candidate', feature_builder=trend_features)
    current = result['metrics']
    def block(code, message):
        result['blocker_codes'].append(code); result['blockers'].append(message)
        result['action'] = 'WAIT'
    if not 0 <= result['data_age_seconds'] < FRESH_SECONDS:
        block('v2_stale', '新策略信号只在指标收盘后60秒内有效')
    if quote and current['atr'] > 0 and abs(quote['price']-candles[-1].close) > .5*current['atr']:
        block('v2_price_moved', '指数已偏离信号收盘价超过0.5 ATR，取消本次追价')
    result.update(policy='trend_v2', strategy_version=VERSION, strategy_name='5分钟趋势＋1分钟回踩/突破',
                  freshness_limit_seconds=FRESH_SECONDS,
                  valid_until=candles[-1].time+MINUTE+FRESH_SECONDS*1000,
                  setup=current['setup'], signal_close=candles[-1].close,
                  warnings=['新策略尚未证明盈利优势；历史统计仅供研究，不用于宣称成功率'],
                  signal_reasons=current['reasons'])
    result['diagnostics']['gate_sample_required'] = None
    result['diagnostics']['gate_lower_required_pct'] = None
    result['diagnostics']['validation'] = '新策略按触发条件提醒；分钟范围仅作历史研究，不是确定胜率'
    return result
