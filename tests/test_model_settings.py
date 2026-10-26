"""模型选择、单次覆盖、队列快照和数据库恢复的行为验证。"""
import contextlib
import io
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bot import Bot
from core import model_settings
import codex_runner
import db
from models import Base, Message, AuditLog
import test_bot_execution
import test_codex_migration
import test_review_regressions  # 注册测试用 SQLite 类型映射

REAL_CATALOG = model_settings.catalog


def catalog():
    return {'model': 'gpt-6.1-sol', 'effort': 'high', 'models': [
        {'model': 'gpt-6.1-sol', 'name': 'Sol', 'efforts': ['low', 'medium', 'high', 'ultra'],
         'default_effort': 'low', 'is_default': True},
        {'model': 'gpt-6-luna', 'name': 'Luna', 'efforts': ['low', 'medium', 'high'],
         'default_effort': 'medium', 'is_default': False},
    ]}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(model_settings, '_CACHE_DIR', Path(self.temp.name)))
        self.stack.enter_context(patch.dict(model_settings._cache, {}, clear=True))

    def test_override_precedence_is_independent_for_each_field(self):
        entry = {'model': 'gpt-6-luna', 'effort': 'medium'}
        selected = model_settings.resolve(entry, catalog(), {'effort': 'low'})
        self.assertEqual((selected['model'], selected['effort']), ('gpt-6-luna', 'low'))
        self.assertEqual((selected['model_source'], selected['effort_source']), ('当前对话', '本次指定'))
        default = model_settings.resolve(entry, catalog(), {'model': None, 'effort': None})
        self.assertEqual((default['model'], default['effort']), ('gpt-6.1-sol', 'high'))
        self.assertEqual(entry, {'model': 'gpt-6-luna', 'effort': 'medium'})

    def test_effort_comes_from_selected_model_when_config_unset(self):
        data = catalog()
        data['effort'] = None
        self.assertEqual(model_settings.resolve({'model': 'gpt-6-luna'}, data)['effort'], 'medium')

    def test_reject_unknown_model_and_incompatible_effort(self):
        for entry in ({'model': 'gpt-unknown'}, {'model': 'gpt-6-luna', 'effort': 'ultra'}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                model_settings.resolve(entry, catalog())

    def test_parse_explicit_once_prefixes(self):
        examples = [
            ('这次用 gpt-6.1-sol，effort high，帮我审查方案',
             '帮我审查方案', {'model': 'gpt-6.1-sol', 'effort': 'high'}),
            ('本次使用 GPT-6-LUNA: 整理文字', '整理文字', {'model': 'gpt-6-luna'}),
            ('这次 effort medium，继续', '继续', {'effort': 'medium'}),
            ('这次用 effort high，继续', '继续', {'effort': 'high'}),
            ('本次推理强度设为 low，继续', '继续', {'effort': 'low'}),
            ('这次用 default，effort default，继续', '继续', {'model': None, 'effort': None}),
        ]
        for text, body, choice in examples:
            with self.subTest(text=text):
                self.assertEqual(model_settings.parse_once(text), (body, choice))
        with self.assertRaises(ValueError):
            model_settings.parse_once('这次用 gpt-6.1-sol，effort high')

    def test_task_body_and_quoted_examples_do_not_change_model(self):
        for text in ('请解释“这次用 gpt-6-luna，effort low”的含义',
                     '“这次用 gpt-6-luna，effort low，任务”',
                     '请将这次用 gpt-6-luna 写进文档', '这次用 Python 写一个脚本',
                     'effort high 是什么意思？'):
            self.assertEqual(model_settings.parse_once(text), (text, {}))

    def test_catalog_reads_effective_config_and_pages_visible_models(self):
        rpc = Mock(side_effect=[{'config': {'model': 'gpt-6-luna', 'model_reasoning_effort': 'low'}},
                               {'data': [self.rpc_model('gpt-6.1-sol')], 'nextCursor': 'next'},
                               {'data': [self.rpc_model('gpt-6-luna'),
                                         self.rpc_model('hidden', hidden=True)]}])
        def request(fn, **kwargs):
            self.assertEqual(kwargs['work_dir'], '/tmp')
            return fn(rpc), 'plus'
        with patch('codex_runner.read_account_data', side_effect=request):
            data = model_settings.fetch('/tmp')
        self.assertEqual((data['model'], data['effort']), ('gpt-6-luna', 'low'))
        self.assertEqual([m['model'] for m in data['models']], ['gpt-6.1-sol', 'gpt-6-luna'])
        self.assertEqual(rpc.call_args_list[-1].args[2]['cursor'], 'next')
        self.assertEqual(rpc.call_args_list[0].args, ('config/read', 3, {'includeLayers': False, 'cwd': '/tmp'}))

    def rpc_model(self, name, hidden=False):
        return {'model': name, 'hidden': hidden, 'defaultReasoningEffort': 'medium',
                'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}

    def test_cache_separates_workspaces_and_does_not_cache_failures(self):
        with patch.dict(model_settings._cache, {}, clear=True), \
             patch.object(model_settings, 'fetch', side_effect=[catalog(), catalog(), RuntimeError('failed'), catalog()]) as fetch:
            model_settings.catalog('/tmp/a')
            model_settings.catalog('/tmp/a')
            model_settings.catalog('/tmp/b')
            with self.assertRaises(RuntimeError):
                model_settings.catalog('/tmp/c')
            model_settings.catalog('/tmp/c')
            self.assertEqual(fetch.call_count, 4)

    def test_tasks_keep_confirmed_settings_after_ttl_without_querying_upstream(self):
        with patch.object(model_settings, 'fetch', side_effect=[catalog(), TimeoutError('upstream down')]) as fetch, \
             patch('core.model_settings.time.monotonic', return_value=100) as clock:
            first = model_settings.catalog('/tmp/a')
            clock.return_value = 10000
            again = model_settings.catalog('/tmp/a', for_task=True)
        self.assertEqual(first, again)
        self.assertEqual(fetch.call_count, 1)

    def test_restart_recovers_confirmed_settings_without_any_network(self):
        with patch.object(model_settings, 'fetch', return_value=catalog()):
            first = model_settings.catalog('/tmp/a')
        model_settings._cache.clear()
        with patch.object(model_settings, 'fetch', side_effect=TimeoutError('upstream down')) as fetch:
            self.assertEqual(model_settings.catalog('/tmp/a', for_task=True), first)
        fetch.assert_not_called()
        snapshot = next(Path(self.temp.name).glob('*.json'))
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        saved = json.loads(snapshot.read_text())
        self.assertEqual(set(saved['data']), {'model', 'effort', 'models'})

    def test_explicit_refresh_failure_keeps_cache_and_returns_warning(self):
        for error in (TimeoutError('timeout'), RuntimeError('workspace routing discovery failed')):
            with self.subTest(error=error), patch.object(model_settings, 'fetch', return_value=catalog()):
                model_settings.catalog('/tmp/a', refresh=True)
            with patch.object(model_settings, 'fetch', side_effect=error):
                old = model_settings.catalog('/tmp/a', refresh=True)
                self.assertIn('_warning', old)
                self.assertEqual(old['model'], 'gpt-6.1-sol')
                # 普通任务继续使用原始快照，不受刷新错误影响。
                task = model_settings.catalog('/tmp/a', for_task=True)
                self.assertNotIn('_warning', task)
                self.assertEqual(task['effort'], 'high')

    def test_authentication_errors_are_not_masked_by_cached_catalog(self):
        with patch.object(model_settings, 'fetch', return_value=catalog()):
            model_settings.catalog('/tmp/a')
        with patch.object(model_settings, 'fetch', side_effect=codex_runner.TokenExpired('login expired')), \
             self.assertRaises(codex_runner.TokenExpired):
            model_settings.catalog('/tmp/a', refresh=True)

    def test_config_change_invalidates_memory_and_disk_defaults(self):
        work_dir = Path(self.temp.name) / 'workspace'
        project_config = work_dir / '.codex' / 'config.toml'
        project_config.parent.mkdir(parents=True)
        with patch.object(model_settings, 'fetch', return_value=catalog()):
            model_settings.catalog(str(work_dir))
        project_config.write_text('model_reasoning_effort = "medium"\n')
        with patch.object(model_settings, 'fetch', side_effect=TimeoutError('upstream down')), \
             self.assertRaises(TimeoutError):
            model_settings.catalog(str(work_dir), for_task=True)
        model_settings._cache.clear()
        updated = catalog()
        updated['effort'] = 'medium'
        with patch.object(model_settings, 'fetch', return_value=updated):
            self.assertEqual(model_settings.catalog(str(work_dir), for_task=True)['effort'], 'medium')

    def test_config_changed_during_query_does_not_save_wrong_defaults(self):
        with patch.object(model_settings, '_config_signature', side_effect=[['before'], ['after']]), \
             patch.object(model_settings, 'fetch', return_value=catalog()), self.assertRaises(RuntimeError):
            model_settings.catalog('/tmp/a')
        self.assertFalse(list(Path(self.temp.name).glob('*.json')))

    def test_disk_write_failure_keeps_valid_memory_cache(self):
        with patch.object(model_settings, '_CACHE_DIR', Path(self.temp.name) / 'file'):
            model_settings._CACHE_DIR.write_text('not a directory')
            with patch.object(model_settings, 'fetch', return_value=catalog()) as fetch:
                self.assertEqual(model_settings.catalog('/tmp/a')['effort'], 'high')
                self.assertEqual(model_settings.catalog('/tmp/a', for_task=True)['effort'], 'high')
                self.assertEqual(fetch.call_count, 1)

    def test_corrupt_snapshot_is_ignored_and_does_not_choose_a_different_model(self):
        with patch.object(model_settings, 'fetch', return_value=catalog()):
            model_settings.catalog('/tmp/a')
        next(Path(self.temp.name).glob('*.json')).write_text('{broken')
        model_settings._cache.clear()
        with patch.object(model_settings, 'fetch', side_effect=TimeoutError('upstream down')), \
             self.assertRaises(TimeoutError):
            model_settings.catalog('/tmp/a', for_task=True)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine)
        self.bot = test_bot_execution.BotExecutionTests().make_bot()
        self.bot.client = None
        self.bot.pending = None
        self.bot.task_queue = queue.Queue()
        self.bot.extra_dirs = []
        self.bot.task_timeout = 30
        self.bot.upload_lock = threading.RLock()
        self.bot.pending_uploads = []
        self.bot.Session = self.factory
        self.bot.db = Bot.db.__get__(self.bot)
        self.bot.task_settings = Bot.task_settings.__get__(self.bot)
        with self.factory.begin() as s:
            row = db.insert_session(s, self.bot.chat_id, '对话一', 0, claude_sid='legacy-session')
            db.set_current(s, self.bot.chat_id, row)
        self.bot.current_entry()['_row_id'] = row
        self.stack = contextlib.ExitStack()
        self.catalog_mock = self.stack.enter_context(patch('core.model_settings.catalog', return_value=catalog()))
        self.reply = self.stack.enter_context(patch('bot.feishu_api.reply_message'))
        self.stack.enter_context(patch('builtins.print'))

    def tearDown(self):
        self.stack.close()
        self.engine.dispose()

    def test_commands_persist_restart_restore_and_defaults(self):
        self.bot.cmd_model({'id': 'm'}, 'gpt-6-luna')
        self.bot.cmd_effort({'id': 'm'}, 'medium')
        with self.factory() as s:
            restored = db.load_state(s, self.bot.chat_id)['sessions']
        self.bot.sessions = restored
        self.assertEqual((self.bot.current_entry()['model'], self.bot.current_entry()['effort']),
                         ('gpt-6-luna', 'medium'))
        self.bot.cmd_model({'id': 'm'}, '')
        self.assertIn('当前对话', self.reply.call_args.args[2])
        self.bot.cmd_effort({'id': 'm'}, '')
        self.assertIn('low / medium / high', self.reply.call_args.args[2])
        self.bot.cmd_effort({'id': 'm'}, 'default')
        self.bot.cmd_model({'id': 'm'}, 'default')
        with self.factory() as s:
            restored = db.load_state(s, self.bot.chat_id)['sessions']['list'][0]
        self.assertIsNone(restored['model'])
        self.assertIsNone(restored['effort'])
        self.assertIn('Codex 默认配置', self.reply.call_args.args[2])

    def test_invalid_and_db_failed_updates_preserve_settings(self):
        entry = self.bot.current_entry()
        self.bot.cmd_model({'id': 'm'}, 'gpt-unknown')
        self.assertTrue(self.reply.call_args.args[2].startswith('[error]'))
        self.assertIsNone(entry['model'])
        self.bot.cmd_model({'id': 'm'}, 'gpt-6-luna')
        self.bot.cmd_effort({'id': 'm'}, 'ultra')
        self.assertTrue(self.reply.call_args.args[2].startswith('[error]'))
        self.assertIsNone(entry.get('effort'))
        with patch.object(self.bot, 'db', side_effect=RuntimeError('commit failed')):
            self.bot.cmd_model({'id': 'm'}, 'gpt-6.1-sol')
        self.assertEqual(entry['model'], 'gpt-6-luna')
        self.assertIn('commit failed', self.reply.call_args.args[2])

    def test_removed_model_still_allows_directory_view_and_recovery(self):
        self.bot.current_entry()['model'] = 'gpt-removed'
        self.bot.cmd_model({'id': 'm'}, '')
        self.assertIn('gpt-6-luna', self.reply.call_args.args[2])
        self.bot.cmd_model({'id': 'm'}, 'default')
        self.assertIsNone(self.bot.current_entry()['model'])

    def test_queued_settings_and_once_restore_are_independent_of_later_commands(self):
        self.bot.run_task({'id': 'm'}, '这次用 gpt-6-luna，effort low，整理文字')
        first = self.bot.task_queue.get_nowait()
        self.bot.run_task({'id': 'm2'}, '继续整理')
        second = self.bot.task_queue.get_nowait()
        self.bot.cmd_model({'id': 'change'}, 'gpt-6-luna')
        self.bot.cmd_effort({'id': 'change'}, 'medium')
        self.assertEqual((first['execution_settings']['model'], first['execution_settings']['effort']),
                         ('gpt-6-luna', 'low'))
        self.assertEqual(first['task_text'], '整理文字')
        self.assertTrue(first['text'].startswith('这次用'))
        self.assertEqual((second['execution_settings']['model'], second['execution_settings']['effort']),
                         ('gpt-6.1-sol', 'high'))
        self.assertEqual(self.bot.current_entry()['effort'], 'medium')

    def test_switching_and_new_sessions_keep_separate_choices(self):
        first = self.bot.current_entry()
        self.bot.cmd_effort({'id': 'm'}, 'low')
        self.bot.cmd_new({'id': 'new'}, '')
        second = self.bot.current_entry()
        self.assertIsNone(second['model'])
        self.assertIsNone(second['effort'])
        self.bot.cmd_model({'id': 'm'}, 'gpt-6-luna')
        self.bot.sessions['current'] = 0
        self.assertEqual(self.bot.task_settings(first)['model'], 'gpt-6.1-sol')
        self.assertEqual(self.bot.task_settings(first)['effort'], 'low')
        self.assertEqual(second['model'], 'gpt-6-luna')

    def test_compaction_and_merge_keep_effort(self):
        self.bot.cmd_effort({'id': 'm'}, 'medium')
        row = self.bot.current_entry()['_row_id']
        with self.factory.begin() as s:
            db.compact_session(s, row, '摘要')
            child = db.insert_session(s, self.bot.chat_id, '子对话', 1)
            db.set_session_settings(s, child, 'gpt-6-luna', 'low')
            db.set_current(s, self.bot.chat_id, child)
        with self.factory() as s:
            self.assertEqual(db.load_state(s, self.bot.chat_id)['sessions']['list'][0]['effort'], 'medium')
        with self.factory.begin() as s:
            db.merge_sessions(s, self.bot.chat_id, row, [child])
        with self.factory() as s:
            self.assertEqual(db.load_state(s, self.bot.chat_id)['sessions']['list'][0]['effort'], 'low')

    def test_invalid_once_does_not_queue_or_consume_attachments(self):
        self.bot._drain_uploads = Mock()
        self.bot.run_task({'id': 'm'}, '这次用 gpt-6-luna，effort ultra，整理附件')
        self.assertTrue(self.bot.task_queue.empty())
        self.bot._drain_uploads.assert_not_called()

    def test_preflight_query_failure_is_audited_and_attachments_are_kept(self):
        self.catalog_mock.side_effect = TimeoutError('Codex 查询超时（initialize）')
        self.bot._drain_uploads = Mock()
        self.bot.run_task({'id': 'failed-message'}, '整理附件')
        self.assertTrue(self.bot.task_queue.empty())
        self.bot._drain_uploads.assert_not_called()
        self.assertTrue(self.reply.call_args.args[2].startswith('[error]'))
        with self.factory() as s:
            record = s.query(AuditLog).filter_by(kind='preflight').one()
            self.assertFalse(record.ok)
            self.assertEqual(record.message_id, 'failed-message')
            self.assertEqual(record.tag, 'model-settings')
            self.assertIn('initialize', record.detail)

    def test_parse_and_command_failures_are_audited_too(self):
        self.bot.run_task({'id': 'parse'}, '这次用 gpt-6-luna，effort low')
        self.bot.cmd_model({'id': 'command'}, 'gpt-unknown')
        with self.factory() as s:
            rows = s.query(AuditLog).filter_by(kind='preflight').all()
            self.assertEqual({r.tag for r in rows}, {'settings-parse', 'model-command'})

    def test_audit_database_failure_still_delivers_preflight_error(self):
        self.catalog_mock.side_effect = TimeoutError('upstream down')
        with patch.object(self.bot, 'db', side_effect=RuntimeError('database down')):
            self.bot.run_task({'id': 'm'}, '继续任务')
        self.assertIn('upstream down', self.reply.call_args.args[2])
        self.assertTrue(self.bot.task_queue.empty())

    def test_command_stale_catalog_warning_is_visible(self):
        data = catalog()
        data.update(_warning='目录暂时无法刷新，使用上次确认的列表', _cached_at=100)
        self.catalog_mock.return_value = data
        self.bot.cmd_model({'id': 'm'}, '')
        self.assertIn(data['_warning'], self.reply.call_args.args[2])

    def test_missing_explicit_model_refreshes_directory_but_never_substitutes(self):
        updated = catalog()
        updated['models'].append({'model': 'gpt-new', 'name': 'New', 'efforts': ['high'],
                                  'default_effort': 'high', 'is_default': False})
        self.catalog_mock.side_effect = [catalog(), updated]
        self.bot.run_task({'id': 'm'}, '这次用 gpt-new，继续任务')
        job = self.bot.task_queue.get_nowait()
        self.assertEqual(job['execution_settings']['model'], 'gpt-new')
        self.assertTrue(self.catalog_mock.call_args.kwargs['refresh'])

    def test_real_cached_task_can_queue_and_execute_during_metadata_outage_after_restart(self):
        self.catalog_mock.side_effect = REAL_CATALOG
        self.bot.execute_codex = Bot.execute_codex.__get__(self.bot)
        self.bot.audit = Bot.audit.__get__(self.bot)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(model_settings, '_CACHE_DIR', Path(directory)), \
             patch.dict(model_settings._cache, {}, clear=True):
            with patch.object(model_settings, 'fetch', return_value=catalog()):
                REAL_CATALOG(self.bot.work_dir)
            path = next(Path(directory).glob('*.json'))
            saved = json.loads(path.read_text())
            saved['saved_at'] = time.time() - 3600
            path.write_text(json.dumps(saved))
            model_settings._cache.clear()  # 模拟重启，内存和 60 秒 TTL 都已失效。

            def run(*args, **kwargs):
                self.assertEqual((kwargs['model'], kwargs['effort']), ('gpt-6.1-sol', 'high'))
                kwargs['execution_info'].update(model='gpt-6.1-sol', effort='high')
                return '完成', 'codex:done', 'gpt-6.1-sol'

            with patch.object(model_settings, 'fetch', side_effect=RuntimeError('workspace routing discovery failed')) as fetch, \
                 patch('bot.codex_runner.run_codex', side_effect=run) as execute, \
                 patch('bot.ops_snapshot.take_quiet'), patch('bot.ops_snapshot.promote_quiet'), \
                 patch('bot.usage.report_inline', return_value=None):
                self.bot.run_task({'id': 'm'}, '继续任务')
                job = self.bot.task_queue.get_nowait()
                self.bot._execute_job(job)
                fetch.assert_not_called()
                execute.assert_called_once()
                self.assertIn('完成', self.bot._push_reply.call_args.args[1])
                self.bot.cmd_model({'id': 'view'}, '')
                fetch.assert_called_once()
                self.assertIn('上次确认', self.reply.call_args.args[2])

    def test_execution_uses_frozen_pair_and_records_actual_effort(self):
        self.bot.execute_codex = Bot.execute_codex.__get__(self.bot)
        self.bot.audit = Bot.audit.__get__(self.bot)
        self.bot.run_task({'id': 'm'}, '这次用 gpt-6-luna，effort low，整理文字')
        job = self.bot.task_queue.get_nowait()
        self.bot.cmd_model({'id': 'change'}, 'gpt-6.1-sol')
        def run(*args, **kwargs):
            self.assertEqual((kwargs['model'], kwargs['effort']), ('gpt-6-luna', 'low'))
            self.assertEqual(args[0], '整理文字')
            kwargs['execution_info'].update(model='gpt-6-luna', effort='low')
            return '完成', 'codex:done', 'gpt-6-luna'
        with patch('bot.codex_runner.run_codex', side_effect=run), \
             patch('bot.ops_snapshot.take_quiet'), patch('bot.ops_snapshot.promote_quiet'), \
             patch('bot.usage.report_inline', return_value=None):
            self.bot._execute_job(job)
        self.assertIn('effort：low', self.bot._push_reply.call_args.args[1])
        with self.factory() as s:
            self.assertEqual(s.query(Message).filter_by(role='assistant').one().effort, 'low')
            self.assertEqual(s.query(AuditLog).filter_by(kind='llm').one().effort, 'low')


class RunnerEffortTests(unittest.TestCase):
    def test_account_query_does_not_force_token_refresh(self):
        messages = [
            {'id': 1, 'result': {}},
            {'id': 2, 'result': {'account': {'type': 'chatgpt', 'planType': 'plus'}}},
            {'id': 3, 'result': {'test': True}},
        ]
        proc = Mock()
        proc.stdout = io.StringIO('\n'.join(json.dumps(m) for m in messages))
        proc.stdin = Mock()
        with patch('codex_runner.subprocess.Popen', return_value=proc):
            result, sub = codex_runner.read_account_data(lambda rpc: rpc('config/read', 3))
        sent = [json.loads(call.args[0]) for call in proc.stdin.write.call_args_list]
        self.assertEqual(next(m for m in sent if m['method'] == 'account/read')['params'],
                         {'refreshToken': False})
        self.assertEqual((result, sub), ({'test': True}, 'plus'))

    def test_resume_passes_explicit_effort_and_reports_actual_metadata(self):
        runner = test_codex_migration.RunnerTests()
        info = {}
        sid = 'codex:01a0fd3f-6512-7e11-a602-ee301c76e661'
        with patch.object(codex_runner, '_session_path', return_value='/file'):
            runner.execute([{'type': 'turn.completed'}], session_id=sid,
                           model='gpt-6-luna', effort='low', execution_info=info)
        self.assertIn('resume', runner.cmd)
        self.assertIn('model_reasoning_effort="low"', runner.cmd)
        self.assertEqual(runner.cmd[runner.cmd.index('--model') + 1], 'gpt-6-luna')
        self.assertIsNone(info['effort'])  # 元数据缺失时不能把请求值冒充实际值

    def test_context_reads_latest_effort_even_without_token_count(self):
        events = [{'type': 'turn_context', 'payload': {'model': 'gpt-6-luna', 'effort': 'low'}},
                  {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'effort': 'high'}}]
        with patch.object(codex_runner, '_events', return_value=iter(events)), \
             patch.object(codex_runner, '_session_path', return_value='/file'):
            info = codex_runner.session_context('codex:01a0fd3f-6512-7e11-a602-ee301c76e661', '/tmp')
        self.assertEqual(info, {'model': 'gpt-6.1-sol', 'effort': 'high'})

    def test_failed_turn_does_not_report_previous_effort(self):
        runner = test_codex_migration.RunnerTests()
        info = {'effort': 'stale'}
        runner.execute([{'type': 'turn.failed', 'error': {'message': 'quota'}}],
                       returncode=1, effort='low', execution_info=info)
        self.assertEqual(info, {})


if __name__ == '__main__':
    unittest.main()
