import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import app
from audit import audit
from journal import Journal
import market
from phone import PhoneMonitor, BarkRejected
from replay import contract_times, exact_replay, settle
from strategy import Candle, MINUTE, analyze, backtest, indicators


def history(count=300):
    return [Candle(i*MINUTE, 100+i, 102+i, 99+i, 101+i, 10) for i in range(count)]


class ContractTests(unittest.TestCase):
    def test_official_next_second_not_submit_price_or_fixed_601(self):
        self.assertEqual(contract_times(100_250), (101_000, 701_000))
        self.assertEqual(contract_times(100_999), (101_000, 701_000))
        self.assertEqual(contract_times(100_000), (101_000, 701_000))
        self.assertEqual(contract_times(17*3600000+59*60000+59000), (18*3600000, 18*3600000+600000))

    def test_win_loss_and_refund_keep_decimal_precision(self):
        self.assertEqual(settle('UP', '70000.00000001', '70000.00000002')[0], 'WIN')
        self.assertEqual(float(settle('DOWN', 100, 101)[1]), -1)
        self.assertEqual(float(settle('UP', 100, 101)[1]), .8)
        self.assertEqual(float(settle('UP', 100, 100)[1]), 0)
        self.assertEqual(float(settle('DOWN', 100, 100)[1]), 0)

    def test_arbitrary_submission_phase_and_causal_closed_feature(self):
        data = history()
        features = [dict(qualified=False, direction='UP') for c in data]
        features[60] = dict(qualified=True, direction='UP')
        submit = 61*MINUTE+12_345
        opened, expiry = contract_times(submit)
        ticks = {submit: 100, opened: 102, expiry: 101, submit+601000: 110}
        result = exact_replay(data, ticks, [submit], features)
        row = result['trades'][0]
        self.assertEqual(row['opened'], opened)
        self.assertEqual(row['expiry'], expiry)
        self.assertEqual(row['outcome'], 'LOSS')
        self.assertFalse(result['production_push_backtest'])

    def test_missing_endpoint_never_nearest_or_interpolated(self):
        data = history()
        features = [dict(qualified=True, direction='UP') for c in data]
        submit = 61*MINUTE+39_999
        opened, expiry = contract_times(submit)
        result = exact_replay(data, {opened:100, expiry-1:101, expiry+1:102}, [submit], features)
        self.assertEqual(result['trades'][0]['outcome'], 'MISSING_INDEX')
        self.assertEqual(result['early']['scored'], 0)
        self.assertEqual(result['early']['coverage_pct'], 0)
        self.assertEqual(result['early']['conservative_wilson_pct'], 0)

    def test_split_censoring_and_nonoverlap_do_not_select_winners(self):
        data = history()
        features = [dict(qualified=True, direction='UP') for c in data]
        submits = [61*MINUTE, 62*MINUTE, 72*MINUTE, 200*MINUTE]
        ticks = {}
        for submit in submits:
            opened, expiry = contract_times(submit)
            ticks.update({opened:100, expiry:101})
        result = exact_replay(data, ticks, submits, features)
        self.assertEqual(len(result['trades']), 3)
        self.assertEqual(result['trades'][-1]['outcome'], 'CROSS_SPLIT')

    def test_ohlc_refund_requires_entire_envelope_flat(self):
        data = [Candle(i*MINUTE, 100, 100, 100, 100, 10) for i in range(100)]
        result = backtest(data, [dict(qualified=True, direction='UP')]*100, 60, 86, .8)
        self.assertEqual(result['ties'], 2)
        self.assertEqual(result['unresolved'], 0)
        self.assertEqual(result['profit_units'], 0)

    def test_next_second_boundary_entry_covers_next_minute_open(self):
        data = history()
        c = data[62]
        data[62] = Candle(c.time, 999, 999, c.low, c.close, c.volume)
        result = backtest(data, [dict(qualified=True, direction='UP')]*len(data), 60, 80, .8)
        self.assertEqual(result['trades'][0]['entry_range'][1], 999)
        self.assertFalse(result['trades'][0]['won'])

    def test_source_quote_and_payout_gates_are_independent(self):
        data = history()
        clock = 300*MINUTE
        features = [dict(qualified=True, direction='UP')]*len(data)
        with patch('strategy.indicators', return_value=features), patch('strategy.backtest', return_value=dict(total=40,wins=40)):
            for values, code in [({}, 'not_index'), ({'price_kind':'index'}, 'stale_index_quote'),
                                 ({'price_kind':'index','quote':{'time':clock-15000,'price':100}}, 'stale_index_quote')]:
                result = analyze(data, clock, **values)
                self.assertEqual(result['action'], 'WAIT')
                self.assertIn(code, result['blocker_codes'])

    def test_dynamic_payout_flows_through_gate_replay_and_phone(self):
        data = history()
        clock = len(data)*MINUTE
        features = [dict(qualified=True, direction='UP')]*len(data)
        with patch('strategy.indicators', return_value=features), patch('strategy.backtest', return_value=dict(total=40,wins=40)):
            result = analyze(data, clock, payout=.75, price_kind='index', quote=dict(time=clock, price=100))
        self.assertEqual(result['action'], 'UP')
        self.assertEqual(result['breakeven_pct'], 57.14)
        submit = 61*MINUTE
        opened, expiry = contract_times(submit)
        result = exact_replay(data, {opened:100, expiry:101}, [submit], features, payout=.75)
        self.assertEqual(result['early']['profit_units'], .75)
        self.assertEqual(result['payout'], .75)
        with tempfile.TemporaryDirectory() as directory:
            sender = Mock()
            monitor = PhoneMonitor(Path(directory)/'phone.json', lambda *args:dict(action='UP',demo=False,signal_time=submit,data_age_seconds=1,quote_age_seconds=1,price_kind='index'), sender)
            monitor.configure(dict(key='fake-test-key-only', enabled=True, mode='live', payout=.75))
            monitor.check_once()
            sender.assert_called_once()
            self.assertIn('赢+75%', sender.call_args.args[2])

    def test_frequency_audit_does_not_call_candidates_pushes(self):
        result = audit(history(4320))
        self.assertFalse(result['exact'])
        self.assertFalse(result['production_push_backtest'])
        self.assertGreater(len(result['holdout']['daily_utc']), 0)
        self.assertIn('price_momentum', result['holdout']['failed_rule_counts'])


