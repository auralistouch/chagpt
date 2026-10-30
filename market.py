"""Binance public spot market data only; no trading or API keys."""
import json
import math
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from strategy import Candle, MINUTE, validate

BASE = 'https://api.binance.com'


def request(path):
    try:
        with urlopen(Request(BASE+path, headers={'User-Agent': 'TenMinuteSignals/1.0'}), timeout=15) as response:
            return json.load(response)
    except HTTPError as e:
        if e.code in (418, 429):
            raise RuntimeError("币安请求频率受限，请稍后再试") from e
        raise RuntimeError(f"行情连接失败（HTTP {e.code}）。请检查网络放行、地区限制或服务状态；不绕过地区限制。") from e
    except (URLError, TimeoutError, OSError) as e:
        raise RuntimeError("无法连接币安行情，请检查网络是否允许 api.binance.com 的 HTTPS 请求") from e


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
