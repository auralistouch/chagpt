import math
import unittest
from unittest.mock import patch
from strategy import Candle, MINUTE
from trend_strategy import trend_features
from pullback_strategy import pullback_features, analyze_pullback


def series(sign=1, count=600):
    data=[];previous=100
    for i in range(count):
        close=100+sign*(.02*i+.1*math.sin(i/2))
        data.append(Candle(i*MINUTE,previous,max(previous,close)+.015,
                           min(previous,close)-.015,close,0))
        previous=close
    return data


class PullbackTests(unittest.TestCase):
    def test_dual_direction_real_triggers_and_episode_deduplication(self):
        for sign,direction in [(1,'UP'),(-1,'DOWN')]:
            rows=pullback_features(series(sign))
            triggered=[r for r in rows if r['qualified']]
            self.assertTrue(triggered)
            self.assertTrue(all(r['direction']==direction for r in triggered))
            episodes=[r['pullback_episode'] for r in triggered]
            self.assertEqual(len(episodes),len(set(episodes)))
            self.assertTrue(all(r['setup']=='回踩收回确认' for r in triggered))
            for row in triggered:
                arm=row['pullback_armed_at']//MINUTE
                current=rows.index(row)
                self.assertTrue(1 <= current-arm <= 3)
                self.assertTrue(rows[arm]['real_pullback'])
                self.assertTrue(rows[arm]['counter_candle'])

    def test_no_future_data_and_partial_five_minute_history(self):
        data=series();full=pullback_features(data)
        for stop in [121,122,124,125,126,150,301,450]:
            self.assertEqual(pullback_features(data[:stop]),full[:stop])
        partial=pullback_features(data[2:152])
        self.assertEqual(partial[2]['completed_5m'],0)
        self.assertEqual(partial[7]['completed_5m'],1)

    def test_monotonic_breakout_has_no_real_counter_pullback(self):
        data=[]
        for i in range(300):
            close=100+.02*i
            data.append(Candle(i*MINUTE,close-.02,close+.015,close-.035,close,0))
        self.assertTrue(any(r['qualified'] for r in trend_features(data)))
        self.assertFalse(any(r['qualified'] for r in pullback_features(data)))

    def test_entire_candle_below_ema_is_not_a_bounded_touch(self):
        data=series();index=200
        original=pullback_features(data)
        ema=original[index]['reference_ema9']; atr=original[index]['atr']
        data[index]=Candle(index*MINUTE,ema-2*atr,ema-1.5*atr,ema-3*atr,ema-2.5*atr,0)
        result=pullback_features(data)[index]
        self.assertFalse(result['pullback_touched'])
        self.assertFalse(result['real_pullback'])
        self.assertFalse(result['qualified'])

    def test_direction_efficiency_rejects_fast_opposite_reversal(self):
        data=series();start=200
        price=data[start-1].close
        for i in range(start,start+10):
            close=price-.04
            data[i]=Candle(i*MINUTE,price,price+.01,close-.01,close,0);price=close
        actual_base=trend_features(data)
        # Isolate a still-lagging upward 5m filter; the signed efficiency must
        # reject the reversal even if its directionless magnitude is high.
        for row in actual_base:
            row['direction']='UP'
        with patch('pullback_strategy.trend_features',return_value=actual_base):
            row=pullback_features(data)[start+9]
        self.assertLess(row['signed_efficiency'],0)
        self.assertIn('signed_efficiency',row['failed_rules'])
        self.assertFalse(row['qualified'])

    def test_new_signal_metadata_and_live_safety_filters(self):
        data=series();rows=pullback_features(data)
        index=next(i for i,row in enumerate(rows) if row['qualified'])
        prefix=data[:index+1];now=prefix[-1].time+MINUTE+1000
        options=dict(price_kind='index',quote=dict(price=prefix[-1].close,time=now))
        result=analyze_pullback(prefix,now,**options)
        self.assertEqual(result['action'],'UP')
        self.assertEqual(result['policy'],'pullback_v3')
        self.assertEqual(result['strategy_version'],'pullback_v3.0')
        self.assertEqual(result['estimated_expiry']-result['estimated_open_time'],600000)
        self.assertFalse(result['advantage_verified'])
        self.assertTrue(result['warnings'])
        for change in [dict(demo=True),dict(price_kind='spot'),dict(quote=None),
                       dict(quote=dict(price=prefix[-1].close+10,time=now))]:
            self.assertEqual(analyze_pullback(prefix,now,**{**options,**change})['action'],'WAIT')
        stale=analyze_pullback(prefix,now+59000,price_kind='index',quote=dict(price=prefix[-1].close,time=now+59000))
        self.assertEqual(stale['action'],'WAIT')
        self.assertIn('v3_stale',stale['blocker_codes'])


if __name__=='__main__':unittest.main()