class IndexProviderTests(unittest.TestCase):
    def response(self, missing=False, duplicate=False):
        def respond(path, base=market.BASE, name='币安'):
            if path == '/fapi/v1/time': return dict(serverTime=5000*MINUTE+12000)
            query = parse_qs(urlsplit(path).query)
            start, end = int(query['startTime'][0]), int(query['endTime'][0])
            is_index = base == market.FUTURES
            self.assertEqual(query['pair' if is_index else 'symbol'], ['BTCUSDT'])
            rows = [[t, 100 if is_index else 900, 102 if is_index else 902,
                     99 if is_index else 899, 101 if is_index else 901,
                     'INVALID-IGNORE' if is_index else 42] for t in range(start, end+1, MINUTE)]
            if missing and not is_index: rows.pop()
            if duplicate and is_index: rows.append(rows[0])
            return rows
        return respond

    def test_index_price_and_explicit_spot_volume_join_not_index_zero_volume(self):
        with patch('market.request', side_effect=self.response()):
            data, clock = market.fetch_index_candles(count=4320)
        self.assertEqual(len(data), 4320)
        self.assertEqual(data[-1].close, 101)
        self.assertEqual(data[-1].volume, 42)
        self.assertEqual(data[-1].time, 4999*MINUTE)

    def test_missing_volume_and_duplicate_index_fail_closed(self):
        for options in [dict(missing=True), dict(duplicate=True)]:
            with patch('market.request', side_effect=self.response(**options)), self.assertRaises(RuntimeError):
                market.fetch_index_candles(count=100)

    def test_index_incremental_refresh_preserves_three_day_window(self):
        previous = [Candle(i*MINUTE, 100, 102, 99, 101, 42) for i in range(680, 5000)]
        calls = []
        response = self.response()
        def next_response(path, base=market.BASE, name='币安'):
            calls.append(path)
            if path == '/fapi/v1/time': return dict(serverTime=5002*MINUTE+12345)
            return response(path, base, name)
        with patch('market.request', side_effect=next_response):
            data, _ = market.fetch_index_candles(previous)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(data), 4320)
        self.assertEqual(data[0].time, 682*MINUTE)
        self.assertEqual(data[-1].time, 5001*MINUTE)

    def test_current_snapshot_reads_index_not_mark(self):
        with patch('market.request', return_value=dict(symbol='BTCUSDT',indexPrice='100',markPrice='999',time=123456)):
            self.assertEqual(market.fetch_index_quote(), dict(price=100,time=123456))
        for data in [dict(symbol='ETHUSDT',indexPrice='100',time=123),dict(symbol='BTCUSDT',indexPrice='NaN',time=123)]:
            with patch('market.request', return_value=data), self.assertRaises(RuntimeError): market.fetch_index_quote()

    def test_index_failure_never_relabels_spot(self):
        app.CACHE.clear()
        with patch('app.fetch_index_candles', side_effect=RuntimeError('index unavailable')), patch('app.fetch_candles') as spot, patch('app.demo_candles') as demo:
            with self.assertRaises(RuntimeError): app.report('live', .8)
        spot.assert_not_called(); demo.assert_not_called()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'phone.json'
        self.key = 'test-diagnostic-device-no-real-user'
        self.sender = Mock()
        self.data = dict(action='WAIT',demo=False,signal_time=1800000000000, data_age_seconds=1,
                         quote_age_seconds=1,price_kind='index',metrics=dict(qualified=True),
                         blocker_codes=['sample_size'],blockers=['样本不足30笔'])
        self.monitor = PhoneMonitor(self.path, lambda *args:self.data, self.sender)
        self.monitor.configure(dict(key=self.key,enabled=True,mode='live',payout=.8))

    def tearDown(self): self.monitor.stop(); self.temp.cleanup()

    def test_each_check_blocker_and_daily_candidate_dedup_are_persisted(self):
        self.monitor.check_once(); self.monitor.check_once()
        status = self.monitor.status()
        self.assertIn('样本不足30笔', status['message'])
        self.assertEqual(status['block_counts']['sample_size'], 2)
        day = next(iter(status['daily_utc'].values()))
        self.assertEqual(day['checks'], 2)
        self.assertEqual(day['candidate_minutes'], 1)
        self.assertEqual(day['signal_attempts'], 0)
        self.assertEqual(os.stat(self.path.parent/'logs/monitor.jsonl').st_mode & 0o777, 0o600)
        self.assertEqual(PhoneMonitor(self.path, Mock(), Mock()).status()['daily_utc'], status['daily_utc'])

    def test_push_accepted_rejected_unknown_and_credential_redaction(self):
        self.data.update(action='UP', blocker_codes=[], blockers=[])
        for index, error in enumerate((None, BarkRejected('reject '+self.key), OSError(self.key))):
            self.data['signal_time'] += 660000
            self.sender.side_effect = error
            self.monitor.check_once()
            self.assertEqual(self.monitor.status()['last_push_result']['result'], ('accepted','rejected','unknown')[index])
        content = ''.join(p.read_text() for p in (self.path.parent/'logs').iterdir())
        self.assertNotIn(self.key, content)
        self.assertEqual(self.sender.call_count, 3)

    def test_provider_errors_have_safe_stage_and_code(self):
        self.monitor.reporter = Mock(side_effect=market.MarketError('body '+self.key,'rate_limited','币安指数',429))
        self.monitor.check_once()
        event = self.monitor.status()['recent_events'][-1]
        self.assertEqual(event['event'], 'check_error')
        self.assertEqual(event['provider_error']['http_status'], 429)
        self.assertNotIn(self.key, json.dumps(event))
        self.sender.assert_not_called()

    def test_unknown_config_fields_and_bark_key_survive_update(self):
        saved = json.loads(self.path.read_text()); saved['future_option'] = dict(keep=True)
        self.path.write_text(json.dumps(saved))
        monitor = PhoneMonitor(self.path, Mock(), Mock())
        monitor.disable()
        reloaded = json.loads(self.path.read_text())
        self.assertEqual(reloaded['future_option'], dict(keep=True))
        self.assertEqual(reloaded['key'], self.key)

    def test_log_failure_prevents_unrecorded_push(self):
        self.data.update(action='UP',blocker_codes=[],blockers=[])
        with patch.object(self.monitor.journal, 'record', side_effect=OSError('disk full')):
            self.monitor.check_once()
        self.sender.assert_not_called()
        self.assertIn('日志写入失败', self.monitor.status()['message'])

    def test_journal_rotates_and_keeps_last_three_archives(self):
        journal = Journal(Path(self.temp.name)/'rotation', max_bytes=150)
        for i in range(20): journal.record('example', detail='x'*50)
        files = list(journal.path.parent.glob('monitor.jsonl*'))
        self.assertLessEqual(len(files), 4)
        self.assertIn(journal.path.with_name('monitor.jsonl.3'), files)


if __name__ == '__main__': unittest.main()

