import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from strategy import Candle, MINUTE, analyze
from phone import PhoneMonitor
from compare import compare

class PolicyTests(unittest.TestCase):
    def test_only_statistical_gate_is_optional_and_unverified_is_explicit(self):
        candles = [Candle(i*MINUTE,100,101,99,100,10) for i in range(300)]
        features = [dict(qualified=True,direction='UP',checks=[],failed_rules=[])]*300
        with patch('strategy.indicators', return_value=features):
            kwargs = dict(price_kind='index',quote=dict(time=300*MINUTE,price=100))
            strict = analyze(candles,300*MINUTE,**kwargs)
            candidate = analyze(candles,300*MINUTE,policy='candidate',**kwargs)
            self.assertEqual(strict['action'],'WAIT')
            self.assertEqual(candidate['action'],'UP')
            self.assertFalse(candidate['advantage_verified'])
            self.assertTrue(candidate['warnings'])
            for overrides in [dict(demo=True),dict(price_kind='spot'),dict(quote=None),dict(quote=dict(time=1,price=100))]:
                self.assertEqual(analyze(candles,300*MINUTE,policy='candidate',**{**kwargs,**overrides})['action'],'WAIT')
            with patch('strategy.indicators', return_value=[{**f,'qualified':False} for f in features]):
                self.assertEqual(analyze(candles,300*MINUTE,policy='candidate',**kwargs)['action'],'WAIT')

    def test_saved_candidate_policy_is_preserved_and_labelled(self):
        with tempfile.TemporaryDirectory() as folder:
            sender=Mock(); reporter=Mock(return_value=dict(action='UP',demo=False,price_kind='index',signal_time=10000000,data_age_seconds=1,quote_age_seconds=1,policy='candidate'))
            monitor=PhoneMonitor(Path(folder)/'phone.json',reporter,sender)
            monitor.configure(dict(key='fake-key-for-tests',enabled=True,mode='live',policy='candidate'))
            monitor.check_once()
            reporter.assert_called_once_with('live',.8,'candidate')
            self.assertIn('候选',sender.call_args.args[1])
            self.assertIn('尚未验证优势',sender.call_args.args[2])
            self.assertEqual(PhoneMonitor(monitor.path,reporter,sender).status()['policy'],'candidate')
            monitor.disable()
            self.assertEqual(monitor.status()['policy'],'candidate')

    def test_research_comparison_keeps_variants_and_unknowns(self):
        candles=[Candle(i*MINUTE,100,101,99,100,10) for i in range(300)]
        result=compare(candles)
        self.assertFalse(result['exact'])
        self.assertFalse(result['production_push_backtest'])
        self.assertEqual(set(result['variants']),{'original_five','research_four'})

if __name__ == '__main__': unittest.main()
