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
from market import fetch_candles, fetch_coinbase, demo_candles
from strategy import analyze
from phone import PhoneMonitor

ROOT = Path(__file__).resolve().parent/'web'
CACHE = {}
LOCK = threading.Lock()


def report(mode, payout):
    with LOCK:
        now = time.monotonic()
        saved = CACHE.get(mode)
        if not saved or now-saved['fetched'] >= 30:
            try:
                started = time.monotonic()
                if mode == 'coinbase':
                    candles, exchange_now = fetch_coinbase(saved.get('candles') if saved else None)
                elif mode == 'live':
                    candles, exchange_now = fetch_candles(saved.get('candles') if saved else None)
                else:
                    candles, exchange_now = demo_candles()
                finished = time.monotonic()
                # Include request time in candle age; slow downloads must not
                # make old market data look freshly observed.
                saved = dict(candles=candles, clock=exchange_now+int((finished-started)*1000), fetched=finished)
                CACHE[mode] = saved
            except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as e:
                # Cache failures too, so browser refreshes cannot hammer the provider.
                previous = saved.get('candles') if saved else None
                saved = dict(error=str(e), fetched=time.monotonic(), candles=previous)
                CACHE[mode] = saved
        if 'error' in saved:
            raise RuntimeError(saved['error'])
        clock = saved['clock']+int((time.monotonic()-saved['fetched'])*1000)
        result = analyze(saved['candles'], clock, payout, mode == 'demo')
        result['source'] = {'coinbase': 'Coinbase 现货', 'live': '币安现货', 'demo': '合成演示'}[mode]
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
                result = report(mode, payout)
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
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
        if url.path not in names:
            return self.send(404, b'{"error":"Not found"}')
        name, mime = names[url.path]
        self.send(200, (ROOT/name).read_bytes(), mime)

    def do_POST(self):
        if self.path not in ('/api/phone', '/api/phone/test', '/api/phone/stop'):
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
            if not 0 < length <= 4096: raise ValueError('手机设置请求过大或为空')
            values = json.loads(self.rfile.read(length))
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
    server.phone_monitor = PhoneMonitor(config_dir/'phone.json', report)
    server.phone_monitor.start()
    try:
        server.serve_forever()
    finally:
        server.phone_monitor.stop()
        server.server_close()
