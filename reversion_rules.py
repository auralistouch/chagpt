"""Three fixed research hypotheses, specified before evaluating this file.

Index OHLC only. Features use completed candles through row i, never future
prices. This module supplies candidates, not a claim of a validated edge.
The supplied 30-day dataset was previously examined: it is research data.
"""
from collections import deque
import math

CANDIDATES = ('band_reentry', 'atr_rejection', 'failed_breakout')
PRIMARY = 'band_reentry'
WARMUP = 60
RULE_TEXT = {
    'band_reentry': (
        'Previous close is outside its 20-close mean +/- 2 population standard deviations; '
        'current close crosses back inside its current band. Current body reverses toward '
        'the mean and closes in the outer 35% of its candle in that direction. '
        'Current 20-minute efficiency <= 0.35; EMA20/EMA50 separation <= 1 ATR14.'),
    'atr_rejection': (
        'Previous close is >= 1.8 ATR14 away from its EMA20 and previous 14-change '
        'simple RSI <= 30 for UP or >= 70 for DOWN. Current close recovers >= 0.35 '
        'previous ATR toward the mean, body reverses, close is in the outer 35% of '
        'its candle in that direction, and current 20-minute efficiency <= 0.55.'),
    'failed_breakout': (
        'Previous candle low/high breaches the previous 20-candle low/high excluding '
        'that previous candle, and previous close stays outside that level. Current '
        'close recovers above/below that fixed level by >= 0.10 previous ATR. '
        'Current body reverses and closes in the outer 35% of its candle in that '
        'direction; previous RSI <= 40 for UP or >= 60 for DOWN; current efficiency <= 0.55.'),
}
COMMON_TEXT = ('At least 60 completed one-minute candles. ATR14/close is 0.02%-0.5%. '
               'No volume, retrospective direction change, model fit, outcome-based '
               'threshold selection, or forward price is used. Ties/net payoff and '
               'sampling are handled by the reference research runner.')


def _states(candles):
    """Causal indicators; RSI uses rolling simple 14-change sums, ATR simple14."""
    closes = []
    trs, gains, losses = deque(maxlen=14), deque(maxlen=14), deque(maxlen=14)
    ema20 = ema50 = None
    states = []
    for i, c in enumerate(candles):
        previous_close = candles[i-1].close if i else c.close
        change = c.close-previous_close
        closes.append(c.close)
        trs.append(max(c.high-c.low, abs(c.high-previous_close), abs(c.low-previous_close)))
        gains.append(max(change, 0.0)); losses.append(max(-change, 0.0))
        ema20 = c.close if ema20 is None else ema20+2/21*(c.close-ema20)
        ema50 = c.close if ema50 is None else ema50+2/51*(c.close-ema50)
        atr = sum(trs)/len(trs)
        gain, loss = sum(gains), sum(losses)
        rsi = 100*gain/(gain+loss) if gain+loss else 50.0
        window = closes[-20:]
        mean = sum(window)/len(window)
        sd = math.sqrt(sum((x-mean)**2 for x in window)/len(window))
        if i >= 20:
            path = sum(abs(closes[j]-closes[j-1]) for j in range(i-19, i+1))
            efficiency = abs(closes[i]-closes[i-20])/path if path else 0.0
        else:
            efficiency = None
        location = (c.close-c.low)/(c.high-c.low) if c.high > c.low else .5
        states.append(dict(ema20=ema20, ema50=ema50, atr=atr, rsi=rsi,
                           band_low=mean-2*sd, band_high=mean+2*sd,
                           efficiency=efficiency, close_location=location))
    return states


def build_candidates(candles):
    """Return one feature list per fixed hypothesis; no labels/outcomes accepted."""
    states = _states(candles)
    outputs = {name: [] for name in CANDIDATES}
    for i, c in enumerate(candles):
        state = states[i]
        previous = candles[i-1] if i else c
        prior = states[i-1] if i else state
        atr, old_atr = state['atr'], prior['atr']
        common = i >= WARMUP and .0002 <= atr/c.close <= .005
        green = c.close > c.open and state['close_location'] >= .65
        red = c.close < c.open and state['close_location'] <= .35
        efficiency = state['efficiency']
        support = min(x.low for x in candles[i-21:i-1]) if i >= 21 else None
        resistance = max(x.high for x in candles[i-21:i-1]) if i >= 21 else None
        inside_band = state['band_low'] <= c.close <= state['band_high']
        band_up = previous.close < prior['band_low'] and inside_band
        band_down = previous.close > prior['band_high'] and inside_band
        extreme_up = previous.close <= prior['ema20']-1.8*old_atr and prior['rsi'] <= 30
        extreme_down = previous.close >= prior['ema20']+1.8*old_atr and prior['rsi'] >= 70
        failed_up = (support is not None and previous.low < support and previous.close < support
                     and c.close >= support+.1*old_atr and prior['rsi'] <= 40)
        failed_down = (resistance is not None and previous.high > resistance and previous.close > resistance
                       and c.close <= resistance-.1*old_atr and prior['rsi'] >= 60)
        triggers = {
            'band_reentry': (band_up, band_down),
            'atr_rejection': (extreme_up, extreme_down),
            'failed_breakout': (failed_up, failed_down),
        }
        for name in CANDIDATES:
            up_setup, down_setup = triggers[name]
            direction = 'UP' if up_setup else 'DOWN' if down_setup else None
            reversal = green if direction == 'UP' else red if direction == 'DOWN' else False
            if name == 'band_reentry':
                context = efficiency is not None and efficiency <= .35 and abs(state['ema20']-state['ema50']) <= atr
                confirmation = reversal
            elif name == 'atr_rejection':
                context = efficiency is not None and efficiency <= .55
                move = c.close-previous.close if direction == 'UP' else previous.close-c.close
                confirmation = reversal and move >= .35*old_atr
            else:
                context = efficiency is not None and efficiency <= .55
                confirmation = reversal
            flags = (common, up_setup or down_setup, context, confirmation)
            checks = [dict(code=code, label=label, passed=bool(passed)) for code, label, passed in zip(
                ('history_volatility', 'excess_setup', 'range_context', 'reversal_confirmation'),
                ('60根已收盘K线且ATR在0.02%–0.5%', '指数过度偏离或假突破',
                 '价格效率满足固定回归环境', '已收盘反转确认'), flags)]
            outputs[name].append(dict(
                direction=direction, qualified=bool(all(flags)),
                checks=checks, failed_rules=[x['code'] for x in checks if not x['passed']],
                reasons=[x['label'] for x in checks if x['passed']], score=sum(flags),
                candidate=name, setup=name, atr=atr, atr_pct=100*atr/c.close,
                rsi=state['rsi'], previous_rsi=prior['rsi'],
                efficiency_20=efficiency, close_location=state['close_location'],
                ema20=state['ema20'], ema50=state['ema50'],
                previous_ema_atr=(previous.close-prior['ema20'])/old_atr if old_atr else None,
                research_only=True,
            ))
    return outputs


def build_features(candles, candidate=PRIMARY):
    """Parent integration entry point. Default is the preregistered primary."""
    if candidate not in CANDIDATES:
        raise ValueError('Unknown fixed mean-reversion hypothesis')
    return build_candidates(candles)[candidate]


def reversion_features(candles, candidate=PRIMARY):
    """Compatibility alias for the newest strategy integration."""
    return build_features(candles, candidate)
