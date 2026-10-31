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
from journal import Journal
from strategy import NOTIFY_COOLDOWN_MS

BARK_ENDPOINT = 'https://api.day.app/push'


class BarkRejected(RuntimeError):
    pass


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
            raise BarkRejected('推送服务未接受请求，请检查 Bark 推送地址和服务状态')
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
        self.journal = Journal(self.path.parent/'logs')
        self.check_count = 0
        self.block_counts = {}
        self.last_diagnostics = None
        self.last_push_result = None
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
                self.state.update(saved)  # Preserve existing and future configuration fields.
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
                        check_count=self.check_count, block_counts=dict(self.block_counts),
                        last_diagnostics=self.last_diagnostics, last_push_result=self.last_push_result,
                        recent_events=self.journal.tail(),
                        daily_utc=self.journal.daily(),
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
        self._record('push_attempt', kind='test')
        try:
            self.sender(key, '测试提醒 · 不是交易信号', 'Mac 已请求发送 iPhone 测试通知。没有产生买点，请确认手机是否实际收到。')
        except Exception as error:
            self._push_result('rejected' if isinstance(error, BarkRejected) else 'unknown', 'test')
            raise RuntimeError('测试推送未确认；请查看手机实际结果，测试不会消耗正式信号冷却期') from None
        self._push_result('accepted', 'test')
        return dict(message='推送服务已接受测试请求，请以 iPhone 实际收到为准；此测试不是买点')

    def _record(self, event, **fields):
        # Only caller-selected diagnostic fields enter the journal. Mask the
        # configured credential even if a future diagnostic embeds it by mistake.
        with self.lock: key = self.state['key']
        def clean(value):
            if isinstance(value, str): return value.replace(key, '[redacted]') if key else value
            if isinstance(value, dict): return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list): return [clean(v) for v in value]
            if isinstance(value, float) and not math.isfinite(value): return None
            return value
        return self.journal.record(event, **clean(fields))

    def _push_result(self, result, kind, signal_time=None):
        with self.lock:
            self.last_push_result = dict(result=result, kind=kind, time=int(time.time()*1000))
        self._record('push_result', kind=kind, result=result, signal_time=signal_time)

    def _blocked(self, codes, reasons):
        for code in codes: self.block_counts[code] = self.block_counts.get(code, 0)+1
        self.message = '监测中：'+('；'.join(reasons) if reasons else '暂无符合条件的观察方向')
        self._record('check_blocked', blocker_codes=codes, blockers=reasons)

    def disable(self):
        with self.lock:
            return self.configure(dict(enabled=False, mode=self.state['mode'], payout=self.state['payout']))

    def check_once(self):
        with self.lock:
            settings = self.state.copy()
            revision = self.revision
        if not settings['enabled'] or not settings['key']: return
        phase = 'report'
        check_started = int(time.time()*1000)
        with self.lock:
            self.last_check = check_started
            self.check_count += 1
        try:
            self._record('check_started', mode=settings['mode'], payout=settings['payout'], check_number=self.check_count)
            data = self.reporter(settings['mode'], settings['payout'])
            report_finished = time.monotonic()
            stamp = data.get('signal_time')
            age = data.get('data_age_seconds')
            quote_age = data.get('quote_age_seconds')
            valid = (not data.get('demo') and data.get('action') in ('UP', 'DOWN')
                     and data.get('price_kind') == 'index' and settings['mode'] == 'live'
                     and settings['payout'] == .8
                     and isinstance(quote_age, (int, float)) and math.isfinite(quote_age) and 0 <= quote_age < 15
                     and isinstance(stamp, (int, float)) and math.isfinite(stamp)
                     and isinstance(age, (int, float)) and math.isfinite(age) and 0 <= age < 75)
            with self.lock:
                if revision != self.revision or self.stop_event.is_set(): return
                self.last_check = int(time.time()*1000)
                self.last_diagnostics = {k: data.get(k) for k in (
                    'source', 'volume_source', 'action', 'signal_time', 'observation_time',
                    'data_age_seconds', 'quote_age_seconds', 'price_kind', 'metrics', 'diagnostics',
                    'blocker_codes', 'blockers')}
                self._record('check_result', duration_ms=self.last_check-check_started,
                             **self.last_diagnostics, holdout={k: data.get('holdout', {}).get(k) for k in (
                                 'total', 'wins', 'losses', 'unresolved', 'lower_bound', 'profit_units', 'exact', 'by_direction')})
                if not valid:
                    codes = list(data.get('blocker_codes', []))
                    reasons = list(data.get('blockers', []))
                    if data.get('price_kind') != 'index':
                        codes.append('phone_not_index'); reasons.append('手机合约提醒需要币安指数来源')
                    if not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age < 75:
                        codes.append('phone_stale'); reasons.append('手机要求指标行情小于75秒')
                    if not isinstance(quote_age, (int, float)) or not math.isfinite(quote_age) or not 0 <= quote_age < 15:
                        codes.append('phone_stale_index'); reasons.append('手机要求指数快照小于15秒')
                    self._blocked(codes or ['invalid_signal'], reasons or ['没有有效观察方向'])
                    return
                if stamp-self.state['last_signal_time'] < NOTIFY_COOLDOWN_MS:
                    self._blocked(['cooldown'], ['同一或相隔不足601秒的观察信号不重复提醒'])
                    return
                # Persist before sending: a timeout may mean the service accepted
                # the notification. Do not automatically resend the same signal.
                candidate = {**self.state, 'last_signal_time': stamp}
                phase = 'save'
                self._save(candidate)
                self.state = candidate
                direction = '看涨' if data['action'] == 'UP' else '看跌'
                baseline = datetime.fromtimestamp(stamp/1000).astimezone().isoformat(timespec='seconds')
                body = f"{data.get('source', '')} · {data.get('pair', '')}\n指数快照 {data.get('reference_price')}\n指标基准 {baseline}\n官方规则：提交后下一秒取开仓指数，再过600秒结算；平价退本金。验证假设赢+80%/输-100%，实际赔率以确认订单为准。此快照不是实际开仓价；仅供观察，不是下单指令。"
            # Network I/O must not block saving a stop request or reading status.
            with self.lock:
                if revision != self.revision or self.stop_event.is_set(): return
                elapsed = time.monotonic()-report_finished
                if age+elapsed >= 75 or quote_age+elapsed >= 15:
                    self._blocked(['stale_before_push'], ['本机处理后行情已过期，取消推送'])
                    return
            phase = 'push'
            self._record('push_attempt', kind='signal', signal_time=stamp)
            self.sender(settings['key'], f'BTC 观察到{direction}方向', body)
            self._push_result('accepted', 'signal', stamp)
            with self.lock:
                if revision != self.revision: return
                self.last_push = int(time.time()*1000)
                self.message = '推送服务已接受观察提醒，手机是否展示请以实际情况为准'
        except Exception as error:
            with self.lock:
                if revision == self.revision:
                    self.message = {'report': '行情检查失败', 'save': '本机去重状态保存失败', 'push': '推送未确认，可能已接受，同一信号不自动重发'}[phase]+'；稍后继续检查。'
                    try:
                        if phase == 'push': self._push_result('rejected' if isinstance(error, BarkRejected) else 'unknown', 'signal', stamp)
                        self._record('check_error', phase=phase, error_type=type(error).__name__,
                                     provider_error=getattr(error, 'diagnostic', None),
                                     message=self.message, duration_ms=int(time.time()*1000)-check_started)
                    except (OSError, ValueError):
                        self.message += '诊断日志写入失败，请检查磁盘空间和目录权限。'

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
