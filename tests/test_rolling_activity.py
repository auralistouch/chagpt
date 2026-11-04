import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rolling_activity import MAX_SAMPLES, POLICY, RollingActivity, STATE_NAME, WINDOW_MS


NOW = 1_800_000_000_000


def row(event, stamp=NOW - 60_000, at=NOW, **fields):
    return dict(time=at, event=event, signal_time=stamp, policy=POLICY, **fields)


class RollingActivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.clock = patch('rolling_activity.now_ms', return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temporary.cleanup()

    def test_candidates_attempts_and_service_results_are_distinct(self):
        activity = RollingActivity(self.directory)
        activity.record(row('check_result', metrics={'qualified': True}))
        activity.record(row('push_attempt', kind='signal'))
        activity.record(row('push_result', kind='signal', result='accepted'))
        activity.record(row('push_result', stamp=NOW - 120_000, kind='signal', result='rejected'))
        activity.record(row('push_result', stamp=NOW - 180_000, kind='signal', result='unknown'))
        status = activity.status()
        self.assertEqual((status['candidate_count'], status['signal_attempts'], status['accepted'], status['rejected'], status['unknown']), (1, 1, 1, 1, 1))
        self.assertEqual(status['gap'], 19)
        self.assertEqual(status['accepted_gap'], 19)
        self.assertIn('不等于手机已展示', status['note'])

    def test_duplicates_do_not_refresh_or_inflate_counts(self):
        activity = RollingActivity(self.directory)
        for at in (NOW - 10_000, NOW):
            activity.record(row('check_result', at=at, metrics={'qualified': True}))
            activity.record(row('push_attempt', at=at, kind='signal'))
            activity.record(row('push_result', at=at, kind='signal', result='unknown'))
            activity.record(row('push_result', at=at, kind='signal', result='accepted'))
        status = activity.status()
        self.assertEqual(status['candidate_count'], 1)
        self.assertEqual(status['signal_attempts'], 1)
        self.assertEqual(status['accepted'], 1)
        self.assertEqual(status['unknown'], 1)
        self.assertEqual(activity.status(NOW + WINDOW_MS - 10_000)['signal_attempts'], 0)

    def test_window_boundary_and_future_rows(self):
        activity = RollingActivity(self.directory)
        activity.record(row('push_attempt', stamp=1, at=NOW - WINDOW_MS, kind='signal'))
        activity.record(row('push_attempt', stamp=2, at=NOW - WINDOW_MS + 1, kind='signal'))
        activity.record(row('push_attempt', stamp=3, at=NOW + 1, kind='signal'))
        self.assertEqual(activity.status()['signal_attempts'], 1)
        self.assertEqual(activity.status(NOW + 1)['signal_attempts'], 0)

    def test_new_tracking_is_not_a_complete_twelve_hour_observation(self):
        activity = RollingActivity(self.directory)
        self.assertFalse(activity.status()['full_window'])
        self.assertTrue(activity.status()['tracking_complete'])
        self.assertEqual(activity.status()['gap'], 20)
        self.assertTrue(activity.status(NOW + WINDOW_MS)['full_window'])

    def test_test_notifications_unqualified_and_other_policies_are_excluded(self):
        activity = RollingActivity(self.directory)
        for event in ('push_attempt', 'push_result'):
            activity.record(row(event, kind='test', result='accepted'))
        for qualified in (False, 1, 'true'):
            activity.record(row('check_result', metrics={'qualified': qualified}))
        other = row('push_attempt', kind='signal')
        other['policy'] = 'latest_v4'
        activity.record(other)
        self.assertEqual(activity.status()['candidate_count'], 0)
        self.assertEqual(activity.status()['signal_attempts'], 0)
        self.assertEqual(activity.status()['accepted'], 0)

    def test_restart_preserves_start_counts_and_dedup_without_rescanning(self):
        activity = RollingActivity(self.directory)
        activity.record(row('push_attempt', kind='signal'))
        # A journal added after the initial import must not be replayed on restart.
        (self.directory / 'monitor.jsonl').write_text(json.dumps(row('push_attempt', stamp=1, kind='signal')) + '\n')
        with patch('rolling_activity.now_ms', return_value=NOW + 1000):
            restarted = RollingActivity(self.directory)
            self.assertFalse(restarted.record(row('push_attempt', kind='signal')))
            self.assertEqual(restarted.status()['signal_attempts'], 1)
            self.assertEqual(restarted.status()['observation_start_ms'], NOW)

    def test_first_start_imports_archives_and_only_latest_policy(self):
        journals = self.directory / 'logs'
        journals.mkdir()
        ancient = row('check_started', at=NOW - WINDOW_MS - 1)
        attempt = row('push_attempt', kind='signal')
        old = row('push_attempt', stamp=1, at=NOW - 1000, kind='signal')
        old['policy'] = 'latest_v4'
        (journals / 'monitor.jsonl.3').write_text(json.dumps(ancient) + '\n' + json.dumps(attempt) + '\n')
        (journals / 'monitor.jsonl').write_text(json.dumps(attempt) + '\n' + json.dumps(old) + '\n')
        activity = RollingActivity(self.directory, journals)
        status = activity.status()
        self.assertTrue(status['full_window'])
        self.assertTrue(status['tracking_complete'])
        self.assertEqual(status['observation_start_ms'], ancient['time'])
        self.assertEqual(status['signal_attempts'], 1)
        (journals / 'monitor.jsonl').write_text('{broken')
        self.assertTrue(activity.status()['tracking_complete'])

    def test_recovery_deduplicates_before_window_clipping(self):
        original = row('push_attempt', at=NOW - WINDOW_MS - 1, kind='signal')
        retry = row('push_attempt', at=NOW - 1, kind='signal')
        (self.directory / 'monitor.jsonl.1').write_text(json.dumps(original) + '\n')
        (self.directory / 'monitor.jsonl').write_text(json.dumps(retry) + '\n')
        activity = RollingActivity(self.directory)
        self.assertEqual(activity.status()['signal_attempts'], 0)

    def test_out_of_order_duplicate_preserves_first_actual_time(self):
        activity = RollingActivity(self.directory)
        activity.record(row('push_attempt', at=NOW - 1, kind='signal'))
        self.assertFalse(activity.record(row('push_attempt', at=NOW - 1000, kind='signal')))
        self.assertEqual(activity.status(NOW + WINDOW_MS - 1000)['signal_attempts'], 0)

    def test_storage_contains_only_whitelisted_event_fields(self):
        activity = RollingActivity(self.directory)
        sensitive = row('push_result', kind='signal', result='accepted', key='SECRET', body='BODY', message='MESSAGE', provider_error={'key': 'NESTED'})
        activity.record(sensitive)
        encoded = activity.path.read_text()
        for value in ('SECRET', 'BODY', 'MESSAGE', 'NESTED', 'provider_error'):
            self.assertNotIn(value, encoded)
        sample = json.loads(encoded)['samples'][0]
        self.assertEqual(set(sample), {'time', 'kind', 'stamp', 'result'})
        self.assertEqual(activity.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.glob('.rolling-activity-*')), [])

    def test_bad_state_or_legacy_totals_cannot_claim_complete_coverage(self):
        previous_policy = dict(schema=1, policy='latest_v4', observation_start_ms=0,
                               tracking_complete=True,
                               samples=[dict(time=NOW, kind='attempt', stamp=NOW - 60_000)])
        for content in ('{broken', json.dumps({'observation_start_ms': 0, 'accepted': 100}), json.dumps(previous_policy)):
            (self.directory / STATE_NAME).write_text(content)
            activity = RollingActivity(self.directory)
            status = activity.status()
            self.assertFalse(status['tracking_complete'])
            self.assertFalse(status['full_window'])
            self.assertEqual(status['signal_attempts'], 0)
            self.assertEqual(status['gap'], 20)

    def test_corrupt_or_torn_journal_cannot_claim_complete_coverage(self):
        (self.directory / 'monitor.jsonl').write_text(json.dumps(row('check_started', at=NOW - WINDOW_MS - 1)) + '\n{"time":')
        activity = RollingActivity(self.directory)
        status = activity.status()
        self.assertTrue(status['full_window'])
        self.assertFalse(status['tracking_complete'])
        self.assertIn('统计不完整', status['note'])
        restarted = RollingActivity(self.directory)
        self.assertFalse(restarted.status()['tracking_complete'])
        self.assertTrue(restarted.status(NOW + WINDOW_MS)['tracking_complete'])

    def test_supported_journal_event_with_missing_identity_is_incomplete(self):
        malformed = row('push_attempt', kind='signal')
        malformed.pop('signal_time')
        (self.directory / 'monitor.jsonl').write_text(json.dumps(malformed) + '\n')
        activity = RollingActivity(self.directory)
        self.assertFalse(activity.status()['tracking_complete'])

    def test_write_error_is_raised_and_retry_does_not_duplicate_accepted(self):
        activity = RollingActivity(self.directory)
        accepted = row('push_result', kind='signal', result='accepted')
        with patch.object(activity, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                activity.record(accepted)
        status = activity.status()
        self.assertFalse(status['tracking_complete'])
        self.assertIn('状态保存异常可能导致少计', status['note'])
        self.assertEqual(status['accepted'], 1)
        self.assertEqual(status['unknown'], 0)
        # A duplicate retries persistence, without inflating the count.
        self.assertFalse(activity.record(accepted))
        restarted = RollingActivity(self.directory)
        self.assertEqual(restarted.status()['accepted'], 1)
        self.assertEqual(restarted.status()['unknown'], 0)
        self.assertFalse(restarted.status()['tracking_complete'])

    def test_next_check_can_flush_a_failed_activity_write(self):
        activity = RollingActivity(self.directory)
        with patch.object(activity, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                activity.record(row('push_result', kind='signal', result='accepted'))
        self.assertFalse(activity.record(row('check_started')))
        restarted = RollingActivity(self.directory)
        self.assertEqual(restarted.status()['accepted'], 1)
        self.assertFalse(restarted.status()['tracking_complete'])

    def test_count_gaps_never_become_negative(self):
        activity = RollingActivity(self.directory)
        for index in range(21):
            activity.record(row('push_attempt', stamp=index, kind='signal'))
            activity.record(row('push_result', stamp=index, kind='signal', result='accepted'))
        self.assertEqual(activity.status()['gap'], 0)
        self.assertEqual(activity.status()['accepted_gap'], 0)

    def test_event_retention_is_bounded_and_loss_is_disclosed(self):
        with patch('rolling_activity.MAX_SAMPLES', 2):
            activity = RollingActivity(self.directory)
            for index in range(3):
                activity.record(row('push_attempt', stamp=index, at=NOW - 1000 + index, kind='signal'))
            self.assertEqual(len(json.loads(activity.path.read_text())['samples']), 2)
            self.assertFalse(activity.status()['tracking_complete'])


if __name__ == '__main__':
    unittest.main()
