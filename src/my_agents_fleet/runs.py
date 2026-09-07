"""Run 记录 —— 「这个角色一直以来跑成什么样」的事实源。

一条 Run = 一次执行。对应 Multica 的 Task（它那句「一个 issue 能横跨多次运行，
每次运行各有各的记录」就是这个意思）。

★ 两条刻意的设计：

1. **不存 prompt 和产出正文。** 中台被多个业务共用，会经手各业务的数据
   （可能含密钥、隐私、用户输入）。Run 表要长期落盘、要被查询、可能被导出，
   **正文一旦进表就再也收不回来**。所以只存「长度」和「有没有拿到结构化产出」——
   足够回答运营问题（跑了几次/多久/多少钱/怎么挂的），不承担泄漏风险。
   error 另存截断后的错误摘要。文件任务的 `log_path` 指向工作目录的追加日志，
   业务自己决定留多久；普通问答不另存全文日志。

2. **`ref` 中台不解释。** 业务的工作对象长什么样（帖子/邮件/研报/一集）
   是业务的事，中台定义不了。`ref` 只是个字符串挂载点：
   业务塞 `"episode:philosophy:37"`，中台就能回答「这个 ref 跑过 3 次、
   2 次超时 1 次成功、一共花了 $0.84」，而不需要知道 episode 是什么。

`state` 的取值刻意留了 Multica 那套的形状（它有 8 个态），现在只用到其中几个，
以后加 `queued` / `deferred` 不用改表。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ._files import file_lock

# 现在会出现的状态；字段是自由文本，以后加不用改表
STATE_RUNNING = "running"
STATE_WAITING_CAPACITY = "waiting_capacity"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_WAITING_WORKSPACE = "waiting_workspace"   # 工作区被别的 run 占着

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT UNIQUE NOT NULL,
    ref            TEXT,              -- 业务的工作项标识，中台不解释
    agent          TEXT NOT NULL,
    backend        TEXT,
    model          TEXT,
    effort         TEXT,
    trigger        TEXT,              -- 谁发起的，业务自报：cron / manual / schedule / …
    session_id     TEXT,              -- 属于哪个持久会话（无则空）
    state          TEXT NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 1,
    fail_class     TEXT,              -- platform | agent
    fail_code      TEXT,
    error          TEXT,              -- 错误摘要，不是产出正文
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    duration_ms    INTEGER,
    cost_usd       REAL,
    usage_json     TEXT,
    prompt_chars   INTEGER,           -- 只存长度，不存正文
    output_chars   INTEGER,
    structured     INTEGER,           -- 是否拿到结构化产出：1/0/NULL(没要)
    identity_mode  TEXT,              -- 人设注入方式：system | prompt-prefix | none
    degraded       TEXT,              -- agents2 的 ag.ignored：这轮有哪些参数没落地
    fingerprint    TEXT,              -- 角色、目录、权限、模型及身份提示的指纹
    log_path       TEXT,
    trust          TEXT,              -- trusted | untrusted
    deny           TEXT,              -- 本轮真拦住的能力类别
    assets_enforced TEXT,             -- hard(机制) | soft(仅约定) | none —— 只读到底做到没有
    waited         TEXT               -- 排过队/等过锁：capacity | workspace
);
"""

