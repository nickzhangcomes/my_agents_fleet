"""Run 记录：留痕、按类重试、老库补列。

`test_migrate_*` 是**回归测试**：实现时踩过一次 —— 建索引排在补列之前，
老库直接 `no such column: ref` 炸掉。Run 表是长期资产，加字段不能重建丢历史。
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from ._fake import AI_ROOT, FakeAgent

sys.path.insert(0, str(AI_ROOT))

from my_agents2 import Result                                              # noqa: E402
from my_agents_fleet import RecordedAgent, Run, RunStore                         # noqa: E402
from my_agents_fleet.runs import STATE_COMPLETED, STATE_FAILED                   # noqa: E402

OK = Result(text="done", ok=True, cost_usd=0.01)
TIMEOUT = Result(text="", ok=False, timed_out=True, error="killed")
NOLOGIN = Result(text="Not logged in · Please run /login", ok=True)


def _store() -> RunStore:
    return RunStore(Path(tempfile.mkdtemp()) / "t.db")


def _agent(store, script, *, name="a", attempts=3, schema=None):
    return RecordedAgent(FakeAgent(script, output_schema=schema), store=store,
                         name=name, backend="fake", max_attempts=attempts,
                         sleep=lambda s: None, logger=lambda m: None)


class TestRetryPolicy(unittest.TestCase):

    def test_platform_failure_retries(self):
        st = _store()
        ag = _agent(st, [TIMEOUT, OK])
        out = ag.ask("x", ref="r1")
        self.assertTrue(out.ok)
        self.assertEqual(ag.raw.calls, 2)
        self.assertEqual(len(out.attempts), 2, "每次尝试各留一条记录")

    def test_agent_failure_does_not_retry(self):
        st = _store()
        ag = _agent(st, [Result(text="非JSON", ok=True), OK], schema={"type": "object"})
        out = ag.ask("x")
        self.assertFalse(out.ok)
        self.assertEqual(ag.raw.calls, 1, "agent 侧的错停在第一次，不白烧第二遍")

    def test_unretryable_platform_failure_stops(self):
        st = _store()
        ag = _agent(st, [NOLOGIN, OK])
        self.assertFalse(ag.ask("x").ok)
        self.assertEqual(ag.raw.calls, 1)

    def test_gives_up_at_max_attempts(self):
        st = _store()
        ag = _agent(st, [TIMEOUT], attempts=2)
        out = ag.ask("x")
        self.assertEqual(ag.raw.calls, 2)
        self.assertEqual(out.failure.code, "timeout")

    def test_cost_sums_across_attempts(self):
        """成本必须含重试 —— 只看最后一次会低估真实花费。"""
        st = _store()
        out = _agent(st, [Result(ok=False, timed_out=True, cost_usd=0.02), OK]).ask("x")
        self.assertAlmostEqual(out.cost_usd, 0.03)


class TestRecording(unittest.TestCase):

    def test_run_row_written_before_execution(self):
        """跑之前就落 running —— 进程被杀也留得下痕迹。"""
        st = _store()
        seen = {}

        class Peek(FakeAgent):
            def ask(self, prompt, *, continuation=None):
                seen["rows"] = len(st.query())
                return super().ask(prompt, continuation=continuation)

        RecordedAgent(Peek([OK]), store=st, name="a", backend="fake",
                      logger=lambda m: None).ask("x")
        self.assertEqual(seen["rows"], 1, "执行时表里就该有那条 running")

    def test_no_prompt_or_output_body_stored(self):
        """只存长度，不存正文 —— Run 表长期落盘，正文进表就收不回来。"""
        st = _store()
        _agent(st, [Result(text="超级敏感的产出内容", ok=True)]).ask("超级敏感的提示词")
        blob = Path(st.path).read_bytes()
        self.assertNotIn("超级敏感的提示词".encode(), blob)
        self.assertNotIn("超级敏感的产出内容".encode(), blob)
        r = st.query()[0]
        self.assertEqual(r.prompt_chars, len("超级敏感的提示词"))
        self.assertEqual(r.output_chars, len("超级敏感的产出内容"))

    def test_exception_is_still_recorded(self):
        st = _store()
        ag = _agent(st, [RuntimeError("boom")])
        with self.assertRaises(RuntimeError):
            ag.ask("x")
        r = st.query()[0]
        self.assertEqual((r.state, r.fail_code), (STATE_FAILED, "backend_crashed"))

    def test_ref_aggregates(self):
        """ref 是业务的挂载点，中台不解释含义，但能按它聚合。"""
        st = _store()
        _agent(st, [TIMEOUT, OK]).ask("x", ref="episode:philosophy:37")
        rows = st.query(ref="episode:philosophy:37")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.state for r in rows}, {STATE_COMPLETED, STATE_FAILED})

    def test_degraded_records_backend_ignored(self):
        st = _store()
        ag = RecordedAgent(FakeAgent([OK], ignored=("effort", "add_dirs")), store=st,
                           name="a", backend="fake", logger=lambda m: None)
        ag.ask("x")
        self.assertIn("effort", st.query()[0].degraded)


class TestResourceHygiene(unittest.TestCase):

    def test_no_leaked_sqlite_connections(self):
        """回归：`with sqlite3.connect(...)` 只提交事务、**不关连接**。
           每次读写漏一个，cron + 常驻服务一起跑迟早把文件描述符耗光。"""
        import warnings
        st = _store()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for i in range(30):
                _agent(st, [OK], name=f"a{i}").ask("x")
                st.query()
                st.stats()
            import gc
            gc.collect()
        leaks = [w for w in caught if "unclosed database" in str(w.message)]
        self.assertEqual(leaks, [], f"漏了 {len(leaks)} 个连接")


class TestStats(unittest.TestCase):

    def test_zero_failures_reads_as_zero_not_none(self):
        """SUM 在零行时返回 NULL —— 渲染成 None 而不是 0，看报表的人会以为没数据。"""
        st = _store()
        _agent(st, [OK]).ask("x")
        s = st.stats()[0]
        self.assertEqual((s["failed"], s["platform_fail"], s["agent_fail"]), (0, 0, 0))

    def test_fail_breakdown_groups_by_code(self):
        st = _store()
        _agent(st, [TIMEOUT], attempts=2).ask("x")
        _agent(st, [NOLOGIN], name="b").ask("x")
        got = {r["fail_code"]: r["n"] for r in st.fail_breakdown()}
        self.assertEqual(got, {"timeout": 2, "not_logged_in": 1})


class TestMigration(unittest.TestCase):
    """老库补列。加字段不能重建表 —— Run 表要看的是趋势。"""

    def _legacy_db(self) -> Path:
        p = Path(tempfile.mkdtemp()) / "old.db"
        with closing(sqlite3.connect(str(p))) as con:
            con.executescript(
                "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE,"
                " agent TEXT, state TEXT, started_at TEXT);"
                "INSERT INTO runs(run_id,agent,state,started_at)"
                " VALUES('old-1','x','completed','2026-01-01');")
            con.commit()
        return p

    def test_migrate_keeps_history(self):
        st = RunStore(self._legacy_db())
        rows = st.query()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].run_id, "old-1")

    def test_migrate_adds_all_columns(self):
        p = self._legacy_db()
        RunStore(p)
        with closing(sqlite3.connect(str(p))) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertLessEqual({"ref", "cost_usd", "trust", "deny",
                              "assets_enforced", "waited"}, cols)

    def test_migrate_builds_indexes_after_columns(self):
        """回归：建索引曾排在补列之前，老库直接 no such column: ref。"""
        p = self._legacy_db()
        RunStore(p)                                   # 不炸就算过
        with closing(sqlite3.connect(str(p))) as con:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_runs_ref", names)

    def test_migrated_db_still_writable(self):
        p = self._legacy_db()
        st = RunStore(p)
        st.begin(Run(run_id="new-1", agent="y", ref="r", trust="untrusted"))
        self.assertEqual(len(st.query()), 2)


if __name__ == "__main__":
    unittest.main()
