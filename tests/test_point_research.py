import copy
from decimal import Decimal
import unittest

from strategy import Candle, MINUTE
from point_research import point_backtest


def candles(count=160, price=100):
    return [Candle(i*MINUTE,price,price+10,price-10,price,0) for i in range(count)]


def features(count=160, qualified=()):
    return [dict(qualified=i in qualified,direction='UP') for i in range(count)]


class PointResearchTests(unittest.TestCase):
    def test_known_single_points_and_exact_ten_minute_reference_holding(self):
        data=candles(); rows=features(qualified=[60])
        data[62]=Candle(62*MINUTE,101,110,90,100,0)
        data[72]=Candle(72*MINUTE,102,110,90,100,0)
        result=point_backtest(data,rows)
        trade=result['trades'][0]
        self.assertEqual(trade['signal_time'],61*MINUTE)
        self.assertEqual(trade['entry_time'],62*MINUTE)
        self.assertEqual(trade['expiry'],72*MINUTE)
        self.assertEqual(trade['expiry']-trade['entry_time'],600000)
        self.assertEqual(trade['entry'],'101')
        self.assertEqual(trade['exit'],'102')
        self.assertEqual(trade['outcome'],'WIN')
        self.assertNotIn('entry_range',trade)
        self.assertEqual(result['entry_delay_seconds'],60)
        self.assertFalse(result['exact_contract_replay'])
        self.assertFalse(result['actual_order_results'])

    def test_win_loss_tie_all_count_and_decimal_precision_is_kept(self):
        data=candles(); rows=features(qualified=[60,80,100])
        data[62]=Candle(62*MINUTE,Decimal('100.000000000000001'),110,90,100,0)
        data[72]=Candle(72*MINUTE,Decimal('100.000000000000002'),110,90,100,0)
        data[82]=Candle(82*MINUTE,101,110,90,100,0)
        data[92]=Candle(92*MINUTE,100,110,90,100,0)
        result=point_backtest(data,rows,payout=Decimal('.75'))
        self.assertEqual((result['wins'],result['losses'],result['ties']),(1,1,1))
        self.assertEqual(result['total'],sum(result[k] for k in ('wins','losses','ties')))
        self.assertEqual(result['win_rate_pct'],50)
        self.assertEqual(result['profit_units'],-.25)
        self.assertEqual(result['trades'][0]['entry'],'100.000000000000001')
        self.assertEqual(result['trades'][-1]['profit'],0)
        rows[60]['direction']='DOWN'
        self.assertEqual(point_backtest(data,rows)['trades'][0]['outcome'],'LOSS')

    def test_missing_and_cross_boundary_never_become_losses_or_winners(self):
        data=candles(); rows=features(qualified=[60,80,150])
        first=point_backtest(data,rows,60,90,.8)
        self.assertEqual(first['total'],1)
        self.assertEqual(first['cross_boundary'],1)
        self.assertEqual(first['losses'],0)
        last=point_backtest(data,rows,140,160,.8)
        self.assertEqual(last['total'],0)
        self.assertEqual(last['missing_endpoints'],1)
        self.assertEqual(last['unverified'],1)
        self.assertIsNone(last['win_rate_pct'])
        self.assertIsNone(last['lower_bound_pct'])
        self.assertEqual(last['unverified_trades'][0]['outcome'],'UNVERIFIED')

    def test_cooldown_is_601_seconds_and_does_not_select_future_winners(self):
        data=candles();rows=features(qualified=range(60,90))
        result=point_backtest(data,rows,60,100)
        self.assertEqual([r['signal_time']//MINUTE for r in result['trades']],[61,72,83])
        before=copy.deepcopy(rows)
        point_backtest(data,rows,60,80)
        self.assertEqual(rows,before)
        # Mutating prices beyond a segment cannot leak back into its outcomes.
        result=point_backtest(data,rows,60,90)
        data[100]=Candle(100*MINUTE,1000,1001,999,1000,0)
        self.assertEqual(result,point_backtest(data,rows,60,90))

    def test_all_ties_do_not_report_zero_win_rate(self):
        result=point_backtest(candles(),features(qualified=[60,80]))
        self.assertEqual(result['ties'],2)
        self.assertIsNone(result['win_rate_pct'])
        self.assertEqual(result['profit_units'],0)

    def test_invalid_lengths_limits_payout_and_gaps_fail(self):
        data=candles();rows=features()
        for payout in [True,0,.001,2.1,float('nan'),'bad']:
            with self.assertRaises(ValueError):point_backtest(data,rows,payout=payout)
        with self.assertRaises(ValueError):point_backtest(data,rows[:-1])
        with self.assertRaises(ValueError):point_backtest(data,rows,start=160)
        with self.assertRaises(ValueError):point_backtest(data,rows,stop=161)
        data[80]=Candle(81*MINUTE,100,110,90,100,0)
        with self.assertRaises(ValueError):point_backtest(data,rows)


if __name__=='__main__':unittest.main()
