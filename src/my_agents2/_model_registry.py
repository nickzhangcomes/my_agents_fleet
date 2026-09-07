"""模型目录的落盘、有效期和刷新协调；不依赖上层 my_agents_fleet。"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class CollectionError(RuntimeError):
    """可公开展示的采集错误；不要把含凭据的命令输出放进消息。"""


def timestamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(timespec='seconds')


def epoch(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp()  # 旧版无时区时间按本机时区解释
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), mode)
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def file_stamp(path):
    """只读文件属性，不读取认证文件正文。路径也参与比较，能识别升级后的软链接。"""
    path = Path(path).expanduser().resolve()
    try:
        s = path.stat()
        return [str(path), s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    except OSError:
        return [str(path), None]


class ModelRegistry:
    def __init__(self, path, collectors, signature, *, ttl=21600, retry_interval=300, clock=time.time):
        self.path = Path(path).expanduser().resolve()
        self.collectors = collectors
        self.signature = signature
        self.ttl = ttl
        self.retry_interval = retry_interval
        self.clock = clock
        self._cache = None
        self._stamp = None
        self._read_lock = threading.RLock()
        self._lock_guard = threading.Lock()
        self._locks = {}
        self.read_error = ''
        self.runtime_errors = {}
        self._runtime_attempts = {}

    def read(self, force=False):
        with self._read_lock:
            stamp = file_stamp(self.path)
            if force or stamp != self._stamp or self._cache is None:
                try:
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    if not isinstance(data, dict) or not isinstance(data.get('backends'), dict):
                        raise ValueError('invalid registry')
                    for entry in data['backends'].values():
                        if not isinstance(entry, dict) or not isinstance(entry.get('models', []), list):
                            raise ValueError('invalid backend')
                        self._validate_models(entry.get('models', []))
                    self._cache = data
                    self.read_error = ''
                except FileNotFoundError:
                    self.read_error = '模型表文件缺失' if self._cache else ''
                    self._cache = self._cache or {'backends': {}}
                except (OSError, ValueError, TypeError, CollectionError):
                    # 常驻进程继续用上一份可读数据；后续写入前会保留损坏文件。
                    self.read_error = '模型表不可读，正在保留最近一次可读数据'
                    self._cache = self._cache or {'backends': {}}
                self._stamp = stamp
            return copy.deepcopy(self._cache)

    @staticmethod
    def _validate_models(models):
        seen = set()
        for m in models:
            if not isinstance(m, dict) or not isinstance(m.get('slug'), str) or not m['slug'].strip():
                raise CollectionError('模型列表格式无效')
            if m['slug'] in seen:
                raise CollectionError('模型列表包含重复 ID')
            seen.add(m['slug'])
            efforts = m.get('efforts')
            if efforts is not None and (not isinstance(efforts, list)
                                        or any(not isinstance(e, str) for e in efforts)):
                raise CollectionError('effort 列表格式无效')

    @contextmanager
    def _lock(self, name):
        with self._lock_guard:
            local = self._locks.setdefault(name, threading.RLock())
        with local:
            directory = self.path.parent / '.model-locks'
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f'{name}.lock').open('a+') as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _due(self, entry, sig, now, max_age):
        if (entry.get('last_error') and entry.get('attempt_fingerprint') == sig
                and now - epoch(entry.get('checked_at')) < self.retry_interval):
            return False
        return (not entry.get('models') or entry.get('source_fingerprint') != sig
                or now - epoch(entry.get('collected_at')) >= max_age)

    def ensure(self, backends, *, force=False, max_age=None, now=''):
        """按后端加锁采集，合并写入前重读；两个进程刷新不同后端也不会互相覆盖。"""
        report = {}
        for name in dict.fromkeys(backends):
            if name not in self.collectors:
                report[name] = 'no-collector'
                continue
            previous = self._runtime_attempts.get(name)
            if (not force and previous and previous[0] == self.signature(name)
                    and self.clock() - previous[1] < self.retry_interval):
                report[name] = 'kept（' + self.runtime_errors[name] + '）'
                continue
            try:
                report[name] = self._refresh_one(name, force, max_age, now)
                self.runtime_errors.pop(name, None)
                self._runtime_attempts.pop(name, None)
            except (OSError, ValueError) as exc:
                # 只读安装目录等落盘问题也不应阻断 Agent；状态接口必须能看见。
                self.runtime_errors[name] = f'模型表无法更新: {type(exc).__name__}'
                self._runtime_attempts[name] = (self.signature(name), self.clock())
                report[name] = 'kept（' + self.runtime_errors[name] + '）'
        return report

    def _refresh_one(self, name, force, max_age, supplied_now):
        now = epoch(supplied_now) if supplied_now else self.clock()
        max_age = self.ttl if max_age is None else max_age
        sig = self.signature(name)
        entry = self.read()['backends'].get(name, {})
        if not force and not self._due(entry, sig, now, max_age):
            return 'cached'
        with self._lock(name):
            # 等锁期间别人可能已经采完，自动刷新要重新检查。
            sig = self.signature(name)
            entry = self.read(force=True)['backends'].get(name, {})
            now = epoch(supplied_now) if supplied_now else self.clock()
            if not force and not self._due(entry, sig, now, max_age):
                return 'cached'
            data = None
            error = ''
            try:
                data = self.collectors[name]()
                if not isinstance(data, dict) or not data.get('models'):
                    raise CollectionError('未采到可用模型，保留旧表')
                self._validate_models(data['models'])
                if self.signature(name) != sig:
                    raise CollectionError('采集期间来源发生变化，下次重新采集')
            except CollectionError as exc:
                error = str(exc)
            except Exception as exc:
                error = f'采集失败: {type(exc).__name__}'
            completed = supplied_now or timestamp(self.clock())
            with self._lock('registry'):
                reg = self.read(force=True)
                if self.read_error and self.path.exists():
                    # 保留原始字节以便恢复；用独立文件名，避免覆盖上次备份。
                    fd, backup = tempfile.mkstemp(prefix=self.path.name + '.corrupt-', dir=self.path.parent)
                    with os.fdopen(fd, 'wb') as fh:
                        fh.write(self.path.read_bytes())
                old = reg['backends'].get(name, {})
                if error:
                    updated = dict(old, checked_at=completed, last_error=error, attempt_fingerprint=sig)
                    result = 'kept（' + error + '）' if old.get('models') else 'empty（' + error + '）'
                else:
                    updated = dict(data, collected_at=completed, checked_at=completed,
                                   source_fingerprint=sig, attempt_fingerprint=sig, last_error='')
                    reg['updated_at'] = completed  # 只有成功采到数据才推进成功时间
                    result = f"ok ({len(data['models'])} 个模型, source={data.get('source', 'unknown')})"
                reg['backends'][name] = updated
                reg['checked_at'] = completed
                _atomic_write(self.path, reg)
                self.read(force=True)
            return result

    def status(self, backend):
        reg = self.read()
        entry = reg['backends'].get(backend, {})
        sig = self.signature(backend)
        reasons = []
        now = self.clock()
        if not entry.get('models'):
            reasons.append('尚无可用模型表')
        if entry.get('source_fingerprint') != sig:
            reasons.append('来源已变化或尚未核对')
        if now - epoch(entry.get('collected_at')) >= self.ttl:
            reasons.append('已超过刷新间隔')
        source_age = None
        if entry.get('source') == 'cache':
            source_age = max(0, now - epoch(entry.get('source_updated_at')))
            if not epoch(entry.get('source_updated_at')) or source_age >= self.ttl:
                reasons.append('CLI 上游缓存过期或时间未知')
        error = self.runtime_errors.get(backend) or entry.get('last_error') or self.read_error
        if error:
            reasons.append(error)
        return dict(backend=backend, model_count=len(entry.get('models', [])),
                    source=entry.get('source', ''), collected_at=entry.get('collected_at'),
                    checked_at=entry.get('checked_at'), source_updated_at=entry.get('source_updated_at'),
                    stale=bool(reasons), stale_reasons=reasons, last_error=error or None,
                    refresh_interval_seconds=self.ttl, retry_interval_seconds=self.retry_interval)
