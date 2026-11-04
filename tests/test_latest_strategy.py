import math
import unittest
from unittest.mock import patch
from strategy import Candle, MINUTE
from latest_strategy import analyze_latest, latest_features, POLICY, VERSION
from consensus_inference import load_bundle

START=load_bundle()['metadata']['calibration_end_exclusive']

def data(count=300,flat=False):
    rows=[];last=100.
    for i in range(count):
        close=100. if flat else 100.+.4*math.sin(i/7)+.2*math.sin(i/2)
        rows.append(Candle(START+i*MINUTE,last,max(last,close)+.1,min(last,close)-.1,close,0))
        last=close
    return rows


def forced(rows,side='UP'):
    return [dict(qualified=True,direction=side,score=5,checks=[],failed_rules=[],reasons=[],
                 atr=1,atr_pct=1,rsi=50,efficiency_20=.2,close_location=.5,
                 setup='模拟行情共识',model_agreement=True,model_strength_ratios={'hgb':1.2,'extra_trees':1.1},warmup_minutes=120) for _ in rows]


class LatestStrategyTests(unittest.TestCase):
    def test_historical_zero_wins_and_small_sample_do_not_block_a_current_signal(self):
        rows=data(flat=True);now=rows[-1].time+MINUTE
        with patch('latest_strategy.latest_features',return_value=forced(rows)):
            result=analyze_latest(rows,now,payout=.01,price_kind='index',quote={'price':100,'time':now})
        self.assertEqual(result['action'],'UP')
        self.assertEqual(result['quality_assessment']['test_win_rate_pct'],0)
        self.assertLess(result['quality_assessment']['test_samples'],100)
        self.assertNotIn('research_validation',result)
        self.assertFalse(result['selection_diagnostics']['past_hit_rate_permission_gate'])
        self.assertIsNone(result['selection_diagnostics']['count_quota'])
        self.assertFalse(result['guaranteed_win_rate'])
        self.assertEqual((result['policy'],result['strategy_version']),(POLICY,VERSION))
        self.assertEqual(result['reference_entry_time'],now+MINUTE)
        self.assertEqual(result['reference_expiry_time']-result['reference_entry_time'],600000)

    def test_source_quote_and_candle_freshness_are_still_required(self):
        rows=data(flat=True);now=rows[-1].time+MINUTE
        with patch('latest_strategy.latest_features',return_value=forced(rows)):
            for changes in ({'now_ms':now+60000},{'quote':None},{'price_kind':'spot'},{'demo':True},
                            {'quote':{'price':100,'time':now-15000}}):
                args=dict(candles=rows,now_ms=now,price_kind='index',quote={'price':100,'time':now})
                args.update(changes)
                self.assertEqual(analyze_latest(**args)['action'],'WAIT')

    def test_actual_feature_prefixes_do_not_look_forward(self):
        rows=data(700);whole=latest_features(rows)
        for count in (121,199,301,601):self.assertEqual(whole[:count],latest_features(rows[:count]))
        flat=[Candle(START+i*MINUTE,100,100,100,100,0) for i in range(200)]
        self.assertFalse(any(r['qualified'] for r in latest_features(flat)))

    def test_training_period_is_not_advertised_as_output_performance(self):
        rows=[Candle(i*MINUTE,100,101,99,100,0) for i in range(300)]
        self.assertFalse(any(r['qualified'] for r in latest_features(rows)))
        result=analyze_latest(rows,300*MINUTE,price_kind='index',quote={'price':100,'time':300*MINUTE})
        self.assertIsNone(result['reference_study']['holdout'])
        self.assertEqual(result['quality_assessment']['test_samples'],0)
        self.assertIsNone(result['quality_assessment']['test_win_rate_pct'])


if __name__=='__main__':unittest.main()
