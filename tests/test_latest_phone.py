import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from journal import Journal
from latest_phone import LatestPhoneMonitor, POLICY, VERSION
from market import demo_candles

KEY = 'fake-latest-device-key-for-tests'
STAMP = 1_793_783_000_000


def observation(**overrides):
    return dict(action='UP', demo=False, signal_time=STAMP, data_age_seconds=1,
                freshness_limit_seconds=60, price_kind='index', quote_age_seconds=1,
                source='币安 BTCUSDT 指数', pair='BTC / USDT', reference_price=70000,
                policy=POLICY, strategy_version=VERSION,
                research_validation={'passed': True, 'target_pct': 65},
                blocker_codes=[], blockers=[], **overrides)


class LatestPhoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'phone.json'
        self.reporter = Mock(return_value=observation())
        self.sender = Mock()
        self.monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)

    def tearDown(self):
        self.monitor.stop()
        self.temp.cleanup()

    def configure(self, **overrides):
        values = dict(key=KEY, enabled=True, mode='live', payout=.8, policy=POLICY)
        values.update(overrides)
        return self.monitor.configure(values)

    def old_settings(self, **overrides):
        values = dict(key=KEY, enabled=True, mode='coinbase', payout=.75,
                      policy='pullback_v3', last_signal_time=42, future_field={'preserve': True})
        values.update(overrides)
        self.path.write_text(json.dumps(values), encoding='utf-8')
        return values

    def test_new_monitor_defaults_to_latest_and_disabled(self):
        self.assertEqual(self.monitor.status()['policy'], POLICY)
        self.assertEqual(self.monitor.status()['strategy_version'], VERSION)
        self.assertFalse(self.monitor.status()['enabled'])
        self.monitor.check_once()
        self.reporter.assert_not_called()

    def test_migration_preserves_bark_settings_and_isolates_old_journal(self):
        saved = self.old_settings()
        Journal(self.path.parent/'logs').record('check_started', policy='pullback_v3')
        monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        migrated = json.loads(self.path.read_text())
        self.assertEqual(migrated, {**saved, 'policy': POLICY})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertTrue(monitor.status()['enabled'])
        self.assertEqual(monitor.status()['mode'], 'coinbase')
        self.assertEqual(monitor.status()['recent_events'], [])
        self.assertEqual(monitor.status()['daily_utc'], {})
        self.assertNotIn(KEY, json.dumps(monitor.status()))

    def test_migration_of_settings_without_old_policy(self):
        saved = self.old_settings()
        del saved['policy']
        self.path.write_text(json.dumps(saved))
        monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        self.assertEqual(json.loads(self.path.read_text()), {**saved, 'policy': POLICY})
        self.assertEqual(monitor.state['last_signal_time'], 42)

    def test_failed_atomic_migration_disables_and_retains_original_file(self):
        saved = self.old_settings(mode='live')
        original = self.path.read_bytes()
        with patch.object(LatestPhoneMonitor, '_save', side_effect=OSError('read only')):
            monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        self.assertFalse(monitor.status()['enabled'])
        self.assertEqual(monitor.state['key'], saved['key'])
        self.assertIn('迁移未保存', monitor.status()['message'])
        self.assertEqual(self.path.read_bytes(), original)
        monitor.check_once()
        self.reporter.assert_not_called()
        monitor.configure(dict(enabled=True, policy=POLICY))
        self.assertTrue(monitor.status()['enabled'])

    def test_latest_settings_need_no_migration_rewrite(self):
        self.old_settings(policy=POLICY)
        with patch.object(LatestPhoneMonitor, '_save') as save:
            monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        save.assert_not_called()
        self.assertEqual(monitor.status()['policy'], POLICY)

    def test_unreadable_settings_are_not_overwritten(self):
        self.path.write_text('{invalid json')
        monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        self.assertFalse(monitor.status()['enabled'])
        self.assertEqual(monitor.status()['policy'], POLICY)
        self.assertEqual(self.path.read_text(), '{invalid json')

    def test_old_tab_cannot_save_an_older_or_missing_policy(self):
        for policy in ('strict', 'candidate', 'trend_v2', 'pullback_v3', None):
            values = dict(key=KEY, enabled=True)
            if policy is not None:
                values['policy'] = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, '仅支持最新版'):
                self.monitor.configure(values)
        self.assertFalse(self.monitor.status()['configured'])

    def test_worker_calls_latest_and_persists_dedup_before_push(self):
        self.configure()
        self.monitor.check_once()
        self.reporter.assert_called_once_with('live', .8, POLICY)
        self.sender.assert_called_once()
        self.assertIn('最新版', self.sender.call_args.args[1])
        self.assertIn('历史表现不保证未来', self.sender.call_args.args[2])
        self.assertEqual(json.loads(self.path.read_text())['last_signal_time'], STAMP)
        restored = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        restored.check_once()
        self.sender.assert_called_once()

    def test_version_and_exact_true_validation_are_required(self):
        self.configure()
        for overrides, code in [
            ({'policy': 'pullback_v3'}, 'latest_policy_mismatch'),
            ({'strategy_version': 'pullback_v3.0'}, 'latest_version_mismatch'),
            ({'research_validation': {'passed': False}}, 'latest_validation_required'),
            ({'research_validation': {'passed': 1}}, 'latest_validation_required'),
            ({'research_validation': None}, 'latest_validation_required'),
        ]:
            self.reporter.return_value = {**observation(), **overrides}
            with self.subTest(overrides=overrides):
                self.monitor.check_once()
                self.assertEqual(self.monitor.status()['last_diagnostics']['action'], 'WAIT')
                self.assertIn(code, self.monitor.status()['last_diagnostics']['blocker_codes'])
        self.sender.assert_not_called()
        self.assertEqual(self.monitor.state['last_signal_time'], 0)
        self.assertIn('65%', self.monitor.status()['message'])

    def test_missing_version_metadata_also_blocks(self):
        self.configure()
        self.reporter.return_value = dict(action='UP', demo=False, signal_time=STAMP,
            data_age_seconds=1, price_kind='index', quote_age_seconds=1,
            research_validation={'passed': True})
        self.monitor.check_once()
        self.sender.assert_not_called()

    def test_test_push_works_while_waiting_or_disabled_without_using_cooldown(self):
        self.configure(enabled=False)
        self.reporter.return_value = {**observation(), 'research_validation': {'passed': False}}
        self.monitor.test()
        self.assertIn('测试提醒', self.sender.call_args.args[1])
        self.assertEqual(self.monitor.state['last_signal_time'], 0)
        self.reporter.assert_not_called()
        self.configure()
        self.reporter.return_value = observation()
        self.monitor.check_once()
        self.assertEqual(self.sender.call_count, 2)

    def test_timeout_is_not_automatically_retried(self):
        self.configure()
        self.sender.side_effect = RuntimeError('timeout')
        self.monitor.check_once()
        self.monitor.check_once()
        self.sender.assert_called_once()
        self.assertEqual(self.monitor.status()['last_push_result']['result'], 'unknown')

    def test_latest_still_enforces_live_fresh_index_data(self):
        self.configure()
        for overrides in ({'quote_age_seconds': 15}, {'data_age_seconds': 60},
                          {'demo': True}, {'price_kind': 'spot'}):
            self.reporter.return_value = {**observation(), **overrides}
            self.monitor.check_once()
        self.sender.assert_not_called()

    def test_disable_preserves_latest_and_unknown_settings(self):
        self.old_settings(mode='live')
        monitor = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        monitor.disable()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['policy'], POLICY)
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['future_field'], {'preserve': True})

    def test_all_events_have_latest_metadata_and_preserve_redaction(self):
        self.configure()
        row = self.monitor._record('diagnostic', policy='old', strategy_version='old', message=KEY)
        self.assertEqual(row['policy'], POLICY)
        self.assertEqual(row['strategy_version'], VERSION)
        self.assertEqual(row['message'], '[redacted]')
        self.monitor.check_once()
        status = self.monitor.status()
        self.assertEqual(status['last_diagnostics']['research_validation']['passed'], True)
        for event in status['recent_events']:
            self.assertEqual(event['policy'], POLICY)
            self.assertEqual(event['strategy_version'], VERSION)
        restored = LatestPhoneMonitor(self.path, self.reporter, self.sender)
        self.assertEqual(restored.status()['daily_utc'], status['daily_utc'])


