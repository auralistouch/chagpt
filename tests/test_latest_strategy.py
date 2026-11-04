import csv,json,math
from pathlib import Path
import unittest
from unittest.mock import patch
from strategy import Candle,MINUTE
from latest_strategy import *

class LatestStrategyTests(unittest.TestCase):
    def test_research65_uses_aggregate_completed_rate_without_bound(self):
        rows=[dict(direction='UP',outcome='WIN') for _ in range(65)]+[dict(direction='UP',outcome='LOSS') for _ in range(35)]
        check=validation_from_rows(rows,'UP',.8)
        self.assertTrue(check['passed'])
        self.assertEqual(check['observed_pct'],65)
        good=[dict(direction='UP',outcome='WIN') for _ in range(85)]+[dict(direction='UP',outcome='LOSS') for _ in range(15)]
        self.assertTrue(validation_from_rows(good,'UP',.8)['passed'])
        self.assertTrue(validation_from_rows(good,'DOWN',.8)['passed'])
        self.assertFalse(validation_from_rows(good[:99],'UP',.8)['passed'])

    def test_ties_are_not_wins_and_directions_are_combined(self):
        def rows(side,wins,losses,ties=0):
            return [dict(direction=side,outcome='WIN')]*wins+[dict(direction=side,outcome='LOSS')]*losses+[dict(direction=side,outcome='TIE')]*ties
        check=validation_from_rows(rows('UP',60,30,10),'UP',.8)
        self.assertEqual(check['observed_pct'],60);self.assertFalse(check['passed'])
        check=validation_from_rows(rows('UP',65,30,5),'UP',.8)
        self.assertEqual(check['observed_pct'],65);self.assertTrue(check['passed'])
        check=validation_from_rows(rows('UP',40,10)+rows('DOWN',25,25),'DOWN',.8)
        self.assertEqual(check['total'],100);self.assertTrue(check['passed'])
        check=validation_from_rows(rows('UP',65,35),'UP',.1)
        self.assertTrue(check['hit_rate_target_met']);self.assertFalse(check['passed'])
        self.assertFalse(check['profit_requirement_met'])
        self.assertIn('净盈亏',check['reason'])
        self.assertFalse(validation_from_rows(rows('UP',64,36),'UP',.8)['passed'])

    def test_seen_data_never_certifies65_or_notifies(self):
        data=[Candle(i*MINUTE,100,101,99,100,0) for i in range(300)]
        features=[dict(qualified=True,direction='UP',score=4,checks=[],failed_rules=[],reasons=[],atr=1,atr_pct=1,rsi=30,efficiency_20=.2,close_location=.8,setup='带外回归确认') for _ in data]
        now=data[-1].time+MINUTE
        with patch('latest_strategy.latest_features',return_value=features):
            result=analyze_latest(data,now,price_kind='index',quote=dict(price=100,time=now),research_mode=False)
        self.assertEqual(result['action'],'WAIT')
        self.assertEqual(result['research_validation']['total'],0)
        self.assertFalse(result['research_validation']['passed'])
        self.assertFalse(result['guaranteed_win_rate'])
        self.assertIn('target_not_validated',result['blocker_codes'])
        self.assertEqual(result['policy'],POLICY)
        self.assertEqual(result['strategy_version'],VERSION)

    def test_post_freeze_simulated_samples_can_pass_but_not_guarantee(self):
        data=[]
        for i in range(1500):
            price=100+i*.01
            data.append(Candle(VALIDATION_START_MS+i*MINUTE,price,price+.02,price-.02,price+.005,0))
        features=[dict(qualified=True,direction='UP',score=4,checks=[],failed_rules=[],reasons=[],atr=1,atr_pct=1,rsi=30,efficiency_20=.2,close_location=.8,setup='模拟条件') for _ in data]
        now=data[-1].time+MINUTE
        with patch('latest_strategy.latest_features',return_value=features):
            result=analyze_latest(data,now,price_kind='index',quote=dict(price=data[-1].close,time=now))
        self.assertEqual(result['action'],'UP')
        self.assertTrue(result['research_validation']['passed'])
        self.assertGreaterEqual(result['research_validation']['decisive'],100)
        self.assertFalse(result['guaranteed_win_rate'])
        self.assertFalse(result['advantage_verified'])

    def test_causality_and_new_rules_wait_on_flat_prices(self):
        data=[];last=100
        for i in range(400):
            close=100+.1*math.sin(i/3)
            data.append(Candle(i*MINUTE,last,max(last,close)+.02,min(last,close)-.02,close,0));last=close
        whole=latest_features(data)
        for count in (71,120,199,301):self.assertEqual(whole[:count],latest_features(data[:count]))
        flat=[Candle(i*MINUTE,100,100,100,100,0) for i in range(300)]
        self.assertFalse(any(row['qualified'] for row in latest_features(flat)))

    def test_research_mode_retains_targets_but_does_not_gate_on_unseen_samples(self):
        data=[Candle(i*MINUTE,100,101,99,100,0) for i in range(300)]
        features=[dict(qualified=True,direction='UP',score=4,checks=[],failed_rules=[],reasons=[],atr=1,atr_pct=1,rsi=30,efficiency_20=.2,close_location=.8,setup='模拟研究') for _ in data]
        now=data[-1].time+MINUTE
        with patch('latest_strategy.latest_features',return_value=features):
            result=analyze_latest(data,now,price_kind='index',quote=dict(price=100,time=now))
        self.assertEqual(result['action'],'UP')
        self.assertTrue(result['research_mode'])
        self.assertFalse(result['research_validation']['passed'])
        self.assertEqual(result['research_validation']['total'],0)
        self.assertFalse(result['frequency_research']['enough_data'])
        self.assertNotIn('target_not_validated',result['blocker_codes'])
        self.assertEqual(result['reference_entry_time'],now+MINUTE)
        self.assertEqual(result['reference_expiry_time']-result['reference_entry_time'],600000)

    def test_latest_is_exact_fixed_atr_candidate_not_the_old_primary(self):
        from reversion_rules import reversion_features
        data=[Candle(i*MINUTE,100,101,99,100+.1*math.sin(i),0) for i in range(300)]
        expected=reversion_features(data,'atr_rejection')
        self.assertEqual([x['qualified'] for x in latest_features(data)], [x['qualified'] for x in expected])
        self.assertEqual([x['direction'] for x in latest_features(data)], [x['direction'] for x in expected])

    def test_legacy_primary_is_preserved_only_for_research(self):
        import reversion_rules
        self.assertEqual(reversion_rules.PRIMARY,'band_reentry')
        self.assertEqual(set(reversion_rules.CANDIDATES),{'band_reentry','atr_rejection','failed_breakout'})
        self.assertEqual(MIN_SAMPLES,100)
        self.assertEqual(TARGET,.65)

if __name__=='__main__':unittest.main()
