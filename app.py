#!/usr/bin/env python3
"""Run: python3 app.py --port 8000 (Python 3.10+, standard library only)."""
import argparse
import json
import math
import os
from pathlib import Path
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from market import fetch_candles, fetch_coinbase, demo_candles, fetch_index_candles, fetch_index_quote
from strategy import analyze
from trend_strategy import analyze_trend
from pullback_strategy import analyze_pullback, pullback_features
from point_research import point_backtest
from trend_strategy import trend_features
from latest_strategy import analyze_latest
from latest_phone import LatestPhoneMonitor
from evidence_history import IndexEvidenceHistory
from order_review import review_csv

ROOT = Path(__file__).resolve().parent/'web'
CACHE = {}
LOCK = threading.Lock()
EVIDENCE_HISTORY = None


def report(mode, payout, policy='strict'):
    with LOCK:
        now = time.monotonic()
        index_only = policy in ('trend_v2', 'pullback_v3', 'latest_v4')
        cache_key = (mode, 'index_only') if index_only else mode
        saved = CACHE.get(cache_key)
        if not saved or now-saved['fetched'] >= 30:
            try:
                started = time.monotonic()
                if mode == 'coinbase':
                    candles, exchange_now = fetch_coinbase(saved.get('candles') if saved else None)
                elif mode == 'live':
                    previous = saved.get('candles') if saved else None
                    candles, exchange_now = (fetch_index_candles(previous, include_spot_volume=False)
                                             if index_only else fetch_index_candles(previous))
                else:
                    candles, exchange_now = demo_candles()
                finished = time.monotonic()
                # Include request time in candle age; slow downloads must not
                # make old market data look freshly observed.
                saved = dict(candles=candles, clock=exchange_now+int((finished-started)*1000), fetched=finished)
                CACHE[cache_key] = saved
            except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as e:
                # Cache failures too, so browser refreshes cannot hammer the provider.
                previous = saved.get('candles') if saved else None
                saved = dict(error=str(e), exception=e, fetched=time.monotonic(), candles=previous)
                CACHE[cache_key] = saved
        if 'error' in saved:
            raise saved['exception']
        analysis_candles = saved['candles']
        history_diagnostic = None
        if policy == 'latest_v4' and mode == 'live' and EVIDENCE_HISTORY is not None:
            analysis_candles, history_diagnostic = EVIDENCE_HISTORY.merge(analysis_candles)
        # Fetch the snapshot after any durable-history work so a slow disk
        # cannot make an already-fetched quote look current.
        # Fetch an independent current index snapshot on every check, even when
        # the much larger closed-candle history is still cached.
        quote = fetch_index_quote() if mode == 'live' else None
        clock = saved['clock']+int((time.monotonic()-saved['fetched'])*1000)
        analyzer = {'trend_v2': analyze_trend, 'pullback_v3': analyze_pullback,
                    'latest_v4': analyze_latest}.get(policy, analyze)
        options = {} if index_only else dict(policy=policy)
        result = analyzer(analysis_candles, clock, payout, mode == 'demo',
                          price_kind='index' if mode == 'live' else 'spot', quote=quote, **options)
        if history_diagnostic is not None:
            result['evidence_history'] = history_diagnostic
            if history_diagnostic['has_gap']:
                result['warnings'] = [*result.get('warnings', []),
                    f"指数历史存在{history_diagnostic['gap_count']}处缺口，仅用最新连续尾段{history_diagnostic['retained_minutes']}分钟计算，未补造价格"]
        if policy == 'trend_v2':
            feature_builder = pullback_features if policy == 'pullback_v3' else trend_features
            features = feature_builder(saved['candles'])
            split = min(len(features)-1, max(121, len(features)*2//3))
            result['reference_study'] = dict(
                early=point_backtest(saved['candles'], features, 120, split, payout),
                holdout=point_backtest(saved['candles'], features, split, len(features), payout)) if len(features)>122 else None
        result['source'] = {'coinbase': 'Coinbase 现货（研究）', 'live': '币安 BTCUSDT 指数', 'demo': '合成演示'}[mode]
        result['volume_source'] = '币安 BTCUSDT 现货成交量（仅量能指标）' if mode == 'live' else result['source']
        if policy in ('trend_v2', 'pullback_v3'): result['volume_source'] = '新策略不使用成交量，仅请求指数行情'
        if policy == 'latest_v4': result['volume_source'] = '最新版不使用成交量，仅请求指数行情'
        result['pair'] = 'BTC / USD' if mode == 'coinbase' else 'BTC / USDT'
        result['quote'] = 'USD' if mode == 'coinbase' else 'USDT'
        return result


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, mime='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The browser may have navigated away during a slow request.

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/api/phone':
            phone = getattr(self.server, 'phone_monitor', None)
            result = phone.status() if phone else dict(available=False, message='手机模块未启动，请运行新版 start.command')
            return self.send(200, json.dumps(result, ensure_ascii=False).encode())
        if url.path == '/health':
            return self.send(200, b'{"status":"ok"}')
        if url.path == '/api/signals':
            query = parse_qs(url.query)
            try:
                payout = float(query.get('payout', ['0.8'])[0])
                mode = query.get('mode', ['live'])[0]
                if mode not in ('coinbase', 'live', 'demo') or not math.isfinite(payout) or not 0.01 <= payout <= 2:
                    raise ValueError('模式或净收益比例无效')
                policy = query.get('policy', ['latest_v4'])[0]
                if policy != 'latest_v4': raise ValueError('仅支持最新版策略，请刷新网页后重试')
                result = report(mode, payout, policy)
                return self.send(200, json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
            except ValueError as e:
                return self.send(400, json.dumps({'error': str(e)}, ensure_ascii=False).encode())
            except RuntimeError as e:
                return self.send(503, json.dumps({'error': str(e)}, ensure_ascii=False).encode())
            except Exception:
                traceback.print_exc()
                return self.send(500, json.dumps({'error': '本机程序处理请求失败。请查看 start.command 终端中的错误，并将提示发给协助人员。'}, ensure_ascii=False).encode())
        names = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8'),
                 '/alerts.js': ('alerts.js', 'text/javascript; charset=utf-8'),
                 '/phone.js': ('phone.js', 'text/javascript; charset=utf-8'),
                 '/orders.js': ('orders.js', 'text/javascript; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
        if url.path not in names:
            return self.send(404, b'{"error":"Not found"}')
        name, mime = names[url.path]
        self.send(200, (ROOT/name).read_bytes(), mime)

    def do_POST(self):
        if self.path not in ('/api/phone', '/api/phone/test', '/api/phone/stop', '/api/order-review'):
            return self.send(404, b'{"error":"Not found"}')
        try:
            origin = urlsplit(self.headers.get('Origin', ''))
            host = urlsplit('http://'+self.headers.get('Host', ''))
            if (self.client_address[0] not in ('127.0.0.1', '::1')
                    or origin.scheme != 'http' or origin.hostname not in ('127.0.0.1', 'localhost')
                    or origin.netloc != host.netloc or (origin.port or 80) != self.server.server_port
                    or self.headers.get_content_type() != 'application/json'
                    or self.headers.get('Transfer-Encoding')):
                return self.send(403, b'{"error":"Local same-origin JSON requests only"}')
            length = int(self.headers.get('Content-Length', '0'))
            limit = 1_100_000 if self.path == '/api/order-review' else 4096
            if not 0 < length <= limit: raise ValueError('请求过大或为空')
            values = json.loads(self.rfile.read(length))
            if self.path == '/api/order-review':
                if not isinstance(values, dict): raise ValueError('请求须为JSON对象')
                result = review_csv(values.get('csv'))
                return self.send(200, json.dumps(result, ensure_ascii=False).encode())
            phone = getattr(self.server, 'phone_monitor', None)
            if not phone: raise RuntimeError('手机模块未启动，请重启新版程序')
            if self.path == '/api/phone/stop':
                result = phone.disable()
            elif self.path == '/api/phone/test':
                if not isinstance(values, dict): raise ValueError('请求格式不正确')
                result = phone.test()
            else:
                result = phone.configure(values)
            return self.send(200, json.dumps(result, ensure_ascii=False).encode())
        except ValueError as error:
            return self.send(400, json.dumps({'error': str(error)}, ensure_ascii=False).encode())
        except Exception:
            # Do not log configuration, device keys, or provider error bodies.
            return self.send(503, json.dumps({'error': '手机设置或推送请求未完成。请确认配置、文件写入权限及网络；以 iPhone 实际收到为准。'}, ensure_ascii=False).encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8000, type=int)
    args = parser.parse_args()
    print(f'行情提示工具已启动，端口 {args.port}；仅提示，不下单。', flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    config_dir = Path(os.environ.get('BTC_OBSERVER_CONFIG_DIR', str(Path.home()/'.config/btc-observer')))
    EVIDENCE_HISTORY = IndexEvidenceHistory(config_dir/'history'/'latest_v4')
    server.phone_monitor = LatestPhoneMonitor(config_dir/'phone.json', report)
    server.phone_monitor.start()
    try:
        server.serve_forever()
    finally:
        server.phone_monitor.stop()
        server.server_close()