class LatestAppTests(unittest.TestCase):
    def tearDown(self):
        app.CACHE.clear()

    def test_latest_report_uses_index_only_fetch_and_latest_analyzer(self):
        app.CACHE.clear()
        candles, now = demo_candles()
        with patch('app.fetch_index_candles', return_value=(candles, now)) as fetch, \
             patch('app.fetch_index_quote', return_value={'price': candles[-1].close, 'time': now}), \
             patch('app.analyze_latest', return_value=observation()) as latest, \
             patch('app.analyze', side_effect=AssertionError('legacy analyzer used')), \
             patch('app.point_backtest', side_effect=AssertionError('legacy research added')):
            result = app.report('live', .8, POLICY)
        fetch.assert_called_once_with(None, include_spot_volume=False)
        self.assertEqual(latest.call_count, 1)
        self.assertEqual(result['policy'], POLICY)
        self.assertEqual(result['source'], '币安 BTCUSDT 指数')
        self.assertNotIn('reference_study', result)
        self.assertIn(('live', 'index_only'), app.CACHE)


class LatestHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.sender = Mock()
        cls.monitor = LatestPhoneMonitor(Path(cls.temp.name)/'phone.json', Mock(), cls.sender)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.server.phone_monitor = cls.monitor
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_signals_default_to_latest(self):
        with patch('app.report', return_value=observation()) as report:
            with urlopen(self.url+'/api/signals', timeout=5) as response:
                self.assertEqual(json.load(response)['policy'], POLICY)
        report.assert_called_once_with('live', .8, POLICY)

    def test_old_signal_policies_are_rejected_before_reporting(self):
        with patch('app.report') as report:
            for policy in ('strict', 'candidate', 'trend_v2', 'pullback_v3', 'unknown'):
                with self.subTest(policy=policy), self.assertRaises(HTTPError) as error:
                    urlopen(self.url+'/api/signals?policy='+policy, timeout=5)
                self.assertEqual(error.exception.code, 400)
        report.assert_not_called()

    def test_old_phone_tab_is_rejected_but_latest_save_and_test_are_available(self):
        def post(path, values):
            request = Request(self.url+path, data=json.dumps(values).encode(),
                              headers={'Content-Type': 'application/json', 'Origin': self.url}, method='POST')
            return urlopen(request, timeout=5)
        with self.assertRaises(HTTPError) as error:
            post('/api/phone', dict(key=KEY, enabled=True, policy='pullback_v3'))
        self.assertEqual(error.exception.code, 400)
        with post('/api/phone', dict(key=KEY, enabled=False, policy=POLICY)) as response:
            result = json.load(response)
            self.assertEqual(result['policy'], POLICY)
            self.assertNotIn(KEY, json.dumps(result))
        with post('/api/phone/test', {}) as response:
            self.assertIn('测试', json.load(response)['message'])
        self.sender.assert_called_once()


if __name__ == '__main__':
    unittest.main()
