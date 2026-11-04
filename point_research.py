"""Fixed-minute index-open benchmark; never an actual event-order replay.

A feature on candle i becomes known when it closes at T. Reference entry is
candle i+2's open (T+60s); reference exit is i+12's open, 600s later. Binance
minute opens are recorded samples in that bucket, not evidence of an arbitrary
next-second order's entry/settlement index. No OHLC bounds or interpolation.
"""
from decimal import Decimal, InvalidOperation

from strategy import MINUTE, WARMUP, NOTIFY_COOLDOWN_MS, validate, wilson_lower


def point_backtest(candles, features, start=WARMUP, stop=None, payout=.8, keep_all=False):
    """Return single-point benchmark outcomes; censored records stay unverified.

    Features must have been built causally from completed candles. The caller
    selects the time segment; results never change the supplied feature rows.
    Reserve the 601s signal cooldown before checking endpoint availability so
    missing/cross-segment cases cannot make the sampling select winners.
    """
    validate(candles)
    if len(features) != len(candles):
        raise ValueError('指标与指数K线长度不一致')
    stop = len(candles) if stop is None else stop
    if type(start) is not int or type(stop) is not int or not 0 <= start < stop <= len(candles):
        raise ValueError('研究时间分段无效')
    if isinstance(payout, bool):
        raise ValueError('净收益假设须在1%至200%之间')
    try:
        premium = Decimal(str(payout))
    except (InvalidOperation, ValueError):
        raise ValueError('净收益假设须在1%至200%之间') from None
    if not premium.is_finite() or not Decimal('.01') <= premium <= Decimal('2'):
        raise ValueError('净收益假设须在1%至200%之间')

    complete, unverified = [], []
    busy_until = -1
    equity = peak = drawdown = Decimal(0)
    missing_endpoints = cross_boundary = 0
    for i in range(max(start, WARMUP), stop):
        feature = features[i]
        if not isinstance(feature, dict) or type(feature.get('qualified')) is not bool:
            raise ValueError('研究指标缺少有效qualified标记')
        if not feature['qualified']:
            continue
        direction = feature.get('direction')
        if direction not in ('UP', 'DOWN'):
            raise ValueError('研究方向无效')
        signal_time = candles[i].time+MINUTE
        if signal_time < busy_until:
            continue
        busy_until = signal_time+NOTIFY_COOLDOWN_MS
        entry_index, exit_index = i+2, i+12
        row = dict(signal_time=signal_time, time=signal_time, direction=direction,
                   entry_time=signal_time+MINUTE,
                   expiry=signal_time+MINUTE+600_000,
                   entry=None, exit=None, outcome='UNVERIFIED', profit=None)
        if exit_index >= stop:
            if exit_index >= len(candles):
                missing_endpoints += 1
                row['reason'] = 'MISSING_ENDPOINT'
            else:
                cross_boundary += 1
                row['reason'] = 'CROSS_BOUNDARY'
            unverified.append(row)
            continue
        entry = Decimal(str(candles[entry_index].open))
        exit_price = Decimal(str(candles[exit_index].open))
        if entry == exit_price:
            outcome, profit = 'TIE', Decimal(0)
        elif (exit_price > entry) == (direction == 'UP'):
            outcome, profit = 'WIN', premium
        else:
            outcome, profit = 'LOSS', Decimal(-1)
        row.update(entry=format(entry, 'f'), exit=format(exit_price, 'f'),
                   outcome=outcome, profit=float(profit),
                   won=outcome == 'WIN', lost=outcome == 'LOSS', tie=outcome == 'TIE')
        complete.append(row)
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    wins = sum(r['outcome'] == 'WIN' for r in complete)
    losses = sum(r['outcome'] == 'LOSS' for r in complete)
    ties = len(complete)-wins-losses
    decisive = wins+losses
    return dict(method='minute_open_reference', exact_contract_replay=False,
                actual_order_results=False, source='caller supplied index minute OHLC; provenance must be checked',
                entry_delay_seconds=60, holding_seconds=600, signal_cooldown_seconds=601,
                from_time=candles[start].time+MINUTE,
                to_time=candles[stop-1].time+MINUTE,
                total=len(complete), candidates=len(complete)+len(unverified),
                wins=wins, losses=losses, ties=ties,
                win_rate_pct=round(100*wins/decisive, 2) if decisive else None,
                lower_bound_pct=round(100*wilson_lower(wins, decisive), 2) if decisive else None,
                profit_units=float(equity), max_drawdown_units=float(drawdown),
                payout_assumption=float(premium),
                missing_endpoints=missing_endpoints, cross_boundary=cross_boundary,
                unverified=len(unverified), trades=complete if keep_all else complete[-30:],
                unverified_trades=unverified if keep_all else unverified[-30:],
                note='固定延后一分的指数分钟开盘点研究；非真实订单，非任意秒位下一秒开仓回放。命中率仅为研究指标。平价净收益0；跨分段或缺失端点不计胜负、也不按亏。未重演实时过滤、通知及人工延迟。')
