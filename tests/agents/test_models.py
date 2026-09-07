"""自动模型发现的失效路径；临时文件、假采集器和本地子进程，不调用真实模型。"""
from __future__ import annotations

import contextlib
import io
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

from my_agents2 import models as M
from my_agents2._model_registry import CollectionError, ModelRegistry, timestamp


def catalog(slug='first', efforts=None, **kwargs):
    return dict(source='command', models=[dict(slug=slug, efforts=efforts)], **kwargs)


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='model-regression-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'models.json'
        self.now = 2000000000.0
        self.sig = {'probe': 'v1'}
        self.calls = []
        self.data = catalog()
        self.collectors = {'probe': self.collect}
        self.store = ModelRegistry(self.path, self.collectors, self.signature,
                                   ttl=10, retry_interval=3, clock=lambda: self.now)

    def signature(self, name):
        return self.sig.get(name, 'v1')

    def collect(self):
        self.calls.append(True)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def integration(self):
        for patcher in (patch.object(M, 'REGISTRY_PATH', self.path),
                        patch.object(M, '_registry_instance', self.store),
                        patch.dict(os.environ, {'MY_AGENTS2_AUTO_REFRESH': '1',
                                                'MY_AGENTS2_MODELS_TTL_SECONDS': '10',
                                                'MY_AGENTS2_MODELS_RETRY_SECONDS': '3'})):
            patcher.start();self.addCleanup(patcher.stop)


