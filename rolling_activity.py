"""Bounded, credential-free counts of actual latest-policy monitor activity."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time


POLICY = 'latest_v5'
WINDOW_MS = 12 * 60 * 60 * 1000
TARGET = 20
MAX_SAMPLES = 10_000
STATE_NAME = 'rolling-activity.json'


def now_ms():
    """Separate clock so callers and tests can supply a deterministic time."""
    return int(time.time() * 1000)


def _stamp(value):
    # A boolean, string or fractional timestamp is not an observation time.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = int(value)
    except (ValueError, OverflowError):
        return None
    return converted if converted >= 0 and converted == value else None


class RollingActivity:
    """Count distinct candidates and real Bark actions in (now - 12h, now].

    A candidate is not a notification. An accepted result only confirms that
    the push service accepted the request, never that a phone displayed it.
    Journals are imported once, only when there is no saved activity state.
    """

    def __init__(self, directory, journal_directory=None):
        self.directory = Path(directory)
        self.path = self.directory / STATE_NAME
        self.journal_directory = Path(journal_directory) if journal_directory is not None else self.directory
        self.lock = threading.RLock()
        current = now_ms()
        self.observation_start_ms = current
        self.samples = []
        self._incomplete_until_ms = None
        self._dirty = False
        if self.path.exists():
            self._load(current)
        else:
            self._recover(current)
        self._prune(current)
        # Persist the start even when no qualifying event has occurred yet.
        self._save(current)

    def _incomplete(self, current):
        self._incomplete_until_ms = max(self._incomplete_until_ms or 0, current + WINDOW_MS)

    @staticmethod
    def _key(sample):
        return sample['kind'], sample['stamp'], sample.get('result')

    @staticmethod
    def _sample(row):
        if not isinstance(row, dict) or row.get('policy') != POLICY:
            return None
        stamp, observed = _stamp(row.get('signal_time')), _stamp(row.get('time'))
        if stamp is None or observed is None:
            return None
        event = row.get('event')
        if event == 'check_result':
            metrics = row.get('metrics')
            if not isinstance(metrics, dict) or metrics.get('qualified') is not True:
                return None
            kind = 'candidate'
        elif event in ('push_attempt', 'push_result') and row.get('kind') == 'signal':
            kind = 'attempt' if event == 'push_attempt' else 'result'
        else:
            return None
        sample = dict(time=observed, kind=kind, stamp=stamp)
        if kind == 'result':
            if row.get('result') not in ('accepted', 'rejected', 'unknown'):
                return None
            sample['result'] = row['result']
        return sample

    @staticmethod
    def _saved_sample(row):
        if not isinstance(row, dict):
            return None
        observed, stamp = _stamp(row.get('time')), _stamp(row.get('stamp'))
        kind = row.get('kind')
        if observed is None or stamp is None or kind not in ('candidate', 'attempt', 'result'):
            return None
        sample = dict(time=observed, kind=kind, stamp=stamp)
        if kind == 'result':
            if row.get('result') not in ('accepted', 'rejected', 'unknown'):
                return None
            sample['result'] = row['result']
        return sample

    def _load(self, current):
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(saved, dict) or saved.get('schema') != 1
                    or saved.get('policy') != POLICY or not isinstance(saved.get('samples'), list)):
                raise ValueError('unsupported activity state')
            start = _stamp(saved.get('observation_start_ms'))
            if start is None or start > current:
                raise ValueError('invalid observation start')
            self.observation_start_ms = start
            recovery = saved.get('incomplete_until_ms')
            if recovery is not None:
                recovery = _stamp(recovery)
                if recovery is None:
                    raise ValueError('invalid completeness state')
                self._incomplete_until_ms = recovery
            elif saved.get('tracking_complete') is not True:
                self._incomplete(current)
            for source in saved['samples']:
                sample = self._saved_sample(source)
                if sample is None or sample['time'] > current:
                    self._incomplete(current)
                else:
                    self.samples.append(sample)
        except (OSError, ValueError, UnicodeError, TypeError):
            # Legacy daily totals and unreadable state do not prove coverage.
            self.observation_start_ms = current
            self.samples = []
            self._incomplete(current)

    def _recover(self, current):
        earliest = current
        for name in ('monitor.jsonl.3', 'monitor.jsonl.2', 'monitor.jsonl.1', 'monitor.jsonl'):
            path = self.journal_directory / name
            if not path.exists():
                continue
            try:
                with path.open('rb') as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError('invalid journal row')
                            if row.get('policy') != POLICY:
                                continue
                            observed = _stamp(row.get('time'))
                            if observed is None or observed > current:
                                raise ValueError('invalid journal time')
                            earliest = min(earliest, observed)
                            sample = self._sample(row)
                            if sample is not None:
                                # Include older rows until deduplication, or a
                                # retry inside the window could refresh an old
                                # signal whose first attempt was outside it.
                                self.samples.append(sample)
                            elif ((row.get('event') in ('push_attempt', 'push_result') and row.get('kind') == 'signal')
                                  or (row.get('event') == 'check_result' and isinstance(row.get('metrics'), dict)
                                      and row['metrics'].get('qualified') is True)):
                                raise ValueError('invalid activity journal row')
                        except (ValueError, UnicodeError, TypeError):
                            self._incomplete(current)
            except OSError:
                self._incomplete(current)
        self.observation_start_ms = earliest

    def _prune(self, current):
        # Keep the first actual event, so a duplicate cannot refresh its age.
        unique = {}
        for sample in sorted(self.samples, key=lambda value: value['time']):
            unique.setdefault(self._key(sample), sample)
        retained = [sample for sample in unique.values()
                    if current - WINDOW_MS < sample['time'] <= current]
        if len(retained) > MAX_SAMPLES:
            dropped = retained[:-MAX_SAMPLES]
            self._incomplete_until_ms = max(self._incomplete_until_ms or 0, dropped[-1]['time'] + WINDOW_MS)
            retained = retained[-MAX_SAMPLES:]
        self.samples = retained

    def _save(self, current):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved = dict(schema=1, policy=POLICY, observation_start_ms=self.observation_start_ms,
                     tracking_complete=self._incomplete_until_ms is None or current >= self._incomplete_until_ms,
                     incomplete_until_ms=self._incomplete_until_ms,
                     samples=self.samples)
        fd, temporary = tempfile.mkstemp(prefix='.rolling-activity-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(saved, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record(self, row):
        """Persist supported rows; return whether a distinct event was added.

        Storage errors are raised. Integrators must isolate these diagnostic
        errors from the sender's result: a failed count write cannot turn an
        already accepted push into an unknown or trigger another push.
        """
        sample = self._sample(row)
        current = now_ms()
        with self.lock:
            self._prune(current)
            added = False
            if sample is not None and current - WINDOW_MS < sample['time'] <= current:
                existing = next((value for value in self.samples
                                 if self._key(value) == self._key(sample)), None)
                if existing is None:
                    self.samples.append(sample)
                    self._dirty = added = True
                elif sample['time'] < existing['time']:
                    existing['time'] = sample['time']
                    self._dirty = True
                self._prune(current)
            if not self._dirty:
                return False
            try:
                self._save(current)
            except (OSError, ValueError, TypeError):
                self._incomplete(current)
                raise
            self._dirty = False
            return added

    def status(self, now_ms=None):
        current = globals()['now_ms']() if now_ms is None else _stamp(now_ms)
        if current is None:
            raise ValueError('invalid current timestamp')
        with self.lock:
            self._prune(current)
            counts = dict(candidate_count=0, signal_attempts=0, accepted=0, rejected=0, unknown=0)
            for sample in self.samples:
                if sample['kind'] == 'candidate':
                    counts['candidate_count'] += 1
                elif sample['kind'] == 'attempt':
                    counts['signal_attempts'] += 1
                else:
                    counts[sample['result']] += 1
            full = self.observation_start_ms <= current - WINDOW_MS
            complete = self._incomplete_until_ms is None or current >= self._incomplete_until_ms
            notes = ['候选不等于正式买点推送；accepted 仅代表 Bark 服务接受，不等于手机已展示。']
            if not full:
                notes.append('连续观察尚未满12小时，当前计数不能证明每12小时达标。')
            if not complete:
                notes.append('记录缺失、损坏、容量截断或状态保存异常可能导致少计，当前12小时统计不完整；需要恢复连续观察，不能证明达标或承诺无遗漏。')
            return dict(window_hours=12, target=TARGET, **counts,
                        gap=max(0, TARGET - counts['signal_attempts']),
                        accepted_gap=max(0, TARGET - counts['accepted']),
                        observation_start_ms=self.observation_start_ms,
                        full_window=full, tracking_complete=complete,
                        note=' '.join(notes))
