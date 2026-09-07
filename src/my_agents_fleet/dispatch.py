"""执行治理：所有执行入口共用并发闸门、目录锁、失败判定与 Run 收尾。

ask 按失败类别重试；stream 与 run 不自动重跑已返回事件或已落盘的任务。
显式 raw 才绕过这一层。普通调用只记元数据，文件任务沿用底层的日志路径。
"""
from __future__ import annotations

import copy
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from my_agents2 import Conversation, Event, Result, TaskResult, TaskSession

from . import failures as F
from .capacity import RunCancelled, WorkspaceBusy
from .runs import (Run, RunStore, STATE_CANCELLED, STATE_COMPLETED, STATE_FAILED,
                   STATE_RUNNING, STATE_WAITING_CAPACITY, STATE_WAITING_WORKSPACE,
                   new_run_id)


@dataclass
class Outcome:
    """执行结果 + 每次尝试的记录；任务结果的 status/artifacts 等字段继续可访问。"""
    result: object
    run: Run
    failure: Optional[F.Failure] = None
    attempts: List[Run] = field(default_factory=list)

    def __getattr__(self, item):
        return getattr(object.__getattribute__(self, 'result'), item)

    @property
    def ok(self):
        return self.failure is None

    @property
    def text(self):
        return getattr(self.result, 'text', '') or ''

    @property
    def structured(self):
        return getattr(self.result, 'structured', None)

    @property
    def continuation(self):
        return getattr(self.result, 'continuation', None)

    @property
    def cost_usd(self):
        """完整成本；任一尝试成本未知时返回 None，真实零值保留为 0。"""
        if not self.attempts or any(r.cost_usd is None for r in self.attempts):
            return None
        return sum(r.cost_usd for r in self.attempts)

    @property
    def known_cost_usd(self):
        """已知部分之和，用作成本下界；没有任何已知值时返回 None。"""
        values = [r.cost_usd for r in self.attempts if r.cost_usd is not None]
        return sum(values) if values else None

    @property
    def error(self):
        return str(self.failure) if self.failure else ''

    def __repr__(self):
        status = 'ok' if self.ok else self.failure.code
        return f'<Outcome {self.run.agent} {status} attempts={len(self.attempts)}>'