class TestRefresh(Fixture):
    def test_first_use_collects_and_ttl_expiry_refreshes(self):
        self.integration()
        self.assertEqual(M.list_models('probe')[0].slug, 'first')
        self.data = catalog('second')
        self.now += 9
        self.assertEqual(M.list_models('probe')[0].slug, 'first')
        self.now += 2
        self.assertEqual(M.list_models('probe')[0].slug, 'second')
        self.assertEqual(len(self.calls), 2)

    def test_source_change_refreshes_before_ttl(self):
        self.store.ensure(['probe'])
        self.data = catalog('after-upgrade');self.sig['probe'] = 'v2'
        self.store.ensure(['probe'])
        self.assertEqual(self.store.read()['backends']['probe']['models'][0]['slug'], 'after-upgrade')
        self.assertEqual(len(self.calls), 2)

    def test_external_write_is_visible_without_restarting(self):
        self.store.ensure(['probe'])
        second = ModelRegistry(self.path, {'probe': lambda: catalog('external')}, self.signature)
        second.ensure(['probe'], force=True)
        self.assertEqual(self.store.read()['backends']['probe']['models'][0]['slug'], 'external')

    def test_caller_cannot_mutate_in_memory_registry(self):
        self.store.ensure(['probe'])
        self.store.read()['backends'].clear()
        self.assertIn('probe', self.store.read()['backends'])

    def test_failed_refresh_preserves_data_and_success_time(self):
        self.store.ensure(['probe']);before = self.store.read()
        self.now += 20;self.data = CollectionError('test failure')
        self.store.ensure(['probe'])
        after = self.store.read();entry = after['backends']['probe']
        self.assertEqual(after['updated_at'], before['updated_at'])
        self.assertEqual(entry['collected_at'], before['backends']['probe']['collected_at'])
        self.assertEqual(entry['models'], before['backends']['probe']['models'])
        self.assertEqual(entry['last_error'], 'test failure')
        self.assertNotEqual(entry['checked_at'], entry['collected_at'])
        self.assertTrue(self.store.status('probe')['stale'])

    def test_failure_cooldown_and_changed_source_bypass(self):
        self.data = CollectionError('failed')
        self.store.ensure(['probe']);self.store.ensure(['probe'])
        self.assertEqual(len(self.calls), 1)
        self.sig['probe'] = 'v2';self.store.ensure(['probe'])
        self.assertEqual(len(self.calls), 2)
        self.now += 4;self.store.ensure(['probe'])
        self.assertEqual(len(self.calls), 3)

    def test_retry_cooldown_survives_new_process_instance(self):
        self.data = CollectionError('failed');self.store.ensure(['probe'])
        other = ModelRegistry(self.path, self.collectors, self.signature, retry_interval=3, clock=lambda: self.now)
        other.ensure(['probe'])
        self.assertEqual(len(self.calls), 1)

    def test_force_refresh_bypasses_ttl_and_works_with_auto_disabled(self):
        self.integration();self.store.ensure(['probe']);self.data = catalog('forced')
        with patch.dict(os.environ, {'MY_AGENTS2_AUTO_REFRESH': '0'}):
            self.assertEqual(M.list_models('probe')[0].slug, 'first')
            M.refresh_models_registry(['probe'])
            self.assertEqual(M.list_models('probe')[0].slug, 'forced')

    def test_cache_only_does_not_run_collector(self):
        self.integration()
        self.assertEqual(M.list_models('probe', refresh=False), [])
        self.assertEqual(M.models_status('probe')['model_count'], 0)
        self.assertEqual(self.calls, [])

    def test_unknown_model_rechecks_with_short_cooldown(self):
        self.integration();M.list_models('probe');self.now += 4
        self.data = catalog('new')
        self.assertEqual(M.find_model('probe', 'new').slug, 'new')
        self.assertIsNone(M.find_model('probe', 'missing'))
        self.assertIsNone(M.find_model('probe', 'missing'))
        self.assertEqual(len(self.calls), 2)

    def test_updated_effort_applies_to_next_resolution(self):
        self.integration();self.data = catalog('first', ['low', 'high'])
        self.assertEqual(M.resolve_effort('probe', 'first', 'max')[0], 'high')
        self.data = catalog('first', ['low', 'high', 'max']);self.sig['probe'] = 'v2'
        self.assertEqual(M.resolve_effort('probe', 'first', 'max')[0], 'max')

    def test_future_effort_does_not_crash_resolution(self):
        self.integration();self.data = catalog('first', ['future'])
        self.assertEqual(M.resolve_effort('probe', 'first', 'high')[0], 'high')

    def test_old_upstream_cache_is_not_relabelled_fresh(self):
        self.data = dict(source='cache', source_updated_at=timestamp(self.now - 100), models=catalog()['models'])
        self.store.ensure(['probe'])
        status = self.store.status('probe')
        self.assertTrue(status['stale'])
        self.assertIn('CLI 上游缓存过期或时间未知', status['stale_reasons'])
        self.store.ensure(['probe'])
        self.assertEqual(len(self.calls), 1)

    def test_source_change_during_collection_does_not_commit_wrong_signature(self):
        def changing():
            self.sig['probe'] = 'v2'
            return catalog('unstable')
        self.collectors['probe'] = changing
        self.store.ensure(['probe'])
        self.assertEqual(self.store.status('probe')['model_count'], 0)
        self.assertIn('来源发生变化', self.store.status('probe')['last_error'])

    def test_empty_or_invalid_collector_output_keeps_previous(self):
        self.store.ensure(['probe'])
        for value in (None, {'models': []}, {'models': [{'slug': ''}]},
                      {'models': [{'slug': 'a', 'efforts': 'high'}]},
                      {'models': [{'slug': 'a'}, {'slug': 'a'}]}):
            with self.subTest(value=value):
                self.data = value;self.store.ensure(['probe'], force=True)
                self.assertEqual(self.store.read()['backends']['probe']['models'][0]['slug'], 'first')

    def test_corrupt_file_preserved_and_last_good_data_recovered(self):
        self.store.ensure(['probe']);self.path.write_text('{broken')
        self.assertEqual(self.store.read()['backends']['probe']['models'][0]['slug'], 'first')
        self.store.ensure(['probe'], force=True)
        backups = list(self.root.glob('models.json.corrupt-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{broken')
        self.assertFalse(self.store.status('probe')['stale'])

    def test_corrupt_file_without_memory_can_rebuild(self):
        self.path.write_text('{broken');self.store.ensure(['probe'])
        self.assertEqual(self.store.status('probe')['model_count'], 1)
        self.assertEqual(len(list(self.root.glob('models.json.corrupt-*'))), 1)

    def test_atomic_write_failure_keeps_previous_and_backs_off(self):
        self.store.ensure(['probe']);before = self.path.read_bytes();self.now += 20
        with patch('my_agents2._model_registry.os.replace', side_effect=OSError('test')):
            self.store.ensure(['probe']);self.store.ensure(['probe'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.root.glob('.models.json.*')), [])
        self.assertIn('无法更新', self.store.status('probe')['last_error'])

    def test_thread_refreshes_share_one_collection(self):
        def slow():
            time.sleep(.02)
            return self.collect()
        self.collectors['probe'] = slow
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: self.store.ensure(['probe']), range(8)))
        self.assertEqual(len(self.calls), 1)

    def test_processes_share_refresh_and_merge_different_backends(self):
        code = '''import sys,time
from pathlib import Path
from my_agents2._model_registry import ModelRegistry
path=Path(sys.argv[1]);name=sys.argv[2]
def collect():
 with (path.parent / (name+'.count')).open('a') as f:f.write('1\\n')
 time.sleep(.1)
 return {'source':'command','models':[{'slug':name,'efforts':['high']}]}
store=ModelRegistry(path,{name:collect},lambda _: 'version')
store.ensure([name])
'''
        names = ['a', 'a', 'a', 'b', 'b']
        procs = [subprocess.Popen([sys.executable, '-B', '-c', code, str(self.path), name],
                                 cwd=str(Path(__file__).resolve().parents[2]),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in names]
        for proc in procs:
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(set(self.store.read()['backends']), {'a', 'b'})
        for name in ['a', 'b']:
            self.assertEqual((self.root / (name + '.count')).read_text(), '1\n')

    def test_failed_cli_refresh_returns_nonzero_exit(self):
        from my_agents2.__main__ import main
        with patch('my_agents2.refresh_models_registry', return_value={'pi': 'kept（failed）'}), \
             patch.object(sys, 'argv', ['my_agents2', '--refresh-models', 'pi']), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 1)


