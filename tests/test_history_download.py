import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import history_download as history
from market import MarketError
from strategy import MINUTE


CLOCK = 2_000_000 * MINUTE + 1234


class HistoryDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = []
        self.sleeps = []

    def provider(self, path, base, name, timeout=30):
        self.assertEqual(base, history.FUTURES)
        self.assertEqual(name, '币安指数')
        self.assertEqual(timeout, 30)
        self.calls.append(path)
        if path == '/fapi/v1/time':
            return {'serverTime': CLOCK}
        parsed = urlsplit(path)
        self.assertEqual(parsed.path, '/fapi/v1/indexPriceKlines')
        query = parse_qs(parsed.query)
        self.assertEqual(query['pair'], ['BTCUSDT'])
        self.assertEqual(query['interval'], ['1m'])
        self.assertEqual(query['limit'], ['500'])
        start = int(query['startTime'][0])
        end = int(query['endTime'][0]) + 1
        # Index field 5 is deliberately nonzero: it must never become volume.
        return [[stamp, '100', '102', '99', '101', '999999']
                for stamp in range(start, end, MINUTE)]

    def download(self, count=1020):
        return history.download_index_history(
            count, self.root, progress=lambda message: None,
            sleep=self.sleeps.append)

    def page_paths(self, count=1020):
        return list((self.root / str(count)).glob('*/*.json'))

    @staticmethod
    def start_of(path):
        return int(parse_qs(urlsplit(path).query)['startTime'][0])

    def test_exhausted_disconnect_preserves_pages_and_resume_freezes_window(self):
        start = CLOCK // MINUTE * MINUTE - 1020 * MINUTE
        failed_start = start + 500 * MINUTE

        def interrupted(path, *args, **kwargs):
            response = self.provider(path, *args, **kwargs)
            if path != '/fapi/v1/time' and self.start_of(path) == failed_start:
                raise MarketError('connection interrupted', 'incomplete_response', '币安指数')
            return response

        with patch.object(history, 'request', side_effect=interrupted):
            with self.assertRaises(MarketError):
                self.download()
        page_calls = [path for path in self.calls if path != '/fapi/v1/time']
        self.assertEqual([self.start_of(path) for path in page_calls],
                         [start] + [failed_start] * 4)
        self.assertEqual(self.sleeps, [.2, 1, 3, 7])
        self.assertEqual(len(self.page_paths()), 1)
        saved_manifest = json.loads((self.root / '1020/manifest.json').read_text())

        self.calls.clear()

        def later_clock(path, *args, **kwargs):
            response = self.provider(path, *args, **kwargs)
            return {'serverTime': CLOCK + 7 * 1440 * MINUTE} if path == '/fapi/v1/time' else response

        with patch.object(history, 'request', side_effect=later_clock):
            candles, manifest = self.download()
        self.assertEqual(manifest, saved_manifest)
        self.assertEqual(len(candles), 1020)
        self.assertEqual(candles[0].time, start)
        self.assertEqual(candles[-1].time + MINUTE, saved_manifest['end'])
        self.assertEqual([self.start_of(path) for path in self.calls],
                         [failed_start, failed_start + 500 * MINUTE])

    def test_completed_cache_needs_no_network_including_server_time(self):
        with patch.object(history, 'request', side_effect=self.provider):
            expected, manifest = self.download()
        with patch.object(history, 'request', side_effect=AssertionError('network forbidden')) as request:
            actual, resumed_manifest = self.download()
        request.assert_not_called()
        self.assertEqual(actual, expected)
        self.assertEqual(resumed_manifest, manifest)

    def test_transient_errors_retry_then_succeed(self):
        errors = [
            MarketError('timeout', 'network_error', '币安指数'),
            MarketError('partial json', 'invalid_json', '币安指数'),
            MarketError('upstream unavailable', 'http_error', '币安指数', 503),
        ]
        for error in errors:
            with self.subTest(code=error.diagnostic):
                with tempfile.TemporaryDirectory() as cache:
                    attempts = []
                    delays = []

                    def flaky(path, *args, **kwargs):
                        response = self.provider(path, *args, **kwargs)
                        if path != '/fapi/v1/time':
                            attempts.append(path)
                            if len(attempts) == 1:
                                raise error
                        return response

                    with patch.object(history, 'request', side_effect=flaky):
                        candles, _ = history.download_index_history(
                            100, cache, progress=lambda message: None, sleep=delays.append)
                    self.assertEqual(len(candles), 100)
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(delays, [1])

    def test_region_rate_limit_and_certificate_errors_never_retry(self):
        errors = [
            MarketError('region', 'http_error', '币安指数', 451),
            MarketError('rate limit', 'rate_limited', '币安指数', 429),
            MarketError('IP banned', 'rate_limited', '币安指数', 418),
            MarketError('certificate', 'tls_error', '币安指数'),
        ]
        for error in errors:
            for stage in ('clock', 'page'):
                with self.subTest(code=error.diagnostic, stage=stage):
                    with tempfile.TemporaryDirectory() as cache:
                        requests = []
                        delays = []

                        def blocked(path, *args, **kwargs):
                            requests.append(path)
                            if stage == 'clock' or path != '/fapi/v1/time':
                                raise error
                            return self.provider(path, *args, **kwargs)

                        with patch.object(history, 'request', side_effect=blocked):
                            with self.assertRaises(MarketError) as raised:
                                history.download_index_history(
                                    100, cache, progress=lambda message: None, sleep=delays.append)
                        self.assertIs(raised.exception, error)
                        self.assertEqual(len(requests), 1 if stage == 'clock' else 2)
                        self.assertEqual(delays, [])
                        self.assertEqual(list(Path(cache).glob('*/*/*.json')), [])

    def test_twenty_candle_final_page_and_index_ignore_field(self):
        with patch.object(history, 'request', side_effect=self.provider):
            candles, manifest = self.download()
        saved_pages = [json.loads(path.read_text()) for path in self.page_paths()]
        self.assertEqual(sorted(len(page['rows']) for page in saved_pages), [20, 500, 500])
        self.assertEqual(len(candles), 1020)
        self.assertTrue(all(c.volume == 0 for c in candles))
        self.assertEqual(candles[-1].time + MINUTE, manifest['end'])
        self.assertTrue(all(b.time - a.time == MINUTE for a, b in zip(candles, candles[1:])))

    def test_corrupt_page_is_quarantined_and_only_that_page_redownloaded(self):
        with patch.object(history, 'request', side_effect=self.provider):
            expected, _ = self.download()
        middle = sorted(self.page_paths())[1]
        original = middle.read_text()
        corruptions = ['{"rows":', json.dumps({**json.loads(original), 'source': 'spot'}),
                       json.dumps({**json.loads(original), 'sha256': 'invalid'})]
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted[:60]):
                middle.write_text(corrupted)
                self.calls.clear()
                with patch.object(history, 'request', side_effect=self.provider):
                    actual, _ = self.download()
                self.assertEqual(actual, expected)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.start_of(self.calls[0]), int(middle.name.split('-')[0]))
        self.assertEqual(len(list(middle.parent.glob('*.invalid-*'))), 3)

    def test_malformed_success_responses_are_not_retried_or_cached(self):
        def invalid_ohlc(rows):
            rows[0][2] = '98'
            return rows

        def nonfinite(rows):
            rows[0][1] = 'NaN'
            return rows

        def duplicate(rows):
            rows[1][0] = rows[0][0]
            return rows

        def missing(rows):
            return rows[:-1]

        corruptions = [lambda rows: {'code': -1, 'msg': 'not candles'},
                       invalid_ohlc, nonfinite, duplicate, missing]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt.__name__):
                with tempfile.TemporaryDirectory() as cache:
                    requests = []
                    delays = []

                    def bad_response(path, *args, **kwargs):
                        requests.append(path)
                        response = self.provider(path, *args, **kwargs)
                        return response if path == '/fapi/v1/time' else corrupt(copy.deepcopy(response))

                    with patch.object(history, 'request', side_effect=bad_response):
                        with self.assertRaises(ValueError):
                            history.download_index_history(
                                100, cache, progress=lambda message: None, sleep=delays.append)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(delays, [])
                    self.assertEqual(list(Path(cache).glob('*/*/*.json')), [])

    def test_page_disk_failure_is_not_treated_as_network_retry(self):
        atomic_text = history.atomic_text
        disk_error = OSError('disk full')

        def fail_page_write(path, text):
            if Path(path).name != 'manifest.json':
                raise disk_error
            return atomic_text(path, text)

        with patch.object(history, 'request', side_effect=self.provider), \
                patch.object(history, 'atomic_text', side_effect=fail_page_write):
            with self.assertRaises(OSError) as raised:
                self.download()
        self.assertIs(raised.exception, disk_error)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.page_paths(), [])


if __name__ == '__main__':
    unittest.main()
