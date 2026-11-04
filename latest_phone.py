"""Latest-only production phone monitor; legacy monitors remain research helpers."""
from journal import Journal
from phone import PhoneMonitor, send_bark
from latest_strategy import POLICY, VERSION
from rolling_activity import RollingActivity
import time



class LatestPhoneMonitor(PhoneMonitor):
    def __init__(self, path, reporter, sender=send_bark, interval=30):
        self._latest_source_reporter = reporter
        self._latest_quality = None
        self._activity = None
        self._activity_error = False
        super().__init__(path, self._latest_report, sender, interval)
        # A new journal keeps old strategies' events and counters out of the
        # latest screen without removing any historical files.
        self.journal = Journal(self.path.parent/'logs'/POLICY)
        try:
            self._activity = RollingActivity(self.path.parent/'activity'/POLICY,self.journal.path.parent)
        except (OSError,ValueError,TypeError):
            self._activity_error = True
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
        quality = data.get('quality_assessment')
        with self.lock:
            self._latest_quality = dict(quality) if isinstance(quality, dict) else None
        result.pop('research_validation', None)
        codes = list(data.get('blocker_codes') or [])
        reasons = list(data.get('blockers') or [])
        if data.get('policy') != POLICY:
            codes.append('latest_policy_mismatch')
            reasons.append('返回的策略不是最新版，已取消正式推送；请重启最新版程序')
        if data.get('strategy_version') != VERSION:
            codes.append('latest_version_mismatch')
            reasons.append('策略版本校验未通过，已取消正式推送')
        if data.get('research_mode') is not True:
            codes.append('latest_research_mode_required')
            reasons.append('缺少明确研究模式标记，取消方向提醒')
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
        fields.update(policy=POLICY, strategy_version=VERSION,research_mode=True)
        fields.pop('research_validation', None)
        if event == 'check_result':
            with self.lock:
                fields['quality_assessment'] = self._latest_quality
        try:
            row=super()._record(event, **fields)
        except (OSError,ValueError,TypeError):
            # Diagnostic storage must not turn an accepted push into unknown.
            # The separate, mandatory pre-send dedup save is still enforced.
            self._activity_error=True
            row=dict(time=int(time.time()*1000),event=event,policy=POLICY,
                     strategy_version=VERSION,**{k:fields[k] for k in
                     ('kind','result','signal_time') if k in fields})
            if isinstance(fields.get('metrics'),dict):
                row['metrics']={'qualified':fields['metrics'].get('qualified') is True}
        if self._activity is not None:
            try:self._activity.record(row)
            except (OSError,ValueError,TypeError):self._activity_error=True
        return row

    def status(self):
        result = super().status()
        with self.lock:
            quality = dict(self._latest_quality) if self._latest_quality is not None else None
        if result['last_diagnostics'] is not None:
            result['last_diagnostics'] = {**result['last_diagnostics'], 'quality_assessment': quality}
        activity=self._activity.status() if self._activity is not None else dict(
            window_hours=12,candidate_count=None,signal_attempts=None,
            accepted=None,rejected=None,unknown=None,
            observation_start_ms=None,full_window=False,tracking_complete=False,
            note='本机次数统计不可用，无法确认最近12小时发送数量；请检查磁盘空间和目录权限。')
        if self._activity_error:
            activity={**activity,'tracking_complete':False,
                      'note':activity['note']+' 本次运行诊断或统计保存曾失败，可能少计，不视为完整记录。'}
        result.update(policy=POLICY, strategy_version=VERSION, quality_assessment=quality,
                      research_mode=True,signal_activity_12h=activity)
        return result