class TestCollectors(Fixture):
    def test_nonzero_metadata_command_cannot_accept_partial_output(self):
        proc = subprocess.CompletedProcess(['pi'], 1, stdout='valid-looking-models', stderr='SECRET')
        with patch.object(M.subprocess, 'run', return_value=proc), self.assertRaises(CollectionError) as raised:
            M._run_metadata(['pi', '--list-models'])
        self.assertNotIn('SECRET', str(raised.exception))

    def test_metadata_timeout_is_explicit(self):
        with patch.object(M.subprocess, 'run', side_effect=subprocess.TimeoutExpired('pi', 1)), \
             self.assertRaisesRegex(CollectionError, '超时'):
            M._run_metadata(['pi', '--list-models'])

    def test_pi_thinking_is_not_mistaken_for_exact_effort_levels(self):
        text = 'provider model context max-out thinking images\na thinking-model 200k 8k yes yes\na plain-model 32k 8k no no\n'
        with patch.object(M, '_executable', return_value='pi'), patch.object(M, '_run_metadata', return_value=text):
            models = M._collect_pi()['models']
        self.assertIsNone(models[0]['efforts'])
        self.assertEqual(models[1]['efforts'], [])

    def test_effort_support_preserves_unknown(self):
        self.assertIsNone(M.ModelInfo('unknown').supports_effort())
        self.assertFalse(M.ModelInfo('none', efforts=()).supports_effort())
        self.assertTrue(M.ModelInfo('yes', efforts=('high',)).supports_effort())

    def test_agy_new_effort_variants_are_collected(self):
        text = 'example-high\tExample (High)\nexample-max\tExample (Max)\nexample-ultra\tExample (Ultra)\n'
        with patch.object(M, '_executable', return_value='agy'), patch.object(M, '_run_metadata', return_value=text):
            result = M._collect_agy()
        self.assertEqual(result['models'][0]['efforts'], ['high', 'max', 'ultra'])

    def test_claude_ultra_capability_is_not_ignored(self):
        self.assertEqual(M._claude_efforts(['effort', 'ultra_effort']), ['low', 'medium', 'high', 'ultra'])

    def test_claude_changed_binary_format_reports_failure(self):
        with patch.object(M, '_executable', return_value='/tmp/claude'), \
             patch.object(M, '_run_metadata', return_value='unrecognized format'), \
             self.assertRaisesRegex(CollectionError, '无法解析'):
            M._collect_claude()

    def test_grok_unknown_effort_is_distinct_from_unsupported(self):
        directory = self.root / '.grok';directory.mkdir()
        (directory / 'models_cache.json').write_text(json.dumps({'models': {
            'unknown': {'info': {'supports_reasoning_effort': True}},
            'none': {'info': {'supports_reasoning_effort': False}},
            'future': {'info': {'reasoning_efforts': [{'value': 'future'}]}}}}))
        with patch.object(M.Path, 'home', return_value=self.root):
            rows = M._collect_grok()['models']
        self.assertIsNone(rows[0]['efforts'])
        self.assertEqual(rows[1]['efforts'], [])
        self.assertEqual(rows[2]['efforts'], ['future'])

    def test_codex_empty_levels_are_explicitly_unsupported(self):
        directory = self.root / '.codex';directory.mkdir()
        (directory / 'models_cache.json').write_text(json.dumps({'models': [
            {'slug': 'none', 'supported_reasoning_levels': []}, {'slug': 'unknown'},
            {'slug': 'null', 'supported_reasoning_levels': None}]}))
        with patch.object(M.Path, 'home', return_value=self.root):
            rows = M._collect_codex()['models']
        self.assertEqual(rows[0]['efforts'], [])
        self.assertIsNone(rows[1]['efforts'])
        self.assertIsNone(rows[2]['efforts'])

    def test_cli_symlink_upgrade_changes_signature(self):
        first = self.root / 'cli-v1';first.write_text('one')
        second = self.root / 'cli-v2';second.write_text('two')
        link = self.root / 'cli';link.symlink_to(first)
        with patch.object(M.shutil, 'which', return_value=str(link)):
            old = M._source_signature('claude')
            link.unlink();link.symlink_to(second)
            self.assertNotEqual(old, M._source_signature('claude'))

    def test_pi_config_change_changes_signature_without_reading_secrets(self):
        folder = self.root / '.pi' / 'agent';folder.mkdir(parents=True)
        config = folder / 'models.json';config.write_text('SECRET')
        with patch.object(M.Path, 'home', return_value=self.root), patch.object(M.shutil, 'which', return_value=None):
            old = M._source_signature('pi')
            config.write_text('SECRET changed')
            new = M._source_signature('pi')
        self.assertNotEqual(old, new)
        self.assertNotIn('SECRET', new)


if __name__ == '__main__':
    unittest.main()
