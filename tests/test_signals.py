import json
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import app
import market
from strategy import Candle, MINUTE, analyze, backtest, indicators, validate, wilson_lower


def candles(n=200):
    return [Candle(i*MINUTE, 100+i, 102+i, 99+i, 101+i, 10) for i in range(n)]


class StrategyTests(unittest.TestCase):
    def test_indicators_cannot_see_future(self):
        original = candles()
        changed = original[:100]+[replace(c, close=c.close*2, high=c.high*2) for c in original[100:]]
        self.assertEqual(indicators(original)[:100], indicators(changed)[:100])
        self.assertEqual(indicators(original[:100]), indicators(original)[:100])

    def test_ten_minute_settlement_next_open_and_nonoverlap(self):
        data = candles()
        features = [dict(qualified=True, direction='UP') for _ in data]
        result = backtest(data, features, 60, 84, .8)
        self.assertEqual(result['total'], 2)
        first, second = result['trades']
        self.assertEqual(first['entry'], data[61].open)
        self.assertEqual(first['exit'], data[70].close)
        self.assertEqual(first['time']+10*MINUTE, data[70].time+MINUTE)
        self.assertGreaterEqual(second['time'], first['time']+10*MINUTE)
        self.assertTrue(first['won'])
        self.assertAlmostEqual(result['profit_units'], 1.6)

    def test_split_cannot_look_into_other_segment(self):
        data = candles()
        features = [dict(qualified=True, direction='UP') for _ in data]
        result = backtest(data, features, 60, 100, .8)
        self.assertTrue(all(t['time']+10*MINUTE <= 100*MINUTE for t in result['trades']))

    def test_ties_loss_and_drawdown(self):
        data = [Candle(i*MINUTE, 100, 101, 99, 100, 10) for i in range(100)]
        features = [dict(qualified=True, direction='DOWN') for _ in data]
        result = backtest(data, features, 60, 84, .8)
        self.assertEqual(result['ties'], 2)
        self.assertEqual(result['wins'], 0)
        self.assertEqual(result['profit_units'], -2)
        self.assertEqual(result['max_drawdown_units'], 2)

    def test_zero_trades_not_success(self):
        data = candles()
        result = backtest(data, [dict(qualified=False) for _ in data], 60, 200, .8)
        self.assertIsNone(result['win_rate'])
        self.assertEqual(result['lower_bound'], 0)

    def test_wilson_reference(self):
        self.assertAlmostEqual(wilson_lower(50, 100), .40383, places=4)
        self.assertEqual(wilson_lower(0, 0), 0)

    def test_bad_data_rejected(self):
        data = candles()
        for broken in [data[:80]+data[81:], data[:80]+data[79:],
                       data[:80]+[replace(data[80], close=float('nan'))]+data[81:],
                       data[:80]+[replace(data[80], high=1)]+data[81:]]:
            with self.assertRaises(ValueError): validate(broken)

    def test_future_and_bad_payout_rejected(self):
        data = candles()
        with self.assertRaises(ValueError): analyze(data, data[-1].time)
        for payout in [0, -1, float('nan'), float('inf'), 3]:
            with self.assertRaises(ValueError): analyze(data, 200*MINUTE, payout)

    def test_stale_demo_insufficient_and_bad_performance_suppress_signals(self):
        data = candles()
        features = [dict(qualified=True, direction='UP') for _ in data]
        with patch('strategy.indicators', return_value=features), patch('strategy.backtest', return_value=dict(total=40, wins=40)):
            self.assertEqual(analyze(data, 200*MINUTE)['action'], 'UP')
            self.assertEqual(analyze(data, 200*MINUTE, demo=True)['action'], 'WAIT')
            self.assertEqual(analyze(data, 202*MINUTE)['action'], 'WAIT')
        for stats in [dict(total=29, wins=29), dict(total=40, wins=20)]:
            with patch('strategy.indicators', return_value=features), patch('strategy.backtest', return_value=stats):
                self.assertEqual(analyze(data, 200*MINUTE)['action'], 'WAIT')


