"""v3: signed-trend pullback confirmation. No standalone breakout entries.

Fixed research rules, not a calibrated probability or a claim of higher win rate.
Only completed index candles are read; every pullback episode can trigger once.
"""
from strategy import MINUTE, analyze
from point_research import point_backtest
from trend_strategy import trend_features, WARMUP_MINUTES

VERSION = 'pullback_v3.0'
FRESH_SECONDS = 60
LABELS = {
    'warmup': '至少120分钟及21根完整5分钟K线',
    'trend_5m': '完整5分钟EMA9/21及快线斜率与方向一致',
    'trend_1m': '1分钟EMA9/21与5分钟方向一致',
    'signed_efficiency': '近10分钟顺向净位移效率至少0.25，逆向反转不通过',
    'volatility': '此前14分钟ATR/价格在0.02%–0.4%',
    'no_chase': '距前一根EMA9不超过1.2 ATR，当前振幅不超过2.5 ATR',
    'pullback_confirm': '前1–3根真实逆向回踩均线带，当前顺向收回且每次回踩只触发一次',
}


def pullback_features(candles):
    """Reuse completed-5m calculations; replace directionless/one-sided triggers."""
    base_features = trend_features(candles)
    fast = slow = candles[0].close
    output = []
    previous_direction = None
    armed_at = None
    episode = 0
    for i, (candle, base) in enumerate(zip(candles, base_features)):
        prior_fast, prior_slow = fast, slow
        fast += (candle.close-fast)*2/10
        slow += (candle.close-slow)*2/22
        sign = 1 if base['direction'] == 'UP' else -1
        atr = base['atr']
        inherited = {c['code']: c['passed'] for c in base['checks']}
        if base['direction'] != previous_direction:
            armed_at = None
        previous_direction = base['direction']
        held = bool(atr and sign*(candle.close-prior_slow) >= -.5*atr)
        if not inherited['trend_5m'] or not held or (armed_at is not None and i-armed_at > 3):
            armed_at = None
        # Intersection with a bounded band, rather than the old single inequality.
        touches_band = bool(atr and candle.low <= prior_fast+.15*atr
                            and candle.high >= prior_fast-.15*atr)
        counter_candle = sign*(candle.close-candle.open) < 0
        real_pullback = bool(inherited['warmup'] and inherited['trend_5m']
                             and held and counter_candle and touches_band)
        if real_pullback:
            if armed_at is None: episode += 1
            armed_at = i
        steps = [abs(candles[j].close-candles[j-1].close)
                 for j in range(max(1, i-9), i+1)]
        total = sum(steps)
        signed_efficiency = sign*(candle.close-candles[max(0, i-10)].close)/total if total else 0
        prev_close = candles[i-1].close if i else candle.close
        true_range = max(candle.high-candle.low, abs(candle.high-prev_close), abs(candle.low-prev_close))
        distance = abs(candle.close-prior_fast)/atr if atr else None
        no_chase = bool(atr and distance <= 1.2 and true_range <= 2.5*atr)
        resume = i > 0 and (candle.close > candles[i-1].high if sign == 1
                           else candle.close < candles[i-1].low)
        confirmation = bool(armed_at is not None and 1 <= i-armed_at <= 3 and atr
                            and sign*(candle.close-candle.open) >= .25*atr
                            and sign*(candle.close-prior_fast) > 0 and resume and held)
        checks = dict(warmup=inherited['warmup'], trend_5m=inherited['trend_5m'],
                      trend_1m=inherited['trend_1m'], signed_efficiency=signed_efficiency >= .25,
                      volatility=inherited['volatility'], no_chase=no_chase,
                      pullback_confirm=confirmation)
        qualified = all(checks.values())
        setup = '回踩收回确认' if qualified else '回踩待收回' if armed_at is not None else '等待新回踩'
        row = {**base, 'strategy_version': VERSION, 'qualified': qualified,
               'setup': setup, 'score': sum(checks.values()),
               'checks': [dict(code=k,label=LABELS[k],passed=bool(v)) for k,v in checks.items()],
               'failed_rules': [k for k,v in checks.items() if not v],
               'reasons': [LABELS[k] for k,v in checks.items() if v],
               'efficiency': round(signed_efficiency,4),
               'signed_efficiency': round(signed_efficiency,4),
               'distance_atr': round(distance,4) if distance is not None else None,
               'pullback_touched': touches_band, 'counter_candle': counter_candle,
               'real_pullback': real_pullback, 'pullback_episode': episode,
               'pullback_armed_at': candles[armed_at].time if armed_at is not None else None,
               'trigger_formed': confirmation,
               'reference_ema9': prior_fast}
        output.append(row)
        if qualified:
            # A later breakout cannot retrigger this pullback. A new countertrend
            # candle intersecting the band is needed to arm a fresh episode.
            armed_at = None
    return output


def analyze_pullback(candles, now_ms, payout=.8, demo=False, price_kind='spot', quote=None):
    result = analyze(candles, now_ms, payout, demo, price_kind, quote,
                     policy='candidate', feature_builder=pullback_features, history_builder=point_backtest)
    current = result['metrics']
    def block(code, message):
        result['blocker_codes'].append(code); result['blockers'].append(message)
        result['action'] = 'WAIT'
    if not 0 <= result['data_age_seconds'] < FRESH_SECONDS:
        block('v3_stale', '新策略信号只在指标收盘后60秒内有效')
    if quote and current['atr'] > 0 and abs(quote['price']-candles[-1].close) > .5*current['atr']:
        block('v3_price_moved', '指数已偏离信号收盘价超过0.5 ATR，取消本次追价')
    result.update(policy='pullback_v3', strategy_version=VERSION,
                  strategy_name='顺向趋势＋真实回踩收回确认',
                  freshness_limit_seconds=FRESH_SECONDS,
                  valid_until=candles[-1].time+MINUTE+FRESH_SECONDS*1000,
                  setup=current['setup'], signal_close=candles[-1].close,
                  warnings=['新策略尚未证明盈利优势；研究命中率不等于实际订单胜率'],
                  signal_reasons=current['reasons'])
    result['reference_study'] = dict(early=result['early'], holdout=result['holdout'])
    result['diagnostics']['gate_passed'] = None
    result['diagnostics']['gate_sample_required'] = None
    result['diagnostics']['gate_lower_required_pct'] = None
    result['diagnostics']['validation'] = '按顺向趋势、真实回踩与收回确认提醒；每次回踩仅一次，不发送独立突破信号'
    return result
