#!/usr/bin/env python3
"""Run: python3 app.py --port 8000 (Python 3.10+, standard library only)."""
import argparse
import json
import math
from pathlib import Path
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from market import fetch_candles, fetch_coinbase, demo_candles
from strategy import analyze

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
        if url.path == '/health':
            return self.send(200, b'{"status":"ok"}')
        if url.path == '/api/signals':
            query = parse_qs(url.query)
            try:
                payout = float(query.get('payout', ['0.8'])[0])
                mode = query.get('mode', ['coinbase'])[0]
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
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
        if url.path not in names:
            return self.send(404, b'{"error":"Not found"}')
        name, mime = names[url.path]
        self.send(200, (ROOT/name).read_bytes(), mime)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8000, type=int)
    args = parser.parse_args()
    print(f'行情提示工具已启动，端口 {args.port}；仅提示，不下单。', flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
