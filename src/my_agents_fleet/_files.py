"""原子 JSON 写入与可重入的跨进程文件锁。"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import fcntl

_GUARD = threading.Lock()
_LOCKS = {}
_HELD = threading.local()


@contextmanager
def file_lock(path):
    """同一路径在线程间串行，在进程间 flock；同线程允许嵌套调用。"""
    path = Path(path).expanduser().resolve()
    key = str(path)
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_HELD, 'paths', None)
        if held is None:
            held = _HELD.paths = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+') as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def atomic_json(path, value):
    """独立临时文件 + fsync + replace；失败时保留原文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), mode)
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
