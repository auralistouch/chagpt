"""Public index prices and explicit spot volume; never silently substitute feeds."""
from datetime import datetime, timezone
from http.client import HTTPException
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
FUTURES = 'https://fapi.binance.com'


class MarketError(RuntimeError):
    def __init__(self, message, code, provider, http_status=None):
        super().__init__(message)
        self.diagnostic = dict(code=code, provider=provider, http_status=http_status)


def fetch_index_candles(previous=None, count=4320):
    """Index OHLC joined to explicit spot volume; index field 5 is NOT volume.

    Fail closed on either source, timestamp mismatch, gaps or duplicate rows.
    Never reuse spot OHLC as index prices.
    """
    clock = int(request('/fapi/v1/time', FUTURES, '币安指数')['serverTime'])
    end = clock//MINUTE*MINUTE
    if type(count) is not int or not 71 <= count <= 129600:
        raise ValueError('指数窗口须在71根至90天一分钟K线之间')
    oldest = end-count*MINUTE
    existing = {c.time: c for c in (previous or []) if oldest <= c.time < end}
    incremental = len(existing) >= count-320 and bool(existing) and max(existing) >= end-999*MINUTE
    begin = max(existing) if incremental else oldest
    combined = existing if incremental else {}
    cursor = begin
    while cursor < end:
        page_end = min(end, cursor+1000*MINUTE)
        query = urlencode(dict(pair='BTCUSDT', interval='1m', limit=1000,
                               startTime=cursor, endTime=page_end-1))
        rows = request('/fapi/v1/indexPriceKlines?'+query, FUTURES, '币安指数')
        volume_query = urlencode(dict(symbol='BTCUSDT', interval='1m', limit=1000,
                                      startTime=cursor, endTime=page_end-1))
        volumes = request('/api/v3/klines?'+volume_query)
        expected = set(range(cursor, page_end, MINUTE))
        def page(items):
            if not isinstance(items, list): raise RuntimeError('行情响应不是 K 线列表')
            result = {}
            for row in items:
                stamp = int(row[0])
                if stamp not in expected or stamp in result:
                    raise RuntimeError('指数/量能行情含重复或越界 K 线')
                result[stamp] = row
            if set(result) != expected:
                raise RuntimeError('指数与现货量能行情缺失或时间未对齐，暂停提示')
            return result
        prices, spot = page(rows), page(volumes)
        for stamp in sorted(expected):
            row = prices[stamp]
            combined[stamp] = Candle(stamp, *map(float, row[1:5]), float(spot[stamp][5]))
        cursor = page_end
    candles = [combined[t] for t in sorted(combined)]
    if len(candles) != count: raise RuntimeError('指数连续行情不足请求窗口，暂停提示')
    validate(candles)
    return candles, clock


def fetch_index_quote():
    data = request('/fapi/v1/premiumIndex?symbol=BTCUSDT', FUTURES, '币安指数')
    if not isinstance(data, dict) or data.get('symbol') != 'BTCUSDT':
        raise RuntimeError('指数快照品种不匹配')
    price, stamp = float(data['indexPrice']), int(data['time'])
    if not math.isfinite(price) or price <= 0 or stamp <= 0:
        raise RuntimeError('指数快照无效')
    # Deliberately never read markPrice, estimatedSettlePrice or lastPrice.
    return dict(price=price, time=stamp)


def request(path, base=BASE, name='币安'):
    try:
        with urlopen(Request(base+path, headers={'User-Agent': 'TenMinuteSignals/1.1'}), timeout=15) as response:
            return json.load(response)
    except HTTPError as e:
        if e.code in (418, 429):
            raise MarketError(f"{name}请求频率受限，请稍后再试", 'rate_limited', name, e.code) from e
        raise MarketError(f"{name}行情连接失败（HTTP {e.code}）。请检查网络、地区支持及服务状态。", 'http_error', name, e.code) from e
    except (URLError, TimeoutError, OSError) as e:
        import ssl
        reason = getattr(e, 'reason', e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise MarketError(f"{name}证书验证失败。请运行 Python 文件夹中的 Install Certificates.command 后重启程序。", 'tls_error', name) from e
        raise MarketError(f"无法连接{name}行情（{type(reason).__name__}），请检查网络是否允许 {base.split('//')[1]} 的 HTTPS 请求", 'network_error', name) from e
    except HTTPException as e:
        raise MarketError(f'{name}行情响应中途断开或不完整，请稍后重试', 'incomplete_response', name) from e
    except json.JSONDecodeError as e:
        raise MarketError(f'{name}行情响应不是有效JSON，暂停提示', 'invalid_json', name) from e


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


def fetch_candles(previous=None):
    exchange_now = int(request('/api/v3/time')['serverTime'])
    end = exchange_now//MINUTE*MINUTE-1
    oldest = end+1-4320*MINUTE
    existing = {c.time: c for c in (previous or []) if oldest <= c.time <= end}
    if len(existing) >= 4000 and max(existing) >= end+1-999*MINUTE:
        start = max(existing)
        batch = request(f'/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000&startTime={start}&endTime={end}')
        if not isinstance(batch, list) or not batch:
            raise RuntimeError('币安返回的最新行情不足，暂停提示')
        updated = {int(r[0]): Candle(int(r[0]), *[float(x) for x in r[1:6]]) for r in batch}
        updated = {t: c for t, c in updated.items() if start <= t <= end}
        if len(updated) != (end+1-start)//MINUTE:
            raise RuntimeError('币安最新行情存在缺失，暂停提示')
        existing.update(updated)
        candles = [existing[t] for t in sorted(existing)]
        if len(candles) != 4320:
            raise RuntimeError('币安连续行情不足三天，暂停提示')
        validate(candles)
        return candles, exchange_now
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
