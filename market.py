"""Independent public spot feeds; never substitute one exchange for another."""
from datetime import datetime, timezone
import json
import math
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from strategy import Candle, MINUTE, validate

BASE = 'https://api.binance.com'
COINBASE = 'https://api.exchange.coinbase.com'


def request(path, base=BASE, name='币安'):
    try:
        with urlopen(Request(base+path, headers={'User-Agent': 'TenMinuteSignals/1.1'}), timeout=15) as response:
            return json.load(response)
    except HTTPError as e:
        if e.code in (418, 429):
            raise RuntimeError(f"{name}请求频率受限，请稍后再试") from e
        raise RuntimeError(f"{name}行情连接失败（HTTP {e.code}）。请检查网络、地区支持及服务状态。") from e
    except (URLError, TimeoutError, OSError) as e:
        import ssl
        reason = getattr(e, 'reason', e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise RuntimeError(f"{name}证书验证失败。请运行 Python 文件夹中的 Install Certificates.command 后重启程序。") from e
        raise RuntimeError(f"无法连接{name}行情（{type(reason).__name__}），请检查网络是否允许 {base.split('//')[1]} 的 HTTPS 请求") from e


def fetch_coinbase(previous=None):
    """BTC-USD, 300 buckets/page, sorted and strictly checked; no gap filling."""
    exchange_now = int(float(request('/time', COINBASE, 'Coinbase')['epoch'])*1000)
    closed_end = exchange_now//MINUTE*MINUTE
    oldest = closed_end-4320*MINUTE
    existing = {c.time: c for c in (previous or []) if oldest <= c.time < closed_end}
    # Refresh the last known closed candle and append new ones when possible.
    incremental = bool(existing) and len(existing) >= 4000 and max(existing) >= closed_end-300*MINUTE
    begin = max(existing) if incremental else oldest
    combined = existing if incremental else {}
    end = closed_end
    first = True
    while end > begin:
        if not first:
            time.sleep(0.35)  # Public endpoint pacing, below 3 requests/second.
        first = False
        start = max(begin, end-300*MINUTE)
        iso = lambda ms: datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()
        query = urlencode(dict(granularity=60, start=iso(start), end=iso(end-1)))
        batch = request('/products/BTC-USD/candles?'+query, COINBASE, 'Coinbase')
        if not isinstance(batch, list) or not batch:
            raise RuntimeError('Coinbase 返回的行情不足，未生成观察信号')
        # Coinbase format: [seconds, low, high, open, close, volume].
        page = {}
        for row in batch:
            timestamp = int(row[0])*1000
            if start <= timestamp < end:
                c = Candle(timestamp, float(row[3]), float(row[2]), float(row[1]), float(row[4]), float(row[5]))
                if timestamp in page and page[timestamp] != c:
                    raise RuntimeError('Coinbase 返回冲突的重复 K 线')
                page[timestamp] = c
        if len(page) != (end-start)//MINUTE:
            raise RuntimeError('Coinbase 行情存在缺失，暂停分析；不会补造价格或换用币安数据')
        combined.update(page)
        end = start
    candles = [combined[t] for t in sorted(combined) if oldest <= t < closed_end]
    if len(candles) != 4320:
        raise RuntimeError('Coinbase 连续行情不足三天，暂停分析')
    validate(candles)
    return candles, exchange_now


def fetch_candles():
    exchange_now = int(request('/api/v3/time')['serverTime'])
    end = exchange_now//MINUTE*MINUTE-1
    rows = []
    # 3 days, paginated backwards; discard the current unclosed candle.
    remaining = 4320
    while remaining:
        batch = request(f'/api/v3/klines?symbol=BTCUSDT&interval=1m&limit={min(1000,remaining)}&endTime={end}')
        if not isinstance(batch, list) or not batch:
            raise RuntimeError("币安返回的行情不足，未生成信号")
        rows = batch+rows
        remaining -= len(batch)
        end = int(batch[0][0])-1
        if remaining < 0:
            raise RuntimeError("币安返回异常数量的 K 线")
    candles = [Candle(int(r[0]), *[float(x) for x in r[1:6]]) for r in rows]
    validate(candles)
    return candles, exchange_now


def demo_candles(count=4320):
    rng = random.Random(42)
    end = int(time.time()*1000)//MINUTE*MINUTE
    price = 70000.0
    candles = []
    for i in range(count):
        opening = price
        price *= 1 + 0.00025*math.sin(i/100)+rng.gauss(0, 0.00065)
        spread = price*rng.uniform(0.00015, 0.0006)
        candles.append(Candle(end-(count-i)*MINUTE, opening, max(price,opening)+spread,
                              min(price,opening)-spread, price, rng.uniform(5, 40)))
    return candles, int(time.time()*1000)
