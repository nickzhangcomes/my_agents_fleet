"""盘点中发现的真实失效路径：权限、会话隔离、执行入口和落盘一致性。

只使用临时目录、假模型事件与本地测试进程，不调用真实模型。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from my_agents2 import Agent, Caps, ClaudeCodeAgent, Event, Result, RunHandle
from my_agents_fleet import (ContractError, FleetConfigError, JsonFileStore, RecordedAgent,
                       Run, RunStore, SessionState, Turn, classify, load_fleet)
from my_agents_fleet.capacity import Capacity
from my_agents_fleet.session import Session
from ._fake import AI_ROOT, FakeAgent


class ProbeAgent(Agent):
    caps = Caps(cwd=True, structured_output=True)

    def __init__(self, events=None, **kw):
        super().__init__(**kw)
        self.script = events if events is not None else [Event('result', text='done')]
        self.calls = []
        self.closed = []

    def stream(self, prompt, *, continuation=None, handle=None):
        self.calls.append(continuation)
        try:
            yield from self.script
        finally:
            self.closed.append(True)


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agents-fleet-regression-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seq = 0

    def fleet(self, agents=None, defaults=None):
        self.seq += 1
        d = self.root / f'fleet-{self.seq}'
        d.mkdir()
        config = {'version': 2, 'global': {'defaults': {
            'backend': 'claude', 'max_attempts': 1, **(defaults or {})}},
            'agents': agents or {'甲': {}}}
        (d / 'fleet.json').write_text(json.dumps(config, ensure_ascii=False))
        return load_fleet(d)

    def wrap(self, agent, **kw):
        self.seq += 1
        store = RunStore(self.root / f'run-{self.seq}.db')
        return RecordedAgent(agent, store=store, name='probe', backend='fake',
                             max_attempts=kw.pop('max_attempts', 1), **kw), store

    def patch_fleet(self, fleet, fakes):
        real = fleet.get
        def get(name, **kwargs):
            ag = real(name, **kwargs)
            ag._agent = fakes[name]
            return ag
        fleet.get = get


class TestManagedContract(Fixture):
    def test_extra_cannot_replace_contract_mode_or_deny(self):
        for extra in ({'mode': 'full'}, {'deny': []}, {'output_schema': {}}, {'cwd': '/tmp'}):
            with self.subTest(extra=extra), self.assertRaises(FleetConfigError):
                self.fleet({'甲': {'trust': 'untrusted', 'extra': extra}}).get('甲')

    def test_native_permission_overrides_rejected(self):
        for extra in ({'permission_mode': 'bypassPermissions'},
                      {'extra_args': ['--permission-mode', 'bypassPermissions']},
                      {'allowed_tools': ['Write']}, {'claude_bin': '/tmp/custom'}):
            with self.subTest(extra=extra), self.assertRaises(ContractError):
                self.fleet({'甲': {'trust': 'untrusted', 'extra': extra}}).get('甲')

    def test_untrusted_disables_inherited_mcp(self):
        ag = self.fleet({'甲': {'trust': 'untrusted'}}).get('甲')
        argv = ag.raw._build_argv('probe', None)
        self.assertIn('--strict-mcp-config', argv)
        i = argv.index('--mcp-config')
        self.assertEqual(json.loads(argv[i + 1]), {'mcpServers': {}})
        self.assertEqual(ag.raw.mode, 'readonly')

    def test_untrusted_rejects_explicit_mcp(self):
        with self.assertRaises(ContractError):
            self.fleet({'甲': {'trust': 'untrusted', 'extra': {'mcp_config': '/tmp/tools.json'}}}).get('甲')

    def test_schema_unsupported_is_rejected_before_execution(self):
        for backend in ('grok', 'pi'):
            with self.subTest(backend=backend), self.assertRaises(FleetConfigError):
                self.fleet({'甲': {'backend': backend, 'output_schema': {'type': 'object'}}}).get('甲')

    def test_plan_mode_is_not_marked_hard_readonly(self):
        ag = self.fleet({'甲': {'backend': 'agy', 'assets': {'资料': '.'}}}).get('甲')
        self.assertEqual(ag.contract.assets_enforced, 'soft')
        self.assertIn('计划模式', ag.raw.system_prompt)

    def test_top_level_deny_is_enforced_or_rejected(self):
        ag = self.fleet({'甲': {'deny': ['network']}}).get('甲')
        self.assertIn('network', ag.raw.deny)
        with self.assertRaises(ContractError):
            self.fleet({'甲': {'backend': 'pi', 'deny': ['network']}}).get('甲')

    def test_permission_model_and_identity_changes_change_fingerprint(self):
        f = self.fleet()
        first = f.get('甲')
        for kw in ({'mode': 'readonly'}, {'model': 'claude-opus-4-6'}, {'intro': 'new role'}, {'deny': ['network']}):
            with self.subTest(kw=kw):
                self.assertNotEqual(first.fingerprint, f.get('甲', **kw).fingerprint)

    def test_missing_fingerprint_is_cold_start(self):
        store = JsonFileStore(self.root / 'sessions')
        state = SessionState('old', '甲', continuation='legacy')
        self.assertFalse(Session(None, '甲', 'old', store)._gate(state, 'new'))

    def test_all_five_cli_backends_still_construct(self):
        for name in ('claude', 'codex', 'grok', 'agy', 'pi'):
            with self.subTest(name=name):
                ag = self.fleet({'甲': {'backend': name}}).get('甲')
                self.assertEqual(ag.raw.kind, 'cli')


class TestSessionIsolation(Fixture):
    def test_chinese_ids_do_not_collide(self):
        store = JsonFileStore(self.root / 'sessions')
        for sid in ('选题一', '选题二', 'a/b', 'a:b', 'x' * 140 + '1', 'x' * 140 + '2'):
            store.save(SessionState(sid, '甲', turns=[Turn(text=sid)]))
        for sid in ('选题一', '选题二', 'a/b', 'a:b', 'x' * 140 + '1', 'x' * 140 + '2'):
            self.assertEqual(store.load(sid).turns[0].text, sid)

    def test_same_id_is_isolated_by_agent(self):
        f = self.fleet({'甲': {}, '乙': {}})
        fakes = {n: FakeAgent([Result(text=n, continuation=n)]) for n in ('甲', '乙')}
        self.patch_fleet(f, fakes)
        f.session('甲', 'same').ask('A_ONLY')
        f.session('乙', 'same').ask('B_ONLY')
        self.assertNotIn('A_ONLY', fakes['乙'].prompts[0])
        self.assertEqual(f.session('甲', 'same').history()[0].prompt, 'A_ONLY')
        self.assertEqual(f.session('乙', 'same').history()[0].prompt, 'B_ONLY')

    def test_legacy_history_is_read_only_when_full_identity_matches(self):
        root = self.root / 'sessions'; root.mkdir()
        legacy = root / 'old.json'
        state = SessionState('old', '甲', continuation='c', turns=[Turn(text='kept')])
        legacy.write_text(json.dumps(state.to_dict()))
        store = JsonFileStore(root)
        self.assertEqual(store.for_agent('甲').load('old').turns[0].text, 'kept')
        self.assertIsNone(store.for_agent('乙').load('old'))
        store.for_agent('甲').save(state)
        self.assertTrue(legacy.exists())
        self.assertEqual(len(list(root.glob('*.json'))), 2)

    def test_legacy_sanitized_collision_is_not_adopted(self):
        store = JsonFileStore(self.root / 'sessions')
        state = SessionState('选题一', '甲', turns=[Turn(text='first')])
        (store.root / 'item.json').write_text(json.dumps(state.to_dict()))
        self.assertIsNone(store.for_agent('甲').load('选题二'))

    def test_concurrent_turns_keep_both_and_resume_in_order(self):
        f = self.fleet()
        fake = FakeAgent([Result(text='a', continuation='c1'), Result(text='b', continuation='c2')], delay=.05)
        self.patch_fleet(f, {'甲': fake})
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda p: f.session('甲', 'thread').ask(p), ['first', 'second']))
        self.assertEqual(len(f.session('甲', 'thread').history()), 2)
        self.assertEqual(fake.continuations, [None, 'c1'])

    def test_session_transaction_is_cross_process(self):
        store = JsonFileStore(self.root / 'sessions').for_agent('甲')
        store.save(SessionState('thread', '甲'))
        code = '''import sys,time
sys.path.insert(0,sys.argv[1])
from my_agents_fleet import JsonFileStore,Turn
s=JsonFileStore(sys.argv[2]).for_agent('甲')
with s.transaction('thread'):
 state=s.load('thread');time.sleep(.05)
 state.turns.append(Turn(prompt=sys.argv[3]));s.save(state)
'''
        procs = [subprocess.Popen([sys.executable, '-B', '-c', code, str(AI_ROOT), str(store.root), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(3)]
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(len(store.load('thread').turns), 3)

    def test_corrupt_history_is_not_silently_overwritten(self):
        store = JsonFileStore(self.root / 'sessions').for_agent('甲')
        p = store._path('bad');p.write_text('{broken')
        with self.assertRaises(ValueError):
            store.load('bad')
        self.assertEqual(p.read_text(), '{broken')

    def test_stale_recovery_keeps_all_attempt_costs(self):
        f = self.fleet()
        raw = FakeAgent([Result(text='first', continuation='old', cost_usd=0),
                         Result(ok=False, error='session not found: old', cost_usd=.1),
                         Result(text='recovered', continuation='new', cost_usd=.2)])
        self.patch_fleet(f, {'甲': raw})
        session = f.session('甲', 'cost')
        session.ask('first')
        out = session.ask('second')
        self.assertTrue(out.ok)
        self.assertEqual(len(out.attempts), 2)
        self.assertAlmostEqual(out.cost_usd, .3)


class TestExecutionEntrypoints(Fixture):
    def test_stream_honors_concurrency_limit(self):
        active = peak = 0
        guard = threading.Lock()
        class Slow(ProbeAgent):
            def stream(self, prompt, **kw):
                nonlocal active, peak
                with guard:
                    active += 1; peak = max(active, peak)
                try:
                    time.sleep(.05)
                    yield Event('result', text='done')
                finally:
                    with guard: active -= 1
        ag, store = self.wrap(Slow(), capacity=Capacity(self.root / 'locks'), max_concurrent=1)
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: list(ag.stream('probe')), range(4)))
        self.assertEqual(peak, 1)
        self.assertTrue(any(r.waited == 'capacity' for r in store.query()))

    def test_stream_honors_workspace_lock(self):
        cap = Capacity(self.root / 'locks')
        raw = ProbeAgent()
        ag, store = self.wrap(raw, capacity=cap, workspace=str(self.root), workspace_timeout=0)
        with cap.workspace(str(self.root)):
            events = list(ag.stream('probe'))
        self.assertEqual(raw.calls, [])
        self.assertEqual(events[0].kind, 'error')
        self.assertEqual(store.query()[0].fail_code, 'workspace_busy')

    def test_stream_result_error_cannot_be_success(self):
        ag, store = self.wrap(ProbeAgent([Event('result', text='failed', raw={'is_error': True})]))
        list(ag.stream('probe'))
        self.assertEqual(store.query()[0].state, 'failed')

    def test_stream_timeout_uses_same_classifier(self):
        ag, store = self.wrap(ProbeAgent([Event('error', text='killed', raw={'timed_out': True})]))
        list(ag.stream('probe'))
        self.assertEqual(store.query()[0].fail_code, 'timeout')

    def test_close_cancels_and_closes_underlying_generator(self):
        raw = ProbeAgent([Event('text', text='partial'), Event('result', text='done')])
        ag, store = self.wrap(raw)
        stream = ag.stream('probe');next(stream);stream.close()
        row = store.query()[0]
        self.assertEqual((row.state, row.fail_code), ('cancelled', 'cancelled'))
        self.assertIsNotNone(row.ended_at)
        self.assertTrue(raw.closed)

    def test_stream_without_final_result_is_failed(self):
        ag, store = self.wrap(ProbeAgent([Event('text', text='incomplete')]))
        list(ag.stream('probe'))
        self.assertEqual(store.query()[0].state, 'failed')

    def test_stream_json_text_is_recognized_as_structured(self):
        ag, store = self.wrap(ProbeAgent([Event('result', text='{"answer":42}')], output_schema={'type': 'object'}))
        list(ag.stream('probe'))
        self.assertTrue(store.query()[0].structured)
        self.assertEqual(store.query()[0].state, 'completed')

    def test_conversation_and_task_calls_are_recorded(self):
        class Writer(ProbeAgent):
            def stream(self, prompt, *, continuation=None, handle=None):
                self.calls.append(continuation)
                p = Path(self.cwd) / f'output-{len(self.calls)}.txt';p.write_text('done')
                yield Event('tool_use', name='Write', command=str(p))
                yield Event('result', text='done', raw={'continuation': 'c'})
        ag, store = self.wrap(Writer(cwd=str(self.root)))
        ag.conversation().ask('chat')
        task = ag.task(workspace=str(self.root))
        first = task.run('write');second = task.run('revise')
        self.assertEqual(len(store.query()), 3)
        self.assertEqual(first.status, 'done')
        self.assertEqual(second.continuation, 'c')
        self.assertEqual(len(task.all_artifacts), 2)
        self.assertTrue(first.run.log_path)

    def test_run_keeps_structured_result(self):
        ag, store = self.wrap(ProbeAgent([Event('result', text='{"answer":42}', raw={'structured': {'answer': 42}})],
                                        output_schema={'type': 'object'}))
        out = ag.run('probe')
        self.assertEqual(out.structured, {'answer': 42})
        self.assertTrue(store.query()[0].structured)

    def test_run_classified_failure_updates_task_status(self):
        ag, store = self.wrap(ProbeAgent([Event('result', text='Not logged in')]))
        out = ag.run('probe')
        self.assertFalse(out.ok)
        self.assertEqual(out.status, 'failed')
        self.assertEqual(out.failure.code, 'not_logged_in')

    def test_run_uses_requested_workspace_lock(self):
        cap = Capacity(self.root / 'locks')
        raw = ProbeAgent(cwd=str(self.root))
        target = self.root / 'output'
        ag, store = self.wrap(raw, capacity=cap, workspace=str(self.root))
        with cap.workspace(str(target)):
            out = ag.run('probe', workspace=str(target))
        self.assertEqual(out.failure.code, 'workspace_busy')
        self.assertEqual(raw.calls, [])
        self.assertFalse(target.exists())

    def test_stop_before_run_returns_task_fields(self):
        ag, store = self.wrap(ProbeAgent())
        handle = RunHandle();handle.stop()
        out = ag.run('probe', handle=handle)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, 'stopped')
        self.assertEqual(store.query()[0].state, 'cancelled')

    def test_stop_while_waiting_does_not_start_agent(self):
        cap = Capacity(self.root / 'locks');handle = RunHandle();raw = ProbeAgent()
        ag, store = self.wrap(raw, capacity=cap, max_concurrent=1)
        with cap.slot('probe', 1), ThreadPoolExecutor(1) as pool:
            future = pool.submit(ag.ask, 'probe', handle=handle)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = store.query()
                if rows and rows[0].state == 'waiting_capacity':break
                time.sleep(.01)
            handle.stop()
            out = future.result(timeout=2)
        self.assertEqual(out.failure.code, 'cancelled')
        self.assertEqual(raw.calls, [])

    def test_generator_close_reaps_actual_test_cli(self):
        script = self.root / 'fake-claude'
        pidfile = self.root / 'pid'
        script.write_text('#!/usr/bin/env python3\nimport os,json,time\n'
                          + f'open({str(pidfile)!r},"w").write(str(os.getpid()))\n'
                          + 'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"ready"}]}}),flush=True)\n'
                          + 'time.sleep(15)\n')
        script.chmod(0o700)
        raw = ClaudeCodeAgent(claude_bin=str(script), cwd=str(self.root), timeout=5)
        ag, store = self.wrap(raw)
        stream = ag.stream('probe')
        try:
            for event in stream:
                if event.kind == 'text':break
        finally:
            stream.close()
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid, 0)
        self.assertEqual(store.query()[0].state, 'cancelled')


class TestRosterAndLedger(Fixture):
    def test_invalid_roster_change_preserves_original(self):
        f = self.fleet();p = f.fleet_dir / 'fleet.json';before = p.read_bytes()
        for entry in ({'backend': 'missing'}, {'instructions_file': 'missing.md'},
                      {'backend': 'pi', 'output_schema': {}}, {'max_attempts': 0},
                      {'max_concurrent': 1.5}, {'max_attempts': float('inf')},
                      {'timeout': float('nan')}, {'workspace_timeout': float('inf')}):
            with self.subTest(entry=entry), self.assertRaises((ValueError, ContractError)):
                f.upsert_agent('bad', entry)
            self.assertEqual(p.read_bytes(), before)
            self.assertEqual(load_fleet(f.fleet_dir).names(), ['甲'])

    def test_valid_roster_update_refreshes_current_instance(self):
        f = self.fleet();f.upsert_agent('乙', {})
        self.assertIn('乙', f.names())
        f.remove_agent('乙');self.assertNotIn('乙', f.names())

    def test_concurrent_roster_changes_do_not_overwrite_each_other(self):
        f = self.fleet()
        code = '''import sys
sys.path.insert(0,sys.argv[1])
from my_agents_fleet import load_fleet
load_fleet(sys.argv[2]).upsert_agent(sys.argv[3],{})
'''
        procs = [subprocess.Popen([sys.executable, '-B', '-c', code, str(AI_ROOT), str(f.fleet_dir), f'role-{i}'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(len(load_fleet(f.fleet_dir).names()), 5)
        self.assertEqual(list(f.fleet_dir.glob('*.tmp')), [])

    def test_batch_configuration_error_is_persisted(self):
        f = self.fleet()
        outs = f.dispatch([{'agent': 'missing', 'prompt': 'probe'}, {'agent': '甲'}])
        self.assertTrue(all(not o.ok for o in outs))
        self.assertEqual(len(f.history()), 2)
        self.assertEqual({r.fail_code for r in f.history()}, {'config_error'})

    def test_waiting_state_does_not_end_the_run(self):
        store = RunStore(self.root / 'time.db')
        r = store.begin(Run(run_id='clock', agent='a', started_at='2026-09-07T10:00:00+08:00'))
        with patch('my_agents_fleet.runs._now', return_value='2026-09-07T10:00:01+08:00'):
            store.transition(r, 'waiting_workspace')
            self.assertIsNone(store.get('clock').ended_at)
            store.transition(r, 'running')
        with patch('my_agents_fleet.runs._now', return_value='2026-09-07T10:00:09+08:00'):
            r.state = 'completed';store.finish(r)
        self.assertEqual(r.duration_ms, 9000)

    def test_unknown_cost_is_not_zero(self):
        ag, store = self.wrap(FakeAgent([Result(text='ok', cost_usd=None)]))
        out = ag.ask('probe')
        self.assertIsNone(out.cost_usd)
        stats = store.stats()[0]
        self.assertIsNone(stats['cost'])
        self.assertEqual(stats['cost_unknown_runs'], 1)

    def test_zero_cost_is_preserved(self):
        ag, store = self.wrap(FakeAgent([Result(text='ok', cost_usd=0)]))
        self.assertEqual(ag.ask('probe').cost_usd, 0)
        self.assertEqual(store.stats()[0]['cost'], 0)

    def test_cancelled_is_separate_from_platform_failures(self):
        ag, store = self.wrap(ProbeAgent())
        handle = RunHandle();handle.stop()
        ag.ask('probe', handle=handle)
        stats = store.stats()[0]
        self.assertEqual(stats['cancelled'], 1)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(stats['platform_fail'], 0)

    def test_partial_cost_is_reported_as_lower_bound(self):
        ag, store = self.wrap(FakeAgent([Result(ok=False, timed_out=True, cost_usd=.2),
                                        Result(text='ok', cost_usd=None)]),
                              max_attempts=2, sleep=lambda _: None)
        out = ag.ask('probe')
        self.assertIsNone(out.cost_usd)
        self.assertEqual(out.known_cost_usd, .2)
        self.assertEqual(store.stats()[0]['known_cost'], .2)

    def test_normal_rate_limit_discussion_does_not_retry(self):
        for text in ('The rate limit configuration is correct.', 'Rate limit configuration is correct.',
                     'Here is how to handle usage limit reached errors.'):
            with self.subTest(text=text):self.assertIsNone(classify(Result(text=text)))

    def test_relative_lock_path_uses_fleet_directory(self):
        f = self.fleet({'甲': {'lock_workspace': './shared'}})
        self.assertEqual(f.get('甲').workspace, str((f.fleet_dir / 'shared').resolve()))


if __name__ == '__main__':
    unittest.main()
