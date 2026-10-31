"""Bounded local diagnostic journal. No credentials or provider error bodies."""
from collections import deque
import json
import os
from pathlib import Path
import threading
import time
from datetime import datetime, timezone
import tempfile


class Journal:
    def __init__(self, directory, max_bytes=5_000_000):
        self.path = Path(directory)/'monitor.jsonl'
        self.max_bytes = max_bytes
        self.recent = deque(maxlen=50)
        self.lock = threading.RLock()
        self.days = {}
        summary = self.path.parent/'daily.json'
        if summary.exists():
            try:
                saved = json.loads(summary.read_text(encoding='utf-8'))
                if isinstance(saved, dict) and all(isinstance(v, dict) for v in saved.values()): self.days = saved
            except (ValueError, OSError): pass
        # Recover only the bounded tail; a torn final line is ignored.
        if self.path.exists():
            with self.path.open('rb') as stream:
                stream.seek(max(0, self.path.stat().st_size-128_000))
                for line in stream:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict): self.recent.append(row)
                    except (ValueError, UnicodeError): pass

    def record(self, event, **fields):
        row = dict(time=int(time.time()*1000), event=event, **fields)
        encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n').encode()
        with self.lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size+len(encoded) > self.max_bytes:
                for number in (2, 1):
                    old = self.path.with_name(f'monitor.jsonl.{number}')
                    if old.exists(): os.replace(old, self.path.with_name(f'monitor.jsonl.{number+1}'))
                os.replace(self.path, self.path.with_name('monitor.jsonl.1'))
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            self.recent.append(row)
            self._count(row)
        return row

    def _count(self, row):
        day = datetime.fromtimestamp(row['time']/1000, timezone.utc).date().isoformat()
        counters = self.days.setdefault(day, dict(checks=0, candidate_minutes=0, blocked=0,
                                                  signal_attempts=0, accepted=0, unknown=0, rejected=0, test_attempts=0))
        if row['event'] == 'check_started': counters['checks'] += 1
        if row['event'] == 'check_result':
            stamp = row.get('signal_time')
            if isinstance(row.get('metrics'), dict) and row['metrics'].get('qualified') and stamp != counters.get('last_candidate_time'):
                counters['candidate_minutes'] += 1
                counters['last_candidate_time'] = stamp
        if row['event'] == 'check_blocked': counters['blocked'] += 1
        if row['event'] == 'push_attempt':
            counters['test_attempts' if row.get('kind') == 'test' else 'signal_attempts'] += 1
        if row['event'] == 'push_result' and row.get('kind') == 'signal':
            counters[row['result']] = counters.get(row['result'], 0)+1
        self.days = {k: self.days[k] for k in sorted(self.days)[-31:]}
        fd, name = tempfile.mkstemp(prefix='.daily-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.days, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path.parent/'daily.json')
        finally:
            if os.path.exists(name): os.unlink(name)

    def daily(self):
        with self.lock: return {k: dict(v) for k, v in self.days.items()}

    def tail(self):
        with self.lock: return list(self.recent)