class RecordedAgent:
    """包住本地 CLI Agent。ask/stream/run/conversation/task 均经过执行治理。"""

    def __init__(self, agent, *, store: RunStore, name: str, backend: str,
                 fingerprint: str = None, identity_mode: str = None,
                 session_id: str = None, max_attempts: int = 3,
                 contract=None, capacity=None, max_concurrent: int = 0,
                 workspace: str = None, workspace_timeout: float = 0.0,
                 sleep=time.sleep, logger=None):
        self._agent = agent
        self._store = store
        self.name = name
        self.backend = backend
        self.fingerprint = fingerprint
        self.identity_mode = identity_mode
        self.session_id = session_id
        self.max_attempts = max(1, int(max_attempts))
        self.contract = contract
        self._cap = capacity
        self.max_concurrent = int(max_concurrent or 0)
        self.workspace = workspace
        self.workspace_timeout = float(workspace_timeout or 0.0)
        self._sleep = sleep
        self._log = logger or (lambda m: None)

    def __getattr__(self, item):
        return getattr(object.__getattribute__(self, '_agent'), item)

    @property
    def raw(self):
        """显式取得底层 Agent，自行承担记录与并发控制。"""
        return self._agent

    def _new_run(self, *, prompt, attempt, ref, trigger):
        ag, c = self._agent, self.contract
        return Run(
            run_id=new_run_id(self.name), agent=self.name, ref=ref, trigger=trigger,
            backend=self.backend, model=getattr(ag, 'model', None),
            effort=getattr(ag, 'effort', None), session_id=self.session_id,
            attempt=attempt, prompt_chars=len(prompt or ''),
            identity_mode=self.identity_mode, fingerprint=self.fingerprint,
            degraded='; '.join(getattr(ag, 'ignored', ()) or ()) or None,
            trust=getattr(c, 'trust', None),
            deny=','.join(getattr(c, 'deny', ()) or ()) or None,
            assets_enforced=getattr(c, 'assets_enforced', None))

    def _waiting(self, run, label, state):
        labels = (run.waited or '').split(',') if run.waited else []
        if label not in labels:
            labels.append(label)
        run.waited = ','.join(labels)
        self._store.transition(run, state)

    @contextmanager
    def _gate(self, run, handle=None):
        if handle is not None and handle.stopped:
            raise RunCancelled('开始执行前被停止')
        if self._cap is None:
            yield
            return
        with self._cap.slot(
                self.name, self.max_concurrent, handle=handle,
                on_wait=lambda: self._waiting(run, 'capacity', STATE_WAITING_CAPACITY)):
            with self._cap.workspace(
                    self.workspace, timeout=self.workspace_timeout, handle=handle,
                    on_wait=lambda: self._waiting(run, 'workspace', STATE_WAITING_WORKSPACE)):
                if run.state != STATE_RUNNING:
                    self._store.transition(run, STATE_RUNNING)
                yield

    def _parse_structured(self, res):
        if (getattr(self._agent, 'output_schema', None) is not None
                and getattr(res, 'structured', None) is None):
            try:
                value = json.loads((getattr(res, 'text', '') or '').strip())
                if isinstance(value, dict):
                    res.structured = value
            except (ValueError, TypeError):
                pass

    def _close(self, run, res, failure):
        if failure and isinstance(res, TaskResult):
            res.ok = False
            if failure.code == 'cancelled':
                res.status = 'stopped'
            elif res.status == 'done':
                res.status = 'failed'
        run.cost_usd = getattr(res, 'cost_usd', None)
        run.usage = getattr(res, 'usage', None)
        run.output_chars = len(getattr(res, 'text', '') or '')
        run.log_path = getattr(res, 'log_path', None)
        if getattr(self._agent, 'output_schema', None) is not None:
            run.structured = getattr(res, 'structured', None) is not None
        run.state = STATE_COMPLETED if failure is None else (
            STATE_CANCELLED if failure.code == 'cancelled' else STATE_FAILED)
        if failure:
            run.fail_class, run.fail_code = failure.cls, failure.code
            run.error = failure.detail or failure.kind.hint
        return self._store.finish(run)

    def _execute(self, method, prompt, *, continuation=None, ref=None, trigger=None,
                 handle=None, retry=False, **kwargs):
        expect = getattr(self._agent, 'output_schema', None) is not None
        attempts = []
        res = failure = None
        limit = self.max_attempts if retry else 1
        for attempt in range(1, limit + 1):
            res = failure = None
            run = self._store.begin(self._new_run(
                prompt=prompt, attempt=attempt, ref=ref, trigger=trigger))
            try:
                with self._gate(run, handle):
                    call_kw = dict(kwargs, continuation=continuation)
                    if handle is not None:
                        call_kw['handle'] = handle
                    res = getattr(self._agent, method)(prompt, **call_kw)
                self._parse_structured(res)
                failure = (F.Failure(F.KINDS['cancelled'], '调用方停止')
                           if handle is not None and handle.stopped else
                           F.classify(res, expect_schema=expect))
            except (WorkspaceBusy, RunCancelled) as e:
                code = 'cancelled' if isinstance(e, RunCancelled) else 'workspace_busy'
                failure = F.Failure(F.KINDS[code], str(e)[:200])
                if method == 'run' and res is None:
                    res = TaskResult(ok=False, error=str(e),
                                     status='stopped' if code == 'cancelled' else 'failed',
                                     workspace=kwargs.get('workspace'))
            except BaseException as e:
                code = 'cancelled' if isinstance(e, (KeyboardInterrupt, GeneratorExit)) else 'backend_crashed'
                failure = F.Failure(F.KINDS[code], f'{type(e).__name__}: {e}'[:400])
                attempts.append(self._close(run, res, failure))
                e.run = run
                raise
            attempts.append(self._close(run, res, failure))
            if failure is None or not failure.retry or attempt == limit:
                break
            delay = F.retry_delay(failure, attempt)
            self._log(f'[{self.name}] {failure.code} → {delay:.0f}s 后重试（第 {attempt + 1} 次）')
            if handle is None or self._sleep is not time.sleep:
                self._sleep(delay)
            else:
                # 等待额度恢复期间也能响应停止。
                deadline = time.monotonic() + delay
                while not handle.stopped and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return Outcome(result=res, run=attempts[-1], failure=failure, attempts=attempts)

    def ask(self, prompt, *, continuation=None, ref=None, trigger=None, handle=None):
        return self._execute('ask', prompt, continuation=continuation, ref=ref,
                             trigger=trigger, handle=handle, retry=True)

    def run(self, brief, *, workspace=None, context_dirs=(), continuation=None,
            handle=None, keep_events=False, ref=None, trigger=None):
        """文件任务也留痕；不自动重试已落盘的任务，返回 Outcome 并透传任务字段。"""
        runner = self
        if workspace:
            runner = copy.copy(self)
            ws = os.path.realpath(workspace)
            if self.workspace and os.path.realpath(self.workspace) == os.path.realpath(self._agent.cwd):
                runner.workspace = ws
            from .identity import fingerprint
            runner.fingerprint = fingerprint(agent=self.name, backend=self.backend, cwd=ws,
                                             extra_dirs=context_dirs,
                                             settings={'parent': self.fingerprint})
        return runner._execute('run', brief, workspace=workspace, context_dirs=context_dirs,
                               continuation=continuation, handle=handle, keep_events=keep_events,
                               ref=ref, trigger=trigger)

    def conversation(self):
        return Conversation(self)

    def task(self, *, workspace=None, context_dirs=()):
        return TaskSession(self, workspace=workspace, context_dirs=context_dirs)

    def stream(self, prompt, *, continuation=None, ref=None, trigger=None, handle=None) -> Iterator:
        """流式输出与 ask 共用闸门和失败分类；关闭生成器时终止底层流并记取消。"""
        run = self._store.begin(self._new_run(prompt=prompt, attempt=1, ref=ref, trigger=trigger))
        res = Result()
        parts = []
        saw_result = False
        failure = None
        source = None
        try:
            with self._gate(run, handle):
                kw = {'continuation': continuation}
                if handle is not None:
                    kw['handle'] = handle
                source = self._agent.stream(prompt, **kw)
                try:
                    for ev in source:
                        if ev.kind == 'text':
                            parts.append(ev.text or '')
                        elif ev.kind == 'result':
                            saw_result = True
                            r = ev.raw or {}
                            res.text = ev.text or ''
                            res.continuation = r.get('continuation') or res.continuation
                            res.cost_usd = r.get('cost_usd', res.cost_usd)
                            res.usage = r.get('usage', res.usage)
                            res.structured = r.get('structured', res.structured)
                            if r.get('is_error'):
                                res.ok = False
                        elif ev.kind == 'error':
                            res.ok = False
                            res.error = ev.text or res.error
                            res.timed_out |= bool((ev.raw or {}).get('timed_out'))
                            res.events.append(ev)
                        yield ev
                finally:
                    if source is not None and hasattr(source, 'close'):
                        source.close()
        except (GeneratorExit, KeyboardInterrupt):
            if handle is not None:
                handle.stop()
            failure = F.Failure(F.KINDS['cancelled'], '调用方关闭输出流')
            raise
        except (RunCancelled, WorkspaceBusy) as e:
            code = 'cancelled' if isinstance(e, RunCancelled) else 'workspace_busy'
            failure = F.Failure(F.KINDS[code], str(e)[:200])
            yield Event(kind='error', text=str(e), raw={'stopped': code == 'cancelled'})
        except BaseException as e:
            failure = F.Failure(F.KINDS['backend_crashed'], f'{type(e).__name__}: {e}'[:400])
            e.run = run
            raise
        finally:
            if not saw_result:
                res.text = ''.join(parts)
            self._parse_structured(res)
            if failure is None:
                if handle is not None and handle.stopped:
                    failure = F.Failure(F.KINDS['cancelled'], '调用方停止')
                else:
                    failure = F.classify(res, expect_schema=getattr(self._agent, 'output_schema', None) is not None)
                    if failure is None and not saw_result:
                        failure = F.Failure(F.KINDS['backend_crashed'], '输出流结束但没有最终结果事件')
            self._close(run, res, failure)
