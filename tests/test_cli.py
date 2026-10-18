"""CLI behavior that must work offline: doctor, sessions, runs, task input, log routing."""
import contextlib
import filecmp
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from my_agents_fleet import load_fleet
from my_agents_fleet.cli import main
from my_agents_fleet.runs import Run, new_run_id
from my_agents_fleet.store import JsonFileStore, SessionState, Turn

REPO = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fleet_dir = Path(self.temp.name) / 'fleet'
        with contextlib.redirect_stdout(io.StringIO()):
            main(['init', str(self.fleet_dir)])
        offline = patch('subprocess.Popen', side_effect=AssertionError('CLI must remain offline'))
        offline.start()
        self.addCleanup(offline.stop)

    def invoke(self, *args, code=0, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with patch('sys.stdin', io.StringIO(stdin)):
                    result = self._call(args)
            else:
                result = self._call(args)
        self.assertEqual(result, code, err.getvalue())
        return out.getvalue()

    @staticmethod
    def _call(args):
        try:
            return main(list(args))
        except SystemExit as exc:
            return exc.code

    def edit_config(self, change):
        path = self.fleet_dir / 'fleet.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        change(data)
        path.write_text(json.dumps(data), encoding='utf-8')

    # ── doctor ──

    def test_doctor_reports_missing_command(self):
        with patch('shutil.which', return_value=None):
            output = self.invoke('doctor', str(self.fleet_dir), code=1)
        self.assertIn('找不到命令 claude', output)
        self.assertIn('2 个 agent 当前无法使用', output)

    def test_doctor_passes_when_commands_exist(self):
        with patch('shutil.which', side_effect=lambda name: f'/usr/bin/{name}'):
            output = self.invoke('doctor', str(self.fleet_dir))
        self.assertIn('reviewer', output)
        self.assertIn('工具白名单', output)

    def test_doctor_flags_incompatible_backend(self):
        self.edit_config(lambda d: d['agents']['summarizer'].update(backend='grok'))
        with patch('shutil.which', side_effect=lambda name: f'/usr/bin/{name}'):
            output = self.invoke('doctor', str(self.fleet_dir), code=1)
        self.assertIn('配置不可用', output)
        self.assertIn('summarizer', output)

    def test_doctor_without_directory_checks_tools_only(self):
        with patch('shutil.which', return_value=None):
            output = self.invoke('doctor', code=1)
        self.assertIn('没有找到任何可用的 agent 工具', output)

    # ── run input ──

    def fake_get(self, captured):
        def get(fleet, name, **kwargs):
            def ask(prompt, **kw):
                captured.append(prompt)
                return SimpleNamespace(ok=True, text='done', structured=None, failure=None)
            return SimpleNamespace(ask=ask)
        return get

    def test_run_reads_prompt_from_file(self):
        task = Path(self.temp.name) / 'task.md'
        task.write_text('第一行\n第二行', encoding='utf-8')
        captured = []
        with patch('my_agents_fleet.fleet.Fleet.get', self.fake_get(captured)):
            self.invoke('run', str(self.fleet_dir), 'reviewer', '--file', str(task))
        self.assertEqual(captured, ['第一行\n第二行'])

    def test_run_reads_prompt_from_stdin(self):
        captured = []
        with patch('my_agents_fleet.fleet.Fleet.get', self.fake_get(captured)):
            self.invoke('run', str(self.fleet_dir), 'reviewer', '-', stdin='来自管道的任务')
        self.assertEqual(captured, ['来自管道的任务'])

    def test_run_requires_exactly_one_prompt_source(self):
        self.invoke('run', str(self.fleet_dir), 'reviewer', code=2)
        task = Path(self.temp.name) / 'task.md'
        task.write_text('x', encoding='utf-8')
        self.invoke('run', str(self.fleet_dir), 'reviewer', 'inline', '--file', str(task), code=2)

    def test_retry_messages_go_to_stderr(self):
        from my_agents_fleet import fleet as fleet_module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fleet_module._log_stderr('[reviewer] timeout → 2s 后重试')
        self.assertEqual(out.getvalue(), '')
        self.assertIn('重试', err.getvalue())

    # ── runs ──

    def test_runs_table_and_json(self):
        fleet = load_fleet(self.fleet_dir)
        run = fleet.runs.begin(Run(run_id=new_run_id('reviewer'), agent='reviewer', state='running'))
        run.state, run.cost_usd = 'completed', 0.5
        fleet.runs.finish(run)
        table = self.invoke('runs', str(self.fleet_dir))
        self.assertIn('reviewer', table)
        self.assertIn('$0.5000', table)
        data = json.loads(self.invoke('runs', str(self.fleet_dir), '--json'))
        self.assertEqual(data['stats'][0]['runs'], 1)

    def test_runs_table_marks_unknown_cost(self):
        fleet = load_fleet(self.fleet_dir)
        run = fleet.runs.begin(Run(run_id=new_run_id('reviewer'), agent='reviewer', state='running'))
        run.state = 'completed'
        fleet.runs.finish(run)
        self.assertIn('未知', self.invoke('runs', str(self.fleet_dir)))

    # ── sessions ──

    def test_sessions_list_show_and_reset(self):
        fleet = load_fleet(self.fleet_dir)
        store = JsonFileStore(fleet.sessions_dir).for_agent('reviewer')
        store.save(SessionState(session_id='review-1', agent_name='reviewer',
                                turns=[Turn('现状？', '只完成了导入。')]))
        listing = self.invoke('sessions', str(self.fleet_dir))
        self.assertIn('review-1', listing)
        shown = self.invoke('sessions', str(self.fleet_dir), 'reviewer', 'review-1')
        self.assertIn('只完成了导入。', shown)
        self.invoke('sessions', str(self.fleet_dir), 'reviewer', 'review-1', '--reset')
        self.assertEqual(store.load('review-1').turns, [])
        self.assertIn('还没有保存的会话', self.invoke('sessions', str(self.fleet_dir)))

    def test_sessions_rejects_unknown_agent(self):
        self.invoke('sessions', str(self.fleet_dir), 'nobody', 's1', code=2)


class TemplateTests(unittest.TestCase):
    def test_example_matches_packaged_template(self):
        """examples/basic_fleet 是模板的公开副本，两份必须保持一致。"""
        diff = filecmp.dircmp(REPO / 'examples' / 'basic_fleet',
                              REPO / 'src' / 'my_agents_fleet' / 'templates' / 'basic')

        def differences(d):
            found = d.left_only + d.right_only + d.diff_files
            for sub in d.subdirs.values():
                found += differences(sub)
            return found

        self.assertEqual([f for f in differences(diff) if f != '__pycache__'], [])


if __name__ == '__main__':
    unittest.main()
