"""Latest-only production phone monitor; legacy monitors remain research helpers."""
from journal import Journal
from phone import PhoneMonitor, send_bark

POLICY = 'latest_v4'
VERSION = 'reversion_v4.0'


class LatestPhoneMonitor(PhoneMonitor):
    def __init__(self, path, reporter, sender=send_bark, interval=30):
        self._latest_source_reporter = reporter
        self._latest_validation = None
        super().__init__(path, self._latest_report, sender, interval)
        # A new journal keeps old strategies' events and counters out of the
        # latest screen without removing any historical files.
        self.journal = Journal(self.path.parent/'logs'/'latest_v4')
        candidate = {**self.state, 'policy': POLICY}
        unreadable = self.message.startswith('本机手机配置无法读取')
        if unreadable:
            self.state = {**candidate, 'enabled': False}
            return
        if self.path.exists() and self.state['policy'] != POLICY:
            try:
                self._save(candidate)
            except (OSError, ValueError, TypeError):
                self.state = {**candidate, 'enabled': False}
                self.message = '最新版设置迁移未保存，手机监测已停用；请检查目录权限后重新保存设置'
                return
            self.message = '已保留 Bark 设置并切换最新版，等待检查' if candidate['enabled'] else '已保留 Bark 设置并切换最新版；手机提醒已停用'
        self.state = candidate

    def _latest_report(self, mode, payout, policy=POLICY):
        # Ignore any caller policy: the production worker can never call an
        # older strategy, including when an old configuration is on disk.
        data = self._latest_source_reporter(mode, payout, POLICY)
        if not isinstance(data, dict):
            raise ValueError('最新版行情报告格式无效')
        result = dict(data)
        validation = data.get('research_validation')
        with self.lock:
            self._latest_validation = dict(validation) if isinstance(validation, dict) else None
        codes = list(data.get('blocker_codes') or [])
        reasons = list(data.get('blockers') or [])
        if data.get('policy') != POLICY:
            codes.append('latest_policy_mismatch')
            reasons.append('返回的策略不是最新版，已取消正式推送；请重启最新版程序')
        if data.get('strategy_version') != VERSION:
            codes.append('latest_version_mismatch')
            reasons.append('策略版本校验未通过，已取消正式推送')
        if not isinstance(validation, dict) or validation.get('passed') is not True:
            codes.append('latest_validation_required')
            reasons.append('最新版尚未通过预先设定的65%研究门槛，暂不发送正式买点')
        if codes:
            result['action'] = 'WAIT'
        result.update(policy=POLICY, strategy_version=VERSION,
                      blocker_codes=codes, blockers=reasons)
        return result

    def configure(self, values):
        if not isinstance(values, dict) or values.get('policy') != POLICY:
            raise ValueError('手机监测仅支持最新版策略，请刷新网页后重新保存')
        return super().configure(values)

    def disable(self):
        with self.lock:
            return self.configure(dict(enabled=False, mode=self.state['mode'],
                                       payout=self.state['payout'], policy=POLICY))

    def _record(self, event, **fields):
        # Caller-supplied metadata must not overwrite the production version.
        fields.update(policy=POLICY, strategy_version=VERSION)
        if event == 'check_result':
            with self.lock:
                fields['research_validation'] = self._latest_validation
        return super()._record(event, **fields)

    def status(self):
        result = super().status()
        with self.lock:
            validation = dict(self._latest_validation) if self._latest_validation is not None else None
        if result['last_diagnostics'] is not None:
            result['last_diagnostics'] = {**result['last_diagnostics'], 'research_validation': validation}
        result.update(policy=POLICY, strategy_version=VERSION, research_validation=validation)
        return result