class ProviderTests(unittest.TestCase):
    def test_partial_http_response_becomes_readable_error(self):
        from http.client import IncompleteRead
        with patch('market.urlopen'), patch('market.json.load', side_effect=IncompleteRead(b'partial', 20)):
            with self.assertRaisesRegex(RuntimeError, '响应中途断开或不完整'):
                market.request('/api/v3/time')

    def test_binance_incremental_refresh_only_fetches_latest_window(self):
        from urllib.parse import parse_qs, urlsplit
        previous = [Candle(i*MINUTE,100,102,99,101,5) for i in range(680,5000)]
        calls = []
        def response(path):
            calls.append(path)
            if path.endswith('/time'): return {'serverTime': 5002*MINUTE+12345}
            query = parse_qs(urlsplit(path).query)
            self.assertEqual(int(query['startTime'][0]), 4999*MINUTE)
            end = int(query['endTime'][0])//MINUTE
            return [[i*MINUTE,100,102,99,101,5] for i in range(4999,end+1)]
        with patch('market.request',side_effect=response): data, now = market.fetch_candles(previous)
        self.assertEqual(len(calls),2)
        self.assertEqual(len(data),4320)
        self.assertEqual(data[0].time,682*MINUTE)
        self.assertEqual(data[-1].time,5001*MINUTE)

    def test_error_keeps_cache_for_recovery_but_never_serves_stale_success(self):
        previous, now = market.demo_candles()
        app.CACHE.clear()
        app.CACHE['live'] = dict(candles=previous,clock=now,fetched=0)
        with patch('app.time.monotonic',return_value=100), patch('app.fetch_candles',side_effect=RuntimeError('temporary disconnect')):
            with self.assertRaisesRegex(RuntimeError, 'temporary disconnect'): app.report('live',.8)
        self.assertEqual(app.CACHE['live']['candles'],previous)
        with patch('app.time.monotonic',return_value=131), patch('app.fetch_candles',return_value=(previous,now)) as provider:
            result=app.report('live',.8)
        provider.assert_called_once_with(previous)
        self.assertFalse(result['demo'])

    def coinbase_response(self, clock_minute=5000, missing=False):
        from datetime import datetime
        from urllib.parse import parse_qs, urlsplit
        calls = []
        def response(path, base, name):
            self.assertEqual(base, market.COINBASE)
            self.assertEqual(name, 'Coinbase')
            calls.append(path)
            if path == '/time': return {'epoch': clock_minute*60+12.345}
            query = parse_qs(urlsplit(path).query)
            start = int(datetime.fromisoformat(query['start'][0]).timestamp())//60
            last = int(datetime.fromisoformat(query['end'][0]).timestamp())//60
            # Unordered response, with extra out-of-window buckets.
            rows = [[i*60, '99', '102', '100', '101', '5'] for i in range(start-1, last+2)]
            if missing: rows = [r for r in rows if r[0] != start*60]
            return list(reversed(rows))
        return response, calls

    def test_coinbase_pagination_sort_units_and_no_open_candle(self):
        response, calls = self.coinbase_response()
        with patch('market.request', side_effect=response), patch('market.time.sleep'):
            data, now = market.fetch_coinbase()
        self.assertEqual(len(data), 4320)
        self.assertEqual(data[0].time, 680*MINUTE)
        self.assertEqual(data[-1].time, 4999*MINUTE)
        self.assertEqual(data[-1].open, 100)
        self.assertEqual(data[-1].low, 99)
        self.assertEqual(data[-1].high, 102)
        self.assertEqual(data[-1].close, 101)
        self.assertEqual(len(calls), 16)  # time + 15 pages
        self.assertLessEqual(data[-1].time+MINUTE, now)

    def test_coinbase_incremental_refresh_does_not_redownload_three_days(self):
        previous = [Candle(i*MINUTE,100,102,99,101,5) for i in range(680,5000)]
        response, calls = self.coinbase_response(clock_minute=5002)
        with patch('market.request', side_effect=response):
            data, now = market.fetch_coinbase(previous)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(data), 4320)
        self.assertEqual(data[0].time, 682*MINUTE)
        self.assertEqual(data[-1].time, 5001*MINUTE)

    def test_coinbase_missing_data_is_never_fabricated(self):
        response, calls = self.coinbase_response(missing=True)
        with patch('market.request', side_effect=response):
            with self.assertRaisesRegex(RuntimeError, '缺失'): market.fetch_coinbase()

    def test_coinbase_failure_never_calls_binance(self):
        app.CACHE.clear()
        with patch('app.fetch_coinbase', side_effect=RuntimeError('Coinbase unavailable')), patch('app.fetch_candles') as binance, patch('app.demo_candles') as demo:
            with self.assertRaisesRegex(RuntimeError, 'Coinbase unavailable'): app.report('coinbase', .8)
            binance.assert_not_called()
            demo.assert_not_called()

    def test_slow_download_does_not_hide_staleness(self):
        app.CACHE.clear()
        data = candles()
        with patch('app.fetch_candles', return_value=(data, 200*MINUTE)), patch('app.time.monotonic', side_effect=[0, 0, 120, 120]):
            result = app.report('live', .8)
        self.assertEqual(result['data_age_seconds'], 120)
        self.assertEqual(result['action'], 'WAIT')

    def test_pagination_excludes_open_candle(self):
        from urllib.parse import urlsplit, parse_qs
        calls = []
        def response(path):
            calls.append(path)
            if path.endswith('/time'): return dict(serverTime=5000*MINUTE+12345)
            query = parse_qs(urlsplit(path).query)
            last = int(query['endTime'][0])//MINUTE
            limit = int(query['limit'][0])
            return [[i*MINUTE, '100', '102', '99', '101', '5'] for i in range(last-limit+1, last+1)]
        with patch('market.request', side_effect=response):
            data, now = market.fetch_candles()
        self.assertEqual(len(data), 4320)
        self.assertEqual(data[-1].time, 4999*MINUTE)
        self.assertLessEqual(data[-1].time+MINUTE, now)
        self.assertEqual(len(calls), 6)

    def test_failures_cached_without_demo_fallback(self):
        app.CACHE.clear()
        with patch('app.fetch_candles', side_effect=RuntimeError('network blocked')) as provider, patch('app.demo_candles') as demo:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'network blocked'): app.report('live', .8)
            provider.assert_called_once()
            demo.assert_not_called()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def get(self, path): return urlopen(self.url+path, timeout=10)

    def test_page_and_assets(self):
        for path, marker in [('/', '十分钟观察台'), ('/app.js', 'function render'), ('/alerts.js', 'maybeNotify'), ('/style.css', ':root')]:
            with self.get(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(marker, response.read().decode())

    def test_demo_labelled_no_action(self):
        with self.get('/api/signals?mode=demo&payout=0.8') as response: data = json.load(response)
        self.assertTrue(data['demo'])
        self.assertEqual(data['action'], 'WAIT')
        self.assertEqual(len(data['chart']), 180)
        self.assertGreater(data['holdout']['total'], 0)

    def test_default_provider_is_coinbase_and_quote_is_usd(self):
        app.CACHE.clear()
        data, now = market.demo_candles()
        with patch('app.fetch_coinbase', return_value=(data, now)) as coinbase, patch('app.fetch_candles') as binance:
            with self.get('/api/signals') as response: result = json.load(response)
        coinbase.assert_called_once()
        binance.assert_not_called()
        self.assertEqual(result['source'], 'Coinbase 现货')
        self.assertEqual(result['pair'], 'BTC / USD')
        self.assertEqual(result['quote'], 'USD')
        self.assertFalse(result['demo'])

    def test_bad_inputs_and_network_error(self):
        for query in ['payout=NaN', 'payout=-1', 'mode=unknown']:
            with self.assertRaises(HTTPError) as error: self.get('/api/signals?'+query)
            self.assertEqual(error.exception.code, 400)
        app.CACHE.clear()
        with patch('app.fetch_candles', side_effect=RuntimeError('network blocked')):
            with self.assertRaises(HTTPError) as error: self.get('/api/signals?mode=live')
            self.assertEqual(error.exception.code, 503)
            self.assertIn('network blocked', error.exception.read().decode())

    def test_unexpected_error_returns_json_instead_of_disconnecting(self):
        with patch('app.report',side_effect=ArithmeticError('unexpected failure')), patch('app.traceback.print_exc') as logged:
            with self.assertRaises(HTTPError) as error: self.get('/api/signals?mode=demo')
            self.assertEqual(error.exception.code,500)
            self.assertIn('本机程序处理请求失败',json.load(error.exception)['error'])
            logged.assert_called_once()


if __name__ == '__main__': unittest.main()
