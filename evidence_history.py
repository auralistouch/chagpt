"""Bounded durable public index history; never fill missing price observations."""
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading

from strategy import Candle, MINUTE

SOURCE = 'binance_usdm_btcusdt_index_1m'
FORMAT_VERSION = 1
DAY_FILE = re.compile(r'\d{4}-\d{2}-\d{2}\.json\Z')


def utc_day(stamp):
    return datetime.fromtimestamp(stamp/1000, timezone.utc).date().isoformat()


def checked_candle(values):
    if not isinstance(values, (list, tuple)) or len(values) != 5:
        raise ValueError('指数记录必须包含时间和OHLC五项')
    stamp, *prices = values
    if type(stamp) is not int or stamp < 0 or stamp % MINUTE:
        raise ValueError('指数时间必须是对齐分钟的毫秒整数')
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0 for value in prices):
        raise ValueError('指数OHLC包含无效价格')
    opening, high, low, close = prices
    if low > min(opening, close) or high < max(opening, close) or low > high:
        raise ValueError('指数OHLC高低关系无效')
    utc_day(stamp)  # Also reject timestamps outside the supported calendar.
    return Candle(stamp, opening, high, low, close, 0.0)


class IndexEvidenceHistory:
    def __init__(self, directory, max_minutes=30*1440):
        if type(max_minutes) is not int or max_minutes < 1:
            raise ValueError('指数历史容量必须为正整数分钟')
        self.directory = Path(directory)
        self.max_minutes = max_minutes
        self.lock = threading.RLock()
        self._cache = {}

    def _read_day(self, path, day):
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._cache.get(day)
            if cached is not None and cached[0] == signature:
                return cached[1]
            document = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(document, dict) or document.get('source') != SOURCE
                    or type(document.get('format_version')) is not int
                    or document['format_version'] != FORMAT_VERSION
                    or document.get('utc_day') != day or document.get('interval_ms') != MINUTE):
                raise ValueError('指数历史来源、版本或日期标记无效')
            rows = document.get('candles')
            if not isinstance(rows, list) or not 1 <= len(rows) <= 1440:
                raise ValueError('指数日历史条数无效')
            records = {}
            previous = -1
            for values in rows:
                candle = checked_candle(values)
                if candle.time <= previous or utc_day(candle.time) != day:
                    raise ValueError('指数日历史存在乱序、重复或跨日时间')
                records[candle.time] = candle
                previous = candle.time
            self._cache[day] = (signature, records)
            return records
        except (OSError, ValueError, TypeError, OverflowError, KeyError) as error:
            raise RuntimeError(f'本机指数研究历史 {path.name} 无法校验；请保留该文件检查，程序未将它当作有效行情') from error

    def _write_day(self, day, records):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        document = dict(source=SOURCE, format_version=FORMAT_VERSION,
                        utc_day=day, interval_ms=MINUTE,
                        candles=[[c.time, c.open, c.high, c.low, c.close]
                                 for c in sorted(records.values(), key=lambda c: c.time)])
        fd, filename = tempfile.mkstemp(prefix='.index-day-', suffix='.tmp', dir=self.directory)
        path = self.directory/(day+'.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, path)
            # Directory fsync is unavailable on Windows and some POSIX file
            # systems. File fsync and atomic replace remain mandatory.
            if os.name != 'nt':
                directory_fd = None
                try:
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    os.fsync(directory_fd)
                except OSError as error:
                    if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                        raise
                finally:
                    if directory_fd is not None:
                        os.close(directory_fd)
            stat = path.stat()
            self._cache[day] = ((stat.st_mtime_ns, stat.st_size), records)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def seed(self):
        """Read newest valid contiguous persisted tail, including partial startup pages.

        It is only a fetch seed. A live call must still verify a full current
        window and obtain a fresh index quote before any direction is possible.
        """
        with self.lock:
            if not self.directory.exists():return []
            paths=sorted(self.directory.glob('*.json'))
            for path in paths:
                if not DAY_FILE.fullmatch(path.name):raise RuntimeError('本机指数研究历史文件名无效')
            records=[]
            for path in paths[-32:]:records.extend(self._read_day(path,path.stem).values())
            if not records:return []
            records.sort(key=lambda candle:candle.time)
            cutoff=records[-1].time-(self.max_minutes-1)*MINUTE
            records=[c for c in records if c.time>=cutoff]
            start=0
            for i in range(1,len(records)):
                if records[i].time-records[i-1].time!=MINUTE:start=i
            return records[start:]

    def merge(self, candles):
        """Return (latest contiguous candles, diagnostics), bounded to 30 days.

        Each UTC file is independently validated and atomically replaced. Old
        files are preserved; only the bounded current window is loaded. An
        incomplete temporary file is never considered a committed day file.
        """
        incoming = []
        previous = -1
        for candle in candles:
            checked = checked_candle([candle.time, candle.open, candle.high, candle.low, candle.close])
            if checked.time <= previous:
                raise ValueError('新增指数历史必须按分钟严格递增且无重复')
            incoming.append(checked)
            previous = checked.time
        if not incoming:
            raise ValueError('新增指数历史不能为空')
        end = incoming[-1].time
        cutoff = max(0, end-(self.max_minutes-1)*MINUTE)
        first_day, last_day = utc_day(cutoff), utc_day(end)
        with self.lock:
            days = {}
            if self.directory.exists():
                for path in self.directory.glob('*.json'):
                    if not DAY_FILE.fullmatch(path.name):
                        raise RuntimeError(f'本机指数研究历史文件名 {path.name} 无效')
                    day = path.stem
                    if first_day <= day <= last_day:
                        # A malformed calendar date also fails file validation.
                        days[day] = self._read_day(path, day)
            changed = {}
            new_minutes = revised_minutes = 0
            for candle in incoming:
                if candle.time < cutoff:
                    continue
                day = utc_day(candle.time)
                records = changed.get(day, days.get(day, {}))
                existing = records.get(candle.time)
                if existing == candle:
                    continue
                if day not in changed:
                    changed[day] = dict(records)
                changed[day][candle.time] = candle
                if existing is None:
                    new_minutes += 1
                else:
                    revised_minutes += 1
            for day, records in sorted(changed.items()):
                self._write_day(day, records)
                days[day] = records
            self._cache = {day: value for day, value in self._cache.items() if first_day <= day <= last_day}
            available = sorted((candle for records in days.values() for candle in records.values()
                                if cutoff <= candle.time <= end), key=lambda candle: candle.time)
            gaps = []
            tail_start = 0
            for i in range(1, len(available)):
                distance = available[i].time-available[i-1].time
                if distance != MINUTE:
                    gaps.append(dict(after_ms=available[i-1].time, before_ms=available[i].time,
                                     missing_minutes=distance//MINUTE-1))
                    tail_start = i
            continuous = available[tail_start:]
            diagnostic = dict(source=SOURCE, format_version=FORMAT_VERSION,
                              capacity_minutes=self.max_minutes, available_minutes=len(available),
                              retained_minutes=len(continuous), discarded_before_latest_gap=tail_start,
                              has_gap=bool(gaps), gap_count=len(gaps),
                              missing_minutes=sum(gap['missing_minutes'] for gap in gaps),
                              latest_gap=gaps[-1] if gaps else None,
                              from_time=continuous[0].time, to_time=continuous[-1].time,
                              new_minutes=new_minutes, revised_minutes=revised_minutes,
                              written_days=len(changed), filled_prices=False)
            return continuous, diagnostic
