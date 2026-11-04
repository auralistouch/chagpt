"""Rolling frequency of completed minute-reference candidates, never delivery."""
from bisect import bisect_right
from statistics import median

MINUTE = 60_000
WINDOW_MS = 720 * MINUTE
TARGET = 20


def rolling_frequency(signal_times, coverage_start, coverage_end):
    """Evaluate each complete minute-ended (end-12h, end] coverage window.

    Coverage describes the observable signal interval, not first/last trades.
    Empty covered windows count as zero; a partial window is never passed.
    """
    if type(coverage_start) is not int or type(coverage_end) is not int:
        raise ValueError('研究覆盖时间须为整数毫秒')
    times = sorted(set(signal_times))
    if any(type(t) is not int for t in times):
        raise ValueError('信号时间须为整数毫秒')
    times = [t for t in times if coverage_start < t <= coverage_end]
    first = ((coverage_start + WINDOW_MS + MINUTE - 1) // MINUTE) * MINUTE
    counts = [bisect_right(times, end) - bisect_right(times, end-WINDOW_MS)
              for end in range(first, coverage_end+1, MINUTE)]
    complete = len(counts)
    reached = sum(n >= TARGET for n in counts)
    return dict(window_hours=12, target=TARGET, coverage_start=coverage_start,
                coverage_end=coverage_end, complete_window_count=complete,
                minimum=min(counts) if complete else None,
                median=median(counts) if complete else None,
                average=round(sum(counts)/complete, 4) if complete else None,
                maximum=max(counts) if complete else None,
                windows_at_target_pct=round(100*reached/complete, 2) if complete else None,
                all_windows_met=bool(complete and reached == complete),
                enough_data=bool(complete), actual_notifications=False,
                note='历史分钟参考候选；每分钟滑动检查完整12小时，不是实际推送，也不是未来次数承诺。')


def study_frequency(candles, study, start=60, stop=None):
    stop = len(candles) if stop is None else stop
    # i+12's minute open is the last endpoint required by point_backtest.
    left = candles[max(start, 60)].time
    right = candles[stop-13].time+MINUTE if stop-13 >= max(start, 60) else left
    return rolling_frequency([r['signal_time'] for r in study['trades']], left, right)
