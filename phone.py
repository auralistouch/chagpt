"""Local-only Bark configuration and a browser-independent observation worker."""
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BARK_ENDPOINT = 'https://api.day.app/push'


def device_key(value):
    value = value.strip()
    if '://' in value:
        url = urlsplit(value)
        if url.scheme != 'https' or url.netloc != 'api.day.app' or url.query or url.fragment:
            raise ValueError('请填写 Bark 的 api.day.app 推送地址，或仅填写设备密钥')
        value = url.path.strip('/').split('/')[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', value):
        raise ValueError('Bark 推送地址或设备密钥格式不正确')
    return value


def send_bark(key, title, body):
    payload = json.dumps(dict(device_key=key, title=title, body=body,
                              group='BTC十分钟观察', level='active', sound='bell'), ensure_ascii=False).encode()
    request = Request(BARK_ENDPOINT, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=12) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('code') != 200:
            raise RuntimeError('推送服务未接受请求，请检查 Bark 推送地址和服务状态')
    except RuntimeError:
        raise
    except Exception:
        # Provider errors can contain credentials: never include their text.
        raise RuntimeError('无法确认手机推送是否成功，请检查网络与 Bark 设置；可能有延迟或重复，请以手机实际收到为准') from None


class PhoneMonitor:
    def __init__(self, path, reporter, sender=send_bark, interval=30):
        self.path = Path(path)
        self.reporter = reporter
        self.sender = sender
        self.interval = interval
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.revision = 0
        self.state = dict(key='', enabled=False, mode='live', payout=.8, last_signal_time=0)
        self.message = '手机提醒尚未配置'
        self.last_check = None
        self.last_push = None
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                parsed_key = device_key(saved['key']) if saved.get('key') else ''
                if saved.get('mode') not in ('live', 'coinbase') or type(saved.get('enabled')) is not bool:
                    raise ValueError()
                if not math.isfinite(saved['payout']) or not .01 <= saved['payout'] <= 2:
                    raise ValueError()
                last = saved.get('last_signal_time', 0)
                if not isinstance(last, (int, float)) or not math.isfinite(last) or last < 0:
                    raise ValueError()
                self.state.update(key=parsed_key, enabled=saved['enabled'] and bool(parsed_key),
                                  mode=saved['mode'], payout=saved['payout'], last_signal_time=last)
                self.message = '已读取本机配置，等待检查' if self.state['enabled'] else '手机提醒已停用'
            except Exception:
                self.message = '本机手机配置无法读取，已停用；请重新保存设置'

    def _save(self, state):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix='.phone-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(filename, self.path)
        finally:
            if os.path.exists(filename): os.unlink(filename)

    def status(self):
        with self.lock:
            return dict(configured=bool(self.state['key']), enabled=self.state['enabled'],
                        mode=self.state['mode'], payout=self.state['payout'],
                        message=self.message, last_check=self.last_check, last_push=self.last_push,
                        running=bool(self.thread and self.thread.is_alive()))

    def configure(self, values):
        if not isinstance(values, dict): raise ValueError('手机设置格式不正确')
        enabled = values.get('enabled', False)
        mode = values.get('mode', 'live')
        payout = values.get('payout', .8)
        if type(enabled) is not bool or mode not in ('live', 'coinbase'):
            raise ValueError('手机提醒只支持真实行情；请选择币安或 Coinbase')
        if not isinstance(payout, (int, float)) or isinstance(payout, bool) or not math.isfinite(payout) or not .01 <= payout <= 2:
            raise ValueError('模拟净收益比例必须在 1% 至 200% 之间')
        raw = values.get('key', '')
        if not isinstance(raw, str): raise ValueError('设备密钥格式不正确')
        with self.lock:
            candidate = {**self.state, 'enabled': enabled, 'mode': mode, 'payout': payout}
            if raw.strip(): candidate['key'] = device_key(raw)
            if enabled and not candidate['key']: raise ValueError('请先在本机网页填写 Bark 推送地址')
            self._save(candidate)  # Preserve old settings if saving fails.
            self.state = candidate
            self.revision += 1
            self.message = '手机监测已启用，等待下一次检查' if enabled else '手机提醒已停用'
            self.wake.set()
        return self.status()

    def test(self):
        with self.lock: key = self.state['key']
        if not key: raise ValueError('请先保存 Bark 推送地址')
        self.sender(key, '测试提醒 · 不是交易信号', 'Mac 已请求发送 iPhone 测试通知。没有产生买点，请确认手机是否实际收到。')
        return dict(message='推送服务已接受测试请求，请以 iPhone 实际收到为准；此测试不是买点')

    def disable(self):
        with self.lock:
            return self.configure(dict(enabled=False, mode=self.state['mode'], payout=self.state['payout']))

    def check_once(self):
        with self.lock:
            settings = self.state.copy()
            revision = self.revision
        if not settings['enabled'] or not settings['key']: return
        try:
            data = self.reporter(settings['mode'], settings['payout'])
            stamp = data.get('signal_time')
            age = data.get('data_age_seconds')
            valid = (not data.get('demo') and data.get('action') in ('UP', 'DOWN')
                     and isinstance(stamp, (int, float)) and math.isfinite(stamp)
                     and isinstance(age, (int, float)) and math.isfinite(age) and 0 <= age < 75)
            with self.lock:
                if revision != self.revision or self.stop_event.is_set(): return
                self.last_check = int(time.time()*1000)
                if not valid:
                    self.message = '监测中：暂无符合提醒条件的观察方向（等待、演示或行情过旧均不发送）'
                    return
                if stamp-self.state['last_signal_time'] < 600000:
                    self.message = '监测中：同一或相隔不足十分钟的观察信号不重复提醒'
                    return
                # Persist before sending: a timeout may mean the service accepted
                # the notification. Do not automatically resend the same signal.
                candidate = {**self.state, 'last_signal_time': stamp}
                self._save(candidate)
                self.state = candidate
                direction = '看涨' if data['action'] == 'UP' else '看跌'
                baseline = datetime.fromtimestamp(stamp/1000).astimezone().isoformat(timespec='seconds')
                body = f"{data.get('source', '')} · {data.get('pair', '')}\n参考价 {data.get('reference_price')}\n基准 {baseline}\n到达后请核对最新行情；仅供观察，不是下单指令。"
            # Network I/O must not block saving a stop request or reading status.
            with self.lock:
                if revision != self.revision or self.stop_event.is_set(): return
            self.sender(settings['key'], f'BTC 观察到{direction}方向', body)
            with self.lock:
                if revision != self.revision: return
                self.last_push = int(time.time()*1000)
                self.message = '推送服务已接受观察提醒，手机是否展示请以实际情况为准'
        except Exception:
            with self.lock:
                if revision == self.revision:
                    self.message = '行情、配置保存或推送检查失败；未确认送达，稍后继续检查。请检查本机网络及设置；同一信号不自动重发。'

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            self.check_once()
            self.wake.wait(self.interval)

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self._run, daemon=True, name='phone-observer')
        self.thread.start()

    def stop(self):
        self.stop_event.set(); self.wake.set()
        if self.thread: self.thread.join(timeout=2)
