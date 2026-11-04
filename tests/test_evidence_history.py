from datetime import datetime, timezone
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
from evidence_history import IndexEvidenceHistory, SOURCE, FORMAT_VERSION, utc_day
from strategy import Candle, MINUTE

START = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()*1000)


def candle(index, price=100, start=START):
    return Candle(start+index*MINUTE, price, price+1, price-1, price+.5, 12345)


def series(start, stop):
    return [candle(i) for i in range(start, stop)]


class EvidenceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)/'history'
        self.store = IndexEvidenceHistory(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def day_path(self, stamp=START):
        return self.directory/(utc_day(stamp)+'.json')

    def test_restart_restores_history_outside_current_fetch_and_saves_only_public_ohlc(self):
        self.store.merge(series(0, 8))
        restored = IndexEvidenceHistory(self.directory)
        merged, diagnostic = restored.merge(series(6, 12))
        self.assertEqual([c.time for c in merged], [c.time for c in series(0, 12)])
        self.assertTrue(all(c.volume == 0 for c in merged))
        self.assertEqual(diagnostic['new_minutes'], 4)
        self.assertFalse(diagnostic['has_gap'])
        saved = json.loads(self.day_path().read_text())
        self.assertEqual(saved['source'], SOURCE)
        self.assertEqual(saved['format_version'], FORMAT_VERSION)
        self.assertEqual(set(saved), {'source', 'format_version', 'utc_day', 'interval_ms', 'candles'})
        self.assertEqual(len(saved['candles'][0]), 5)

    def test_identical_minutes_do_not_rewrite_any_day(self):
        self.store.merge(series(1437, 1443))
        before = {path.name: path.stat().st_mtime_ns for path in self.directory.glob('*.json')}
        with patch.object(self.store, '_write_day', wraps=self.store._write_day) as write:
            merged, diagnostic = self.store.merge(series(1438, 1443))
        write.assert_not_called()
        self.assertEqual(diagnostic['written_days'], 0)
        self.assertEqual(diagnostic['new_minutes'], 0)
        self.assertEqual(diagnostic['revised_minutes'], 0)
        self.assertEqual(len(merged), 6)
        self.assertEqual(before, {path.name: path.stat().st_mtime_ns for path in self.directory.glob('*.json')})

    def test_only_changed_utc_day_is_rewritten_and_corrections_are_counted(self):
        self.store.merge(series(1437, 1443))
        first_day = self.day_path()
        before = first_day.read_bytes(), first_day.stat().st_mtime_ns
        with patch.object(self.store, '_write_day', wraps=self.store._write_day) as write:
            merged, diagnostic = self.store.merge([candle(1441, 101), candle(1442), candle(1443)])
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.args[0], '2026-11-02')
        self.assertEqual(diagnostic['new_minutes'], 1)
        self.assertEqual(diagnostic['revised_minutes'], 1)
        self.assertEqual((first_day.read_bytes(), first_day.stat().st_mtime_ns), before)
        self.assertEqual(merged[-3].open, 101)

    def test_gap_is_not_filled_and_only_latest_contiguous_tail_is_returned(self):
        incoming = [candle(i) for i in (0, 1, 4, 5, 9, 10)]
        merged, diagnostic = self.store.merge(incoming)
        self.assertEqual([c.time for c in merged], [candle(i).time for i in (9, 10)])
        self.assertTrue(diagnostic['has_gap'])
        self.assertFalse(diagnostic['filled_prices'])
        self.assertEqual(diagnostic['gap_count'], 2)
        self.assertEqual(diagnostic['missing_minutes'], 5)
        self.assertEqual(diagnostic['discarded_before_latest_gap'], 4)
        saved = json.loads(self.day_path().read_text())
        self.assertEqual(len(saved['candles']), 6)
        restored, again = IndexEvidenceHistory(self.directory).merge([candle(10)])
        self.assertEqual(restored, merged)
        self.assertEqual(again['new_minutes'], 0)

    def test_old_file_is_retained_but_only_bounded_window_and_boundary_are_loaded(self):
        store = IndexEvidenceHistory(self.directory, max_minutes=6)
        store.merge(series(1437, 1443))
        old_bytes = self.day_path().read_bytes()
        merged, diagnostic = store.merge(series(1443, 1449))
        self.assertEqual([c.time for c in merged], [c.time for c in series(1443, 1449)])
        self.assertEqual(diagnostic['retained_minutes'], 6)
        self.assertTrue(self.day_path().exists())
        self.assertEqual(self.day_path().read_bytes(), old_bytes)

    def test_exact_30_day_capacity_can_load_31_partial_utc_files(self):
        incoming = [candle(i) for i in range(60, 31*1440+60)]
        merged, diagnostic = self.store.merge(incoming)
        self.assertEqual(len(merged), 30*1440)
        self.assertEqual(merged[0].time, candle(1440+60).time)
        self.assertEqual(diagnostic['capacity_minutes'], 30*1440)
        self.assertEqual(len(list(self.directory.glob('*.json'))), 31)

    def test_corrupt_or_wrong_source_files_raise_instead_of_becoming_evidence(self):
        self.store.merge(series(0, 3))
        valid = json.loads(self.day_path().read_text())
        mutations = [
            ('source', lambda d: d.update(source='spot')),
            ('version', lambda d: d.update(format_version=999)),
            ('date', lambda d: d.update(utc_day='2026-11-02')),
            ('interval', lambda d: d.update(interval_ms=1000)),
            ('duplicate', lambda d: d['candles'].append(d['candles'][-1])),
            ('ohlc', lambda d: d['candles'][0].__setitem__(2, 1)),
            ('timestamp', lambda d: d['candles'][0].__setitem__(0, START+1)),
            ('nonfinite', lambda d: d['candles'][0].__setitem__(1, float('nan'))),
        ]
        for name, mutate in mutations:
            document = json.loads(json.dumps(valid))
            mutate(document)
            self.day_path().write_text(json.dumps(document))
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, '无法校验'):
                IndexEvidenceHistory(self.directory).merge(series(1, 3))
        self.day_path().write_text('{torn')
        with self.assertRaisesRegex(RuntimeError, '无法校验'):
            IndexEvidenceHistory(self.directory).merge(series(1, 3))

    def test_interrupted_replace_keeps_committed_file_and_restart_is_safe(self):
        self.store.merge(series(0, 3))
        old_bytes = self.day_path().read_bytes()
        with patch('evidence_history.os.replace', side_effect=OSError('power loss before commit')):
            with self.assertRaises(OSError):
                self.store.merge(series(1, 4))
        self.assertEqual(self.day_path().read_bytes(), old_bytes)
        self.assertEqual(list(self.directory.glob('*.tmp')), [])
        # A temporary file left by an abrupt process exit is not a day file.
        (self.directory/'.index-day-interrupted.tmp').write_text('{incomplete')
        merged, diagnostic = IndexEvidenceHistory(self.directory).merge([candle(2)])
        self.assertEqual([c.time for c in merged], [c.time for c in series(0, 3)])
        self.assertEqual(diagnostic['new_minutes'], 0)

    def test_file_committed_before_interruption_is_recovered_on_restart(self):
        self.store.merge(series(0, 3))
        real_fsync = __import__('os').fsync
        count = 0
        def fail_directory_fsync(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('interrupted after atomic commit')
            return real_fsync(fd)
        with patch('evidence_history.os.fsync', side_effect=fail_directory_fsync):
            with self.assertRaises(OSError):
                self.store.merge(series(1, 4))
        merged, _ = IndexEvidenceHistory(self.directory).merge([candle(3)])
        self.assertEqual([c.time for c in merged], [c.time for c in series(0, 4)])

    def test_windows_skips_only_directory_sync(self):
        with patch('evidence_history.os.name', 'nt'), \
             patch('evidence_history.os.fsync', wraps=__import__('os').fsync) as fsync:
            merged, _ = self.store.merge(series(0, 3))
        self.assertEqual(len(merged), 3)
        self.assertEqual(fsync.call_count, 1)

    def test_posix_unsupported_directory_sync_is_allowed_but_real_io_errors_raise(self):
        real_fsync = __import__('os').fsync
        for code in (errno.EINVAL, errno.ENOTSUP, errno.EIO, errno.EACCES):
            count = 0
            def directory_error(fd):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError(code, 'directory sync')
                return real_fsync(fd)
            store = IndexEvidenceHistory(self.directory/str(code))
            with self.subTest(errno=code), patch('evidence_history.os.name', 'posix'), \
                 patch('evidence_history.os.fsync', side_effect=directory_error):
                if code in (errno.EINVAL, errno.ENOTSUP):
                    self.assertEqual(len(store.merge(series(0, 3))[0]), 3)
                else:
                    with self.assertRaises(OSError):
                        store.merge(series(0, 3))

    def test_unsupported_file_fsync_is_still_a_failure_before_replace(self):
        with patch('evidence_history.os.fsync', side_effect=OSError(errno.EINVAL, 'file sync')):
            with self.assertRaises(OSError):
                self.store.merge(series(0, 3))
        self.assertFalse(self.day_path().exists())

    def test_future_cached_prices_are_not_exposed_to_earlier_request(self):
        self.store.merge(series(0, 10))
        merged, _ = IndexEvidenceHistory(self.directory).merge(series(0, 5))
        self.assertEqual(merged[-1].time, candle(4).time)
        self.assertEqual(len(merged), 5)

    def test_invalid_input_is_rejected_before_writing(self):
        for incoming in ([], [candle(1), candle(0)], [candle(0), candle(0)],
                         [Candle(START+1, 100, 101, 99, 100, 0)],
                         [Candle(START, 100, 90, 99, 100, 0)]):
            with self.subTest(incoming=incoming), self.assertRaises(ValueError):
                self.store.merge(incoming)
        self.assertFalse(self.directory.exists())


class EvidenceAppTests(unittest.TestCase):
    def tearDown(self):
        app.CACHE.clear()

    def test_latest_merges_before_getting_quote_and_passes_diagnostic(self):
        app.CACHE.clear()
        fresh = series(100, 200)
        merged = series(0, 200)
        order = []
        diagnostic = {'has_gap': True, 'gap_count': 1, 'retained_minutes': 200}
        store = Mock()
        store.seed.return_value = []
        store.merge.side_effect = lambda candles: (order.append('merge') or (merged, diagnostic))
        now = fresh[-1].time+MINUTE+1000
        with patch.object(app, 'EVIDENCE_HISTORY', store), \
             patch('app.fetch_index_candles', return_value=(fresh, now)), \
             patch('app.fetch_index_quote', side_effect=lambda: order.append('quote') or {'price': 100, 'time': now}), \
             patch('app.analyze_latest', return_value={'warnings': []}) as analyze:
            result = app.report('live', .8, 'latest_v4')
        self.assertEqual(order, ['merge', 'quote'])
        self.assertIs(analyze.call_args.args[0], merged)
        self.assertIs(result['evidence_history'], diagnostic)
        self.assertIn('未补造价格', result['warnings'][0])

    def test_demo_and_legacy_reports_never_enter_durable_index_history(self):
        app.CACHE.clear()
        data = series(0, 200)
        store = Mock()
        now = data[-1].time+MINUTE+1000
        with patch.object(app, 'EVIDENCE_HISTORY', store), \
             patch('app.demo_candles', return_value=(data, now)), \
             patch('app.analyze_latest', return_value={}):
            app.report('demo', .8, 'latest_v4')
        with patch.object(app, 'EVIDENCE_HISTORY', store), \
             patch('app.fetch_index_candles', return_value=(data, now)), \
             patch('app.fetch_index_quote', return_value={'price': 100, 'time': now}), \
             patch('app.analyze', return_value={}):
            app.report('live', .8, 'strict')
        store.merge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
