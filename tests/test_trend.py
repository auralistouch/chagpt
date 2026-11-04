import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import app
import market
from phone import PhoneMonitor
from strategy import Candle, MINUTE
from trend_strategy import trend_features, analyze_trend


def series(sign=1, count=600):
    data=[]; previous=100
    for i in range(count):
        close=100+sign*(.02*i+.1*math.sin(i/2))
        data.append(Candle(i*MINUTE,previous,max(previous,close)+.015,
                           min(previous,close)-.015,close,0))
        previous=close
    return data


def triggered():
    data=series()
    features=trend_features(data)
    index=next(i for i,f in enumerate(features) if f['qualified'])
    return data[:index+1]


class TrendTests(unittest.TestCase):
    def test_real_triggers_in_both_directions_and_flat_market_waits(self):
        for sign, direction in [(1,'UP'),(-1,'DOWN')]:
            rows=[r for r in trend_features(series(sign)) if r['qualified']]
            self.assertTrue(rows)
            self.assertTrue(all(r['direction']==direction for r in rows))
        flat=[Candle(i*MINUTE,100,100,100,100,0) for i in range(600)]
        self.assertFalse(any(r['qualified'] for r in trend_features(flat)))

    def test_breakout_is_a_separate_trigger_and_evaluation_keeps_unknowns(self):
        data=[]
        for i in range(300):
            close=100+.02*i
            data.append(Candle(i*MINUTE,close-.02,close+.015,close-.035,close,0))
        rows=[f for f in trend_features(data) if f['qualified']]
        self.assertTrue(any(f['setup']=='区间突破' for f in rows))
        from evaluate import evaluate
        result=evaluate(series())
        self.assertFalse(result['exact_contract_replay'])
        self.assertFalse(result['production_push_backtest'])
        self.assertIn('daily_utc',result['holdout'])
        self.assertEqual(result['holdout']['reference']['method'],'minute_open_reference')

    def test_prefix_invariance_and_only_completed_five_minute_candles(self):
        data=series(); full=trend_features(data)
        for count in [121,122,123,124,125,126,199,200,301]:
            prefix=trend_features(data[:count])
            self.assertEqual(prefix,full[:count])
            self.assertEqual(prefix[-1]['completed_5m'],count//5)
        # Non-boundary history starts must not treat partial groups as 5m bars.
        shifted=trend_features(data[2:152])
        self.assertEqual(shifted[0]['completed_5m'],0)
        self.assertEqual(shifted[2]['completed_5m'],0)
        self.assertEqual(shifted[7]['completed_5m'],1)

    def test_new_signal_is_not_gated_by_thirty_trade_sample_or_spot_volume(self):
        data=triggered(); now=data[-1].time+MINUTE+1000
        result=analyze_trend(data,now,price_kind='index',quote=dict(price=data[-1].close,time=now))
        self.assertEqual(result['action'],'UP')
        self.assertLess(result['holdout']['total'],30)
        self.assertEqual(result['strategy_version'],'trend_v2.0')
        self.assertFalse(result['advantage_verified'])
        self.assertNotIn('sample_size',result['blocker_codes'])
        self.assertTrue(result['warnings'])
        self.assertEqual(result['estimated_expiry']-result['estimated_open_time'],600000)

    def test_stale_moved_demo_and_wrong_source_cannot_alert(self):
        data=triggered(); now=data[-1].time+MINUTE+1000
        options=dict(price_kind='index',quote=dict(price=data[-1].close,time=now))
        cases=[dict(demo=True),dict(price_kind='spot'),dict(quote=None),
               dict(quote=dict(price=data[-1].close+100,time=now))]
        for changes in cases:
            result=analyze_trend(data,now,**{**options,**changes})
            self.assertEqual(result['action'],'WAIT')
        stale=analyze_trend(data,now+59000,price_kind='index',quote=dict(price=data[-1].close,time=now+59000))
        self.assertEqual(stale['action'],'WAIT')
        self.assertIn('v2_stale',stale['blocker_codes'])
        spike=list(data);c=spike[-1]
        spike[-1]=Candle(c.time,c.open,c.high+10,c.low,c.close,0)
        self.assertIn('no_chase',trend_features(spike)[-1]['failed_rules'])

    def test_new_phone_configuration_and_real_strategy_report_push_then_dedup(self):
        data=triggered(); now=data[-1].time+MINUTE+1000
        report=analyze_trend(data,now,price_kind='index',quote=dict(price=data[-1].close,time=now))
        with tempfile.TemporaryDirectory() as folder:
            sender=Mock(); reporter=Mock(return_value=report)
            monitor=PhoneMonitor(Path(folder)/'phone.json',reporter,sender)
            self.assertEqual(monitor.status()['policy'],'pullback_v3')
            monitor.configure(dict(key='test-v2-only-fake-key',enabled=True,mode='live',policy='trend_v2'))
            monitor.check_once();monitor.check_once()
            sender.assert_called_once()
            title,body=sender.call_args.args[1:]
            self.assertIn('看涨',title)
            self.assertIn('新策略',title)
            self.assertIn('触发',body);self.assertIn('原因',body);self.assertIn('有效至',body)
            self.assertEqual(monitor.status()['last_push_result']['result'],'accepted')
            monitor.disable();self.assertEqual(monitor.status()['policy'],'trend_v2')

    def test_old_bark_settings_not_silently_switched(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'phone.json'
            path.write_text(json.dumps(dict(key='test-old-fake-key',enabled=True,mode='live',payout=.8,last_signal_time=42)))
            monitor=PhoneMonitor(path,Mock(),Mock())
            self.assertEqual(monitor.status()['policy'],'strict')
            monitor.configure(dict(enabled=True,key='',policy='trend_v2'))
            saved=json.loads(path.read_text())
            self.assertEqual(saved['key'],'test-old-fake-key')
            self.assertEqual(saved['last_signal_time'],42)
            self.assertEqual(saved['policy'],'trend_v2')

    def test_index_only_fetch_does_not_request_spot_and_ignores_field_five(self):
        def response(path,base=market.BASE,name='币安'):
            if path=='/fapi/v1/time':return dict(serverTime=1000*MINUTE)
            self.assertEqual(base,market.FUTURES)
            from urllib.parse import parse_qs,urlsplit
            q=parse_qs(urlsplit(path).query)
            return [[i,100,101,99,100,'IGNORE'] for i in range(int(q['startTime'][0]),int(q['endTime'][0])+1,MINUTE)]
        with patch('market.request',side_effect=response):
            candles,_=market.fetch_index_candles(count=100,include_spot_volume=False)
        self.assertEqual(len(candles),100)
        self.assertTrue(all(c.volume==0 for c in candles))

    def test_app_routes_to_new_strategy_and_isolates_history_cache(self):
        data=triggered();now=data[-1].time+MINUTE+1000
        app.CACHE.clear()
        with patch('app.fetch_index_candles',return_value=(data,now)) as fetch, patch('app.fetch_index_quote',return_value=dict(price=data[-1].close,time=now)):
            result=app.report('live',.8,'trend_v2')
            fetch.assert_called_once_with(None,include_spot_volume=False)
        self.assertEqual(result['policy'],'trend_v2')
        self.assertEqual(result['action'],'UP')
        self.assertNotIn('live',app.CACHE)
        self.assertIn(('live','index_only'),app.CACHE)
        app.CACHE.clear()

if __name__=='__main__':unittest.main()