# ⚠️ 索引**必须**在补列之后建：老库缺 ref 时，`CREATE INDEX … ON runs(ref)` 会直接炸。
#    所以建表 → 补列 → 建索引，三步分开，不能塞进一个 executescript。
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_runs_agent   ON runs(agent, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_ref     ON runs(ref);
CREATE INDEX IF NOT EXISTS idx_runs_state   ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""

# 期望的列 → 类型。补列时逐条比对，不用维护「第 N 版加了什么」的迁移脚本。
# 不含 id / run_id / agent / state / started_at —— 那几个建表时就有，缺了说明库不是这张表。
_COLUMNS = (
    ("ref", "TEXT"), ("backend", "TEXT"), ("model", "TEXT"), ("effort", "TEXT"),
    ("trigger", "TEXT"), ("session_id", "TEXT"), ("attempt", "INTEGER"),
    ("fail_class", "TEXT"), ("fail_code", "TEXT"), ("error", "TEXT"),
    ("ended_at", "TEXT"), ("duration_ms", "INTEGER"), ("cost_usd", "REAL"),
    ("usage_json", "TEXT"), ("prompt_chars", "INTEGER"), ("output_chars", "INTEGER"),
    ("structured", "INTEGER"), ("identity_mode", "TEXT"), ("degraded", "TEXT"),
    ("fingerprint", "TEXT"), ("log_path", "TEXT"), ("trust", "TEXT"),
    ("deny", "TEXT"), ("assets_enforced", "TEXT"), ("waited", "TEXT"),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_run_id(agent: str) -> str:
    return f"{agent}-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(8)}"


@dataclass
class Run:
    """一条执行记录。`begin()` 造出来时是 running，`finish()` 收尾。"""
    run_id: str
    agent: str
    state: str = STATE_RUNNING
    ref: Optional[str] = None
    backend: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    trigger: Optional[str] = None
    session_id: Optional[str] = None
    attempt: int = 1
    fail_class: Optional[str] = None
    fail_code: Optional[str] = None
    error: Optional[str] = None
    started_at: str = field(default_factory=_now)
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    usage: Optional[dict] = None
    prompt_chars: Optional[int] = None
    output_chars: Optional[int] = None
    structured: Optional[bool] = None
    identity_mode: Optional[str] = None
    degraded: Optional[str] = None
    fingerprint: Optional[str] = None
    log_path: Optional[str] = None
    trust: Optional[str] = None
    deny: Optional[str] = None
    assets_enforced: Optional[str] = None
    waited: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.state == STATE_COMPLETED

    def __repr__(self) -> str:
        tail = f" {self.fail_code}" if self.fail_code else ""
        cost = f" ${self.cost_usd:.4f}" if self.cost_usd else ""
        return f"<Run {self.run_id} {self.state}{tail}{cost}>"


class RunStore:
    """Run 记录的 SQLite 落盘。放业务项目自己的 fleet 目录下（`state/fleet.db`），不写进中台。"""

    def __init__(self, db_path):
        self.path = Path(os.path.expanduser(str(db_path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix(self.path.suffix + ".init.lock")), self._con() as con:
            con.executescript(_SCHEMA)      # ① 建表（已有就跳过）
            self._migrate(con)              # ② 补列
            con.executescript(_INDEXES)     # ③ 建索引 —— 必须在补列之后

    @staticmethod
    def _migrate(con) -> None:
        """老库补缺的列。Run 表是长期资产（要看趋势），不能因为加了字段就重建丢历史。"""
        have = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        for col, decl in _COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

    @contextmanager
    def _con(self):
        """开一个连接，用完提交并**关掉**。

        ⚠️ 不能只写 `with sqlite3.connect(...) as con:` —— sqlite3 的连接上下文管理器
           只管事务提交/回滚，**不关连接**。每次读写漏一个，常驻服务（cron + 服务进程
           一起跑）迟早把文件描述符耗光。
        """
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")     # 多进程同时写（cron + 服务）不互相锁死
        try:
            with con:
                yield con
        finally:
            con.close()

    # ── 写 ──
    def begin(self, run: Run) -> Run:
        """开一条 running 记录。**执行之前**就落盘 —— 进程被杀也留得下痕迹，
           否则「跑到一半机器重启」这类失败在表里根本不存在。"""
        with self._con() as con:
            con.execute(
                "INSERT INTO runs (run_id, ref, agent, backend, model, effort, trigger,"
                " session_id, state, attempt, started_at, prompt_chars, identity_mode,"
                " degraded, fingerprint, trust, deny, assets_enforced, waited)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run.run_id, run.ref, run.agent, run.backend, run.model, run.effort,
                 run.trigger, run.session_id, run.state, run.attempt, run.started_at,
                 run.prompt_chars, run.identity_mode, run.degraded, run.fingerprint,
                 run.trust, run.deny, run.assets_enforced, run.waited))
        return run

    def transition(self, run: Run, state: str) -> Run:
        """更新执行中的状态，不写结束时间或耗时。"""
        if state not in (STATE_RUNNING, STATE_WAITING_WORKSPACE, STATE_WAITING_CAPACITY):
            raise ValueError(f'非运行中状态，请使用 finish: {state}')
        run.state = state
        with self._con() as con:
            con.execute('UPDATE runs SET state=?, waited=? WHERE run_id=?',
                        (state, run.waited, run.run_id))
        return run

    def finish(self, run: Run) -> Run:
        """收尾：写终态 + 耗时 + 成本 + 失败分类。"""
        if run.state not in (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED):
            raise ValueError(f'finish 只接收终态，收到 {run.state}')
        run.ended_at = run.ended_at or _now()
        if run.duration_ms is None:
            try:
                a = datetime.fromisoformat(run.started_at)
                b = datetime.fromisoformat(run.ended_at)
                run.duration_ms = int((b - a).total_seconds() * 1000)
            except ValueError:
                pass
        with self._con() as con:
            con.execute(
                "UPDATE runs SET state=?, fail_class=?, fail_code=?, error=?, ended_at=?,"
                " duration_ms=?, cost_usd=?, usage_json=?, output_chars=?, structured=?,"
                " degraded=?, log_path=?, waited=? WHERE run_id=?",
                (run.state, run.fail_class, run.fail_code, run.error, run.ended_at,
                 run.duration_ms, run.cost_usd,
                 json.dumps(run.usage, ensure_ascii=False) if run.usage else None,
                 run.output_chars,
                 None if run.structured is None else int(run.structured),
                 run.degraded, run.log_path, run.waited, run.run_id))
        return run

    # ── 读 ──
    def get(self, run_id: str) -> Optional[Run]:
        with self._con() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def query(self, *, agent: str = None, ref: str = None, state: str = None,
              since: str = None, failed_only: bool = False, limit: int = 100) -> List[Run]:
        """回头看。`since` 收 ISO 串（`2026-08-01` 这种前缀也行，字符串比较即可）。"""
        sql = "SELECT * FROM runs WHERE 1=1"
        args: list = []
        if agent:
            sql += " AND agent=?"; args.append(agent)
        if ref:
            sql += " AND ref=?"; args.append(ref)
        if state:
            sql += " AND state=?"; args.append(state)
        if failed_only:
            sql += " AND state=?"; args.append(STATE_FAILED)
        if since:
            sql += " AND started_at>=?"; args.append(since)
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._con() as con:
            return [_row_to_run(r) for r in con.execute(sql, args)]

    def stats(self, *, agent: str = None, since: str = None) -> List[dict]:
        """按 agent 汇总：跑了几次、成了几次、失败分布、总花费、平均耗时。

        这是「换个模型是不是更好」「这个月贵在哪」唯一能靠数字回答的地方。
        """
        # COALESCE 包一层：SUM 在「一条失败都没有」时返回 NULL，会渲染成 None 而不是 0
        sql = ("SELECT agent, COUNT(*) runs,"
               " COALESCE(SUM(state='completed'),0) ok,"
               " COALESCE(SUM(state='failed'),0) failed,"
               " COALESCE(SUM(state='failed' AND fail_class='platform'),0) platform_fail,"
               " COALESCE(SUM(state='failed' AND fail_class='agent'),0) agent_fail,"
               " CASE WHEN COUNT(cost_usd)=COUNT(*) THEN ROUND(SUM(cost_usd),4) END cost,"
               " ROUND(SUM(cost_usd),4) known_cost, COUNT(cost_usd) cost_known_runs,"
               " COUNT(*)-COUNT(cost_usd) cost_unknown_runs,"
               " COALESCE(SUM(state='cancelled'),0) cancelled,"
               " COALESCE(SUM(state IN ('running','waiting_workspace','waiting_capacity')),0) active,"
               " CAST(AVG(duration_ms) AS INTEGER) avg_ms"
               " FROM runs WHERE 1=1")
        args: list = []
        if agent:
            sql += " AND agent=?"; args.append(agent)
        if since:
            sql += " AND started_at>=?"; args.append(since)
        sql += " GROUP BY agent ORDER BY runs DESC"
        with self._con() as con:
            return [dict(r) for r in con.execute(sql, args)]

    def fail_breakdown(self, *, agent: str = None, since: str = None) -> List[dict]:
        """失败码分布 —— 「失败率 12%」没用，「12% 里 9% 是超时」才能动手。"""
        sql = ("SELECT fail_class, fail_code, COUNT(*) n FROM runs"
               " WHERE state='failed'")
        args: list = []
        if agent:
            sql += " AND agent=?"; args.append(agent)
        if since:
            sql += " AND started_at>=?"; args.append(since)
        sql += " GROUP BY fail_class, fail_code ORDER BY n DESC"
        with self._con() as con:
            return [dict(r) for r in con.execute(sql, args)]


def _row_to_run(row: sqlite3.Row) -> Run:
    d = dict(row)
    d.pop("id", None)
    usage = d.pop("usage_json", None)
    st = d.pop("structured", None)
    return Run(usage=json.loads(usage) if usage else None,
               structured=None if st is None else bool(st), **d)
