"""容量与工作区锁 —— 「排得上吗」「会不会跟人抢目录」。

两件事，两种作用域，别混：

    并发上限  per-agent，**进程内**    同一个 agent 同时最多跑几个
    工作区锁  per-目录，**跨进程**    同一个目录同时只许一个 run 写

为什么并发上限挂在 agent 而不是全局：不同 agent 跑不同后端、不同模型、成本差一个量级，
容量该分开设（Multica 也是把 max-concurrent-tasks 放在 agent 上，默认 6）。

为什么工作区锁必须**跨进程**：真实的踩踏场景是「cron 里的批处理」和「常驻服务的调度器」
同时叫醒同一个 agent，两个**不同的进程**写同一个资料目录。线程锁在这个场景里完全没用。
所以走文件锁（`fcntl.flock`），锁文件落在 fleet 的状态区。

拿不到锁时 Run 会先落一条 `waiting_workspace`（Multica 的 `waiting_local_directory` 同款）——
「没在跑」有很多种原因，混成一个 pending 就查不出到底卡在哪。
"""
from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

try:
    import fcntl
except ImportError:                      # pragma: no cover  (Windows)
    fcntl = None


class WorkspaceBusy(RuntimeError):
    """等锁超时。调用方决定是排队重来还是放弃 —— 库不替你决定。"""


class RunCancelled(RuntimeError):
    """等待名额或目录锁时被调用方停止。"""


class Capacity:
    """一个 fleet 的容量管理：角色并发计数 + 工作目录文件锁。"""

    def __init__(self, lock_dir):
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._active: Dict[str, int] = {}
        self._limits: Dict[str, int] = {}
        self._condition = threading.Condition()

    # ── 并发上限（进程内）──
    @contextmanager
    def slot(self, agent: str, limit: int, *, on_wait=None, handle=None):
        """占名额；调整上限时仍统计已运行任务，不另开一套计数。"""
        waited = False
        with self._condition:
            self._limits[agent] = limit
            self._condition.notify_all()
            while self._limits[agent] > 0 and self._active.get(agent, 0) >= self._limits[agent]:
                if handle is not None and handle.stopped:
                    raise RunCancelled('等待并发名额时被停止')
                if not waited:
                    waited = True
                    if on_wait:
                        on_wait()
                self._condition.wait(timeout=0.1)
            if handle is not None and handle.stopped:
                raise RunCancelled('开始执行前被停止')
            self._active[agent] = self._active.get(agent, 0) + 1
        try:
            yield waited
        finally:
            with self._condition:
                self._active[agent] -= 1
                self._condition.notify_all()

    # ── 工作区锁（跨进程）──
    def _lock_path(self, path: str) -> Path:
        real = os.path.realpath(os.path.expanduser(path))   # /tmp 是软链，不 realpath 会算成两个
        h = hashlib.sha1(real.encode("utf-8")).hexdigest()[:16]
        return self.lock_dir / f"ws-{h}.lock"

    @contextmanager
    def workspace(self, path: Optional[str], *, timeout: float = 0.0,
                  poll: float = 0.1, on_wait=None, handle=None):
        """独占一个工作目录。`timeout=0` 表示拿不到就立刻抛 WorkspaceBusy。

        没有 fcntl 时明确报错，不能把未加锁当成已获得独占访问。
        """
        if not path:
            yield False
            return
        if fcntl is None:
            raise RuntimeError('当前平台不支持工作目录文件锁')
        lp = self._lock_path(path)
        fh = open(lp, "a+")                                   # noqa: SIM115  锁的生命周期跟着 with
        deadline = time.monotonic() + max(0.0, timeout)
        waited = False
        try:
            while True:
                if handle is not None and handle.stopped:
                    raise RunCancelled('等待工作目录锁时被停止')
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if not waited:
                        waited = True
                        if on_wait:
                            on_wait()                          # 让调用方去落 waiting_workspace
                    if time.monotonic() >= deadline:
                        raise WorkspaceBusy(
                            f"工作目录被另一个 run 占着：{path}"
                            f"（锁文件 {lp}，等了 {timeout:.0f}s）") from e
                    time.sleep(poll)
            fh.write(f"{os.getpid()} {time.time():.0f}\n")
            fh.flush()
            yield waited
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
