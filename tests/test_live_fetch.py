"""Live retries and durable partial-page recovery, with no network access."""
from http.client import IncompleteRead
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import app
from evidence_history import IndexEvidenceHistory
import history_download
import market
from strategy import Candle, MINUTE


CLOCK = 2_000_000 * MINUTE + 1234
END = CLOCK // MINUTE * MINUTE


class LiveFetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = []
        self.sleeps = []
        self.addCleanup(app.CACHE.clear)

    @staticmethod
    def bounds(path):
        query = parse_qs(urlsplit(path).query)
        return int(query['startTime'][0]), int(query['endTime'][0]) + 1

    def provider(self, path, base=market.FUTURES, name='币安指数'):
        self.calls.append(path)
        self.assertEqual(base, market.FUTURES)
        if path == '/fapi/v1/time':
            return {'serverTime': CLOCK}
        self.assertEqual(urlsplit(path).path, '/fapi/v1/indexPriceKlines')
        query = parse_qs(urlsplit(path).query)
        self.assertEqual(query['pair'], ['BTCUSDT'])
        self.assertEqual(query['interval'], ['1m'])
        self.assertEqual(query['limit'], ['500'])
        start, end = self.bounds(path)
        # The unused sixth index field must never become volume.
        return [[stamp, '100', '102', '99', '101', '999999']
                for stamp in range(start, end, MINUTE)]

    def fetch(self, **kwargs):
        return market.fetch_index_candles(count=1020, include_spot_volume=False, **kwargs)

    def test_transient_live_errors_retry_three_times_with_bounded_delays(self):
        errors = [
            market.MarketError('disconnect', 'incomplete_response', '币安指数'),
            market.MarketError('timeout', 'network_error', '币安指数'),
            market.MarketError('partial JSON', 'invalid_json', '币安指数'),
            market.MarketError('unavailable', 'http_error', '币安指数', 503),
        ]
        for error in errors:
            with self.subTest(code=error.diagnostic), \
                 patch.object(market, 'request', side_effect=[error, error, {'ok': True}]) as request, \
                 patch.object(market.time, 'sleep') as sleep:
                self.assertEqual(market.live_request('/test'), {'ok': True})
                self.assertEqual(request.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [.5, 1.5])
        with patch.object(market, 'request', side_effect=errors[0]) as request, \
             patch.object(market.time, 'sleep') as sleep:
            with self.assertRaises(market.MarketError):
                market.live_request('/test')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_region_rate_limit_and_tls_errors_never_retry(self):
        errors = [
            market.MarketError('region', 'http_error', '币安指数', 451),
            market.MarketError('rate', 'rate_limited', '币安指数', 429),
            market.MarketError('banned', 'rate_limited', '币安指数', 418),
            market.MarketError('certificate', 'tls_error', '币安指数'),
        ]
        for error in errors:
            with self.subTest(code=error.diagnostic), \
                 patch.object(market, 'request', side_effect=error) as request, \
                 patch.object(market.time, 'sleep') as sleep:
                with self.assertRaises(market.MarketError) as raised:
                    market.live_request('/test')
                self.assertIs(raised.exception, error)
                request.assert_called_once()
                sleep.assert_not_called()

    def test_quote_has_only_two_attempts_and_keeps_index_price(self):
        error = market.MarketError('disconnect', 'incomplete_response', '币安指数')
        quote = {'symbol': 'BTCUSDT', 'indexPrice': '101', 'markPrice': '999', 'time': CLOCK}
        with patch.object(market, 'request', side_effect=[error, quote]) as request, \
             patch.object(market.time, 'sleep') as sleep:
            self.assertEqual(market.fetch_index_quote(), {'price': 101, 'time': CLOCK})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(.5)
        with patch.object(market, 'request', side_effect=error) as request, \
             patch.object(market.time, 'sleep'):
            with self.assertRaises(market.MarketError):
                market.fetch_index_quote()
        self.assertEqual(request.call_count, 2)

    def test_index_only_pages_have_500_500_20_closed_minutes(self):
        sink = Mock()
        with patch.object(market, 'request', side_effect=self.provider):
            candles, clock = self.fetch(page_sink=sink)
        self.assertEqual(clock, CLOCK)
        self.assertEqual([len(call.args[0]) for call in sink.call_args_list], [500, 500, 20])
        self.assertEqual(len(candles), 1020)
        self.assertEqual(candles[0].time, END - 1020 * MINUTE)
        self.assertEqual(candles[-1].time + MINUTE, END)
        self.assertTrue(all(c.volume == 0 for c in candles))
        self.assertTrue(all(b.time-a.time == MINUTE for a, b in zip(candles, candles[1:])))

    def test_second_page_disconnect_survives_restart_and_reuses_prefix(self):
        start = END - 1020 * MINUTE
        failed_start = start + 500 * MINUTE
        store = IndexEvidenceHistory(self.root)
        error = market.MarketError('disconnect', 'incomplete_response', '币安指数')

        def interrupted(path, *args):
            response = self.provider(path, *args)
            if path != '/fapi/v1/time' and self.bounds(path)[0] == failed_start:
                raise error
            return response

        with patch.object(market, 'request', side_effect=interrupted), \
             patch.object(market.time, 'sleep', side_effect=self.sleeps.append):
            with self.assertRaises(market.MarketError):
                self.fetch(page_sink=store.merge)
        self.assertEqual(self.sleeps, [.5, 1.5])
        self.assertEqual([self.bounds(path)[0] for path in self.calls[1:]],
                         [start] + [failed_start] * 3)
        restarted = IndexEvidenceHistory(self.root)
        seed = restarted.seed()
        self.assertEqual(len(seed), 500)
        self.calls.clear()
        with patch.object(market, 'request', side_effect=self.provider):
            candles, _ = self.fetch(previous=seed, page_sink=restarted.merge)
        # Only the final cached minute is refreshed; the first 499 are reused.
        self.assertEqual([self.bounds(path)[0] for path in self.calls[1:]],
                         [start + 499 * MINUTE, start + 999 * MINUTE])
        self.assertEqual(len(candles), 1020)
        self.assertEqual(candles[:499], seed[:499])
        self.assertEqual(restarted.seed(), candles)

    def test_tail_seed_fills_prefix_and_refreshes_final_cached_minute(self):
        start = END - 1020 * MINUTE
        tail = [Candle(stamp, 100, 102, 99, 100, 0)
                for stamp in range(start + 500 * MINUTE, END, MINUTE)]
        with patch.object(market, 'request', side_effect=self.provider):
            candles, _ = self.fetch(previous=tail)
        self.assertEqual([self.bounds(path) for path in self.calls[1:]],
                         [(start, start + 500 * MINUTE), (END - MINUTE, END)])
        self.assertEqual(candles[-2].close, 100)
        self.assertEqual(candles[-1].close, 101)

    def test_invalid_ohlc_is_not_sent_to_durable_sink(self):
        for price in ('nan', '-1', '90'):
            with self.subTest(high=price):
                sink = Mock()

                def invalid(path, *args):
                    response = self.provider(path, *args)
                    if path != '/fapi/v1/time':
                        response[-1][2] = price
                    return response

                with patch.object(market, 'request', side_effect=invalid):
                    with self.assertRaises(ValueError):
                        self.fetch(page_sink=sink)
                sink.assert_not_called()

    def test_history_budget_preserves_completed_page_and_stops_new_requests(self):
        elapsed = [0]
        store = IndexEvidenceHistory(self.root)

        def slow_page(path, *args):
            response = self.provider(path, *args)
            if path != '/fapi/v1/time':
                elapsed[0] = 46
            return response

        with patch.object(market, '_MONOTONIC', side_effect=lambda: elapsed[0]), \
             patch.object(market, 'request', side_effect=slow_page), \
             patch.object(market.time, 'sleep') as sleep:
            with self.assertRaises(market.MarketError) as raised:
                self.fetch(page_sink=store.merge)
        self.assertEqual(raised.exception.diagnostic['code'], 'batch_deadline')
        self.assertEqual(len(self.calls), 2)  # Clock and first page only.
        self.assertEqual(len(IndexEvidenceHistory(self.root).seed()), 500)
        sleep.assert_not_called()

    def test_budget_cancels_retry_before_request_can_exceed_deadline(self):
        elapsed = [0]
        error = market.MarketError('timeout', 'network_error', '币安指数')

        def timed_out(*args):
            elapsed[0] = 46
            raise error

        with patch.object(market, '_MONOTONIC', side_effect=lambda: elapsed[0]), \
             patch.object(market, 'request', side_effect=timed_out) as request, \
             patch.object(market.time, 'sleep') as sleep:
            with self.assertRaises(market.MarketError) as raised:
                market.live_request('/test', deadline=60)
        self.assertIs(raised.exception, error)
        request.assert_called_once()
        sleep.assert_not_called()

    def test_partial_seed_under_71_minutes_can_resume(self):
        store = IndexEvidenceHistory(self.root)
        start = END - 1020 * MINUTE
        store.merge([Candle(start, 100, 102, 99, 101, 0)])
        seed = IndexEvidenceHistory(self.root).seed()
        self.assertEqual(len(seed), 1)
        with patch.object(market, 'request', side_effect=self.provider):
            candles, _ = self.fetch(previous=seed, page_sink=store.merge)
        self.assertEqual(len(candles), 1020)

    def test_app_injects_persistent_seed_and_page_sink(self):
        app.CACHE.clear()
        store = IndexEvidenceHistory(self.root)
        seed = [Candle(END-MINUTE, 100, 102, 99, 101, 0)]
        store.merge(seed)
        with patch.object(app, 'EVIDENCE_HISTORY', store), \
             patch.object(app, 'fetch_index_candles', return_value=(seed, CLOCK)) as fetch, \
             patch.object(app, 'fetch_index_quote', return_value={'price': 101, 'time': CLOCK}), \
             patch.object(app, 'analyze_latest', return_value={}):
            app.report('live', .8, 'latest_v6')
        self.assertEqual(fetch.call_args.args[0], seed)
        self.assertIs(fetch.call_args.kwargs['include_spot_volume'], False)
        self.assertEqual(fetch.call_args.kwargs['page_sink'], store.merge)

    def test_historical_request_stays_single_attempt_and_retry_stays_four(self):
        self.assertIs(history_download.request, market.request)
        delays = []
        with patch.object(market, 'urlopen', side_effect=IncompleteRead(b'partial')) as open_url, \
             patch.object(market.time, 'sleep') as live_sleep:
            with self.assertRaises(market.MarketError):
                history_download.request('/test', market.FUTURES, '币安指数')
            self.assertEqual(open_url.call_count, 1)
            open_url.reset_mock()
            with self.assertRaises(market.MarketError):
                history_download._retry('/test', lambda message: None, delays.append)
            self.assertEqual(open_url.call_count, 4)
            live_sleep.assert_not_called()
        self.assertEqual(delays, [1, 3, 7])


if __name__ == '__main__':
    unittest.main()
