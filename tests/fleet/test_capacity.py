"""容量与工作区锁。

工作区锁必须**跨进程**：真实的踩踏是「cron 的批处理」和「常驻服务的调度器」
两个不同进程写同一个目录，线程锁在这个场景里完全没用。
所以这里真的起子进程去持锁，不用 mock —— mock 出来的「跨进程」证明不了任何事。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ._fake import AI_ROOT

sys.path.insert(0, str(AI_ROOT))

from my_agents_fleet import Capacity, WorkspaceBusy                               # noqa: E402

_HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from my_agents_fleet import Capacity
with Capacity({locks!r}).workspace({ws!r}, timeout=0):
    print("held", flush=True)
    time.sleep({secs})
"""


class TestConcurrencyLimit(unittest.TestCase):

    def setUp(self):
        self.cap = Capacity(Path(tempfile.mkdtemp()) / "locks")

    def _peak(self, limit, threads):
        peak = cur = 0
        lk = threading.Lock()

        def work():
            nonlocal peak, cur
            with self.cap.slot("a", limit):
                with lk:
                    cur += 1
                    peak = max(peak, cur)
                time.sleep(0.08)
                with lk:
                    cur -= 1

        ts = [threading.Thread(target=work) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return peak

    def test_limit_is_respected(self):
        self.assertEqual(self._peak(2, 6), 2)

    def test_limit_one_serializes(self):
        self.assertEqual(self._peak(1, 4), 1)

    def test_zero_means_unlimited(self):
        self.assertGreater(self._peak(0, 4), 1)

    def test_reports_whether_it_queued(self):
        """排过队要能看见 —— Run 里记 waited=capacity。"""
        with self.cap.slot("a", 1) as first:
            self.assertFalse(first)
        seen = []
        with self.cap.slot("a", 1):
            t = threading.Thread(target=lambda: seen.append(
                self.cap.slot("a", 1).__enter__()))
            t.start()
            time.sleep(0.1)
        t.join()
        self.assertEqual(seen, [True], "第二个应当报告自己排过队")

    def test_changing_limit_does_not_deadlock(self):
        """配置改了（磁盘即事实源，每次 get 都重读）不能把在跑的那批卡死。"""
        with self.cap.slot("a", 1):
            pass
        self.assertEqual(self._peak(3, 3), 3)


class TestWorkspaceLock(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp()
        self.ws = os.path.join(d, "ws")
        os.makedirs(self.ws)
        self.locks = os.path.join(d, "locks")
        self.cap = Capacity(self.locks)
        self.holder = None

    def tearDown(self):
        if self.holder:
            if self.holder.poll() is None:
                self.holder.kill()
            self.holder.wait()
            if self.holder.stdout:
                self.holder.stdout.close()

    def _hold(self, secs):
        self.holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER.format(root=str(AI_ROOT), locks=self.locks,
                                                  ws=self.ws, secs=secs)],
            stdout=subprocess.PIPE, text=True)
        self.holder.stdout.readline()      # 等它真拿到锁再往下

    def test_uncontended_is_free(self):
        with self.cap.workspace(self.ws, timeout=0) as waited:
            self.assertFalse(waited)

    def test_busy_with_zero_timeout_fails_fast(self):
        self._hold(3)
        t0 = time.time()
        with self.assertRaises(WorkspaceBusy):
            with self.cap.workspace(self.ws, timeout=0):
                pass
        self.assertLess(time.time() - t0, 1.0, "timeout=0 就该立刻失败，不该等")

    def test_waits_and_acquires_after_release(self):
        self._hold(1.5)
        t0 = time.time()
        with self.cap.workspace(self.ws, timeout=15) as waited:
            self.assertTrue(waited)
        self.assertGreater(time.time() - t0, 1.0, "确实等到了对方释放")

    def test_on_wait_fires_once_when_blocked(self):
        self._hold(1.5)
        hits = []
        with self.cap.workspace(self.ws, timeout=15, on_wait=lambda: hits.append(1)):
            pass
        self.assertEqual(len(hits), 1, "只在开始等的时候通知一次，不是每轮轮询都通知")

    def test_symlinked_paths_share_one_lock(self):
        """macOS 的 /tmp 是软链 —— 不走 realpath 会把同一个目录算成两把锁，
           锁了等于没锁。"""
        a = self.cap._lock_path("/tmp/x")
        b = self.cap._lock_path(os.path.realpath("/tmp") + "/x")
        self.assertEqual(a, b)

    def test_none_path_is_a_noop(self):
        with self.cap.workspace(None, timeout=0) as waited:
            self.assertFalse(waited)


if __name__ == "__main__":
    unittest.main()
