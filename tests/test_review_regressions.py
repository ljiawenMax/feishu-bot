"""真实状态转换/数据库恢复/发送失败/知识库分页的回归场景，外部接口隔离。"""
import contextlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from bot import Bot
from core.locking import index_lock
import db
import feishu_api
import feishu_claude
from models import Base, Conversation, Message
import tool_bridge
import uploads
import wiki_index
import wiki_sync
import test_bot_execution


@compiles(LONGTEXT, 'sqlite')
def sqlite_longtext(type_, compiler, **kw):
    return 'TEXT'


@compiles(BigInteger, 'sqlite')
def sqlite_bigint(type_, compiler, **kw):
    return 'INTEGER'


class SessionRaceTests(unittest.TestCase):
    def make_bot(self):
        bot = test_bot_execution.BotExecutionTests().make_bot()
        bot.task_queue = queue.Queue()
        bot.pending_uploads = []
        bot.upload_lock = threading.RLock()
        bot.client = None
        bot.extra_dirs = []
        bot.task_timeout = 30
        bot.current_entry()['cwd'] = '/original/project'
        bot.current_entry()['_row_id'] = 7
        return bot

    def execution(self):
        stack = contextlib.ExitStack()
        for target in ('bot.ops_snapshot.take_quiet', 'bot.ops_snapshot.promote_quiet',
                       'bot.usage.report_inline', 'builtins.print'):
            stack.enter_context(patch(target, return_value=None))
        return stack

    def test_switch_during_run_keeps_session_id_history_cwd_and_audit_on_original(self):
        bot = self.make_bot()
        original = bot.current_entry()
        switched = {'id': None, 'label': '新对话', 'cwd': '/other/project', '_row_id': 8}
        started, finish = threading.Event(), threading.Event()
        def runner(*a, **kw):
            started.set()
            if not finish.wait(3):
                raise RuntimeError('test synchronization timed out')
            return '结果', 'codex:original-new-id', 'gpt-test'
        bot.execute_codex = Bot.execute_codex.__get__(bot)
        bot.audit = Bot.audit.__get__(bot)
        with self.execution(), patch('bot.codex_runner.run_codex', side_effect=runner) as run:
            bot._enqueue({'text': '任务', 'msg_id': 'm'}, 'm', '执行')
            job = bot.task_queue.get_nowait()
            worker = threading.Thread(target=bot._execute_job, args=(job,))
            worker.start()
            self.assertTrue(started.wait(3))
            with bot.lock:
                bot.sessions['list'].append(switched)
                bot.sessions['current'] = 1
                bot.sessions['history'] = []
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(run.call_args.args[1], '/original/project')
        self.assertEqual(original['id'], 'codex:original-new-id')
        self.assertIsNone(switched['id'])
        self.assertEqual(bot.sessions['history'], [])
        self.assertEqual(original['_history'][-1]['content'], '结果')
        records = [c for c in bot.db.call_args_list if c.args[0] is db.append_message]
        self.assertEqual([c.args[1] for c in records], [7, 7])

    def test_queued_task_uses_session_selected_at_enqueue(self):
        bot = self.make_bot()
        original = bot.current_entry()
        with self.execution(), patch('bot.feishu_api.reply_message'):
            bot._enqueue({'text': '排队任务', 'msg_id': 'm'}, 'm', '执行')
            other = {'id': 'other', 'label': 'other'}
            bot.sessions['list'].append(other)
            bot.sessions['current'] = 1
            bot.sessions['history'] = []
            bot._execute_job(bot.task_queue.get_nowait())
        self.assertEqual(bot.execute_codex.call_args.args[1], 'legacy-session')
        self.assertIs(bot.execute_codex.call_args.kwargs['entry'], original)
        self.assertEqual(bot.sessions['history'], [])

    def test_queued_or_running_session_cannot_be_deleted(self):
        bot = self.make_bot()
        with patch('bot.feishu_api.reply_message') as reply:
            bot._enqueue({'text': 'task', 'msg_id': 'm'}, 'm', '执行')
            bot.cmd_del({'id': 'm'}, '1')
        self.assertEqual(len(bot.sessions['list']), 1)
        self.assertIn('排队', reply.call_args.args[2])
        bot.db.assert_not_called()

    def test_failed_recovery_does_not_replace_existing_id(self):
        bot = self.make_bot()
        with patch('bot.codex_runner.run_codex', return_value=('[error] quota', 'codex:empty', None)):
            Bot.execute_codex(bot, 'task', 'legacy-session', entry=bot.current_entry())
        self.assertEqual(bot.current_entry()['id'], 'legacy-session')
        bot.db.assert_not_called()

    def test_permission_snapshot_is_taken_after_semaphore_wait(self):
        bot = self.make_bot()
        bot.unsafe_mode = True
        bot.run_slot.acquire()
        waiting = threading.Event()
        bot._start_heartbeat.side_effect = lambda *a: waiting.set() or threading.Event()
        with self.execution():
            worker = threading.Thread(target=bot._execute_job,
                                      args=({'text': 'task', 'msg_id': 'm'},))
            worker.start()
            self.assertTrue(waiting.wait(3))
            with bot.lock:
                bot.unsafe_mode = False
            bot.run_slot.release()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(bot.execute_codex.call_args.kwargs['permissions'], (False, False))

    def test_direct_compact_is_available_at_hard_limit(self):
        bot = self.make_bot()
        bot._context_guard = Mock(side_effect=AssertionError('must bypass hard guard'))
        bot._do_compact = Mock(return_value={'seed': '摘要'})
        bot._execute_job({'text': '压缩一下上下文', 'msg_id': 'm'})
        bot._do_compact.assert_called_once()
        bot.execute_codex.assert_not_called()

    def test_compaction_keeps_original_entry_cwd_selection_and_queued_followups(self):
        bot = self.make_bot()
        source = bot.current_entry()
        with patch('bot.feishu_api.reply_message'):
            bot._enqueue({'text': 'followup', 'msg_id': 'm'}, 'm', '执行')
        other = {'id': 'other', 'label': 'other', 'cwd': '/other/project'}
        def summarize(*a, **kw):
            with bot.lock:
                bot.sessions['list'].append(other)
                bot.sessions['current'] = 1
                bot.sessions['history'] = [{'role': 'user', 'content': '其他会话'}]
            return '压缩摘要'
        with patch('bot.core_compact.summarize', side_effect=summarize) as run:
            compacted = bot._do_compact('m', entry=source)
        self.assertIs(compacted, source)
        self.assertEqual(len(bot.sessions['list']), 2)
        self.assertIs(bot.current_entry(), other)
        self.assertEqual(bot.sessions['history'][0]['content'], '其他会话')
        self.assertEqual(compacted['cwd'], '/original/project')
        self.assertEqual(run.call_args.args[1], '/original/project')
        self.assertEqual(compacted['seed'], '压缩摘要')
        self.assertIs(bot.task_queue.get_nowait()['entry'], compacted)
        self.assertEqual(compacted['_jobs'], 1)
        self.assertEqual(compacted['_row_id'], 7)
        self.assertEqual(compacted['label'], '旧会话')
        self.assertIsNone(compacted['id'])
        self.assertIs(bot.db.call_args.args[0], db.compact_session)
        self.assertTrue(bot.db.call_args.kwargs['strict'])
        self.assertEqual(bot.db.call_args.args[1:], (7, '压缩摘要'))

    def test_compacted_session_cannot_be_deleted_while_announcing_it(self):
        bot = self.make_bot()
        source = bot.current_entry()
        source['_jobs'] = 1
        job = {'entry': source, 'text': '任务', 'msg_id': 'm'}
        def reply(msg_id, text, **kw):
            if text.startswith('✅ 已压缩'):
                selected = bot.current_entry()
                self.assertEqual(selected['_jobs'], 1)
                bot.cmd_del({'id': 'm'}, str(bot.sessions['current'] + 1))
                self.assertIs(bot.current_entry(), selected)
            return True
        bot._push_reply.side_effect = reply
        with patch('bot.core_compact.summarize', return_value='摘要'), patch('bot.feishu_api.reply_message'):
            compacted = bot._do_compact('m', entry=source, job=job)
        self.assertIs(job['entry'], compacted)
        self.assertIs(compacted, source)
        self.assertEqual(source['_jobs'], 1)

    def test_automatic_compaction_reuses_list_entry_and_sends_summary_to_execution(self):
        bot = self.make_bot()
        source = bot.current_entry()
        source['model'] = 'gpt-test'
        source['external'] = True
        with self.execution(), patch.object(bot, '_context_pct', return_value=80), \
             patch('bot.core_compact.summarize', return_value='关键事实'):
            bot._execute_job({'text': '继续任务', 'msg_id': 'm', 'entry': source})
        self.assertEqual(len(bot.sessions['list']), 1)
        self.assertIs(bot.current_entry(), source)
        self.assertEqual(source['_row_id'], 7)
        self.assertEqual(source['model'], 'gpt-test')
        self.assertFalse(source['external'])
        self.assertEqual(bot.execute_codex.call_args.args[:2], ('继续任务', None))
        self.assertIn('关键事实', bot.execute_codex.call_args.kwargs['run_text'])
        self.assertEqual(bot.execute_codex.call_args.kwargs['history'], [])

    def test_repeated_compaction_preserves_previous_summary_without_adding_entries(self):
        bot = self.make_bot()
        source = bot.current_entry()
        with patch('bot.core_compact.summarize', side_effect=['第一份摘要', '第二份摘要']) as run:
            bot._do_compact('m', entry=source)
            bot._do_compact('m', entry=source)
        self.assertEqual(run.call_count, 2)
        self.assertIn('第一份摘要', run.call_args.args[0][0]['content'])
        self.assertEqual(len(bot.sessions['list']), 1)
        self.assertEqual(source['seed'], '第二份摘要')

    def test_compaction_after_successful_followup_keeps_older_summary(self):
        bot = self.make_bot()
        source = bot.current_entry()
        source['_summary'] = '早餐牛奶固定200ml'
        with patch('bot.core_compact.summarize', return_value='新摘要') as run:
            bot._do_compact('m', entry=source)
        self.assertIn('早餐牛奶固定200ml', run.call_args.args[0][0]['content'])
        self.assertEqual(run.call_args.args[0][1]['content'], '旧对话事实')

    def test_failed_compaction_does_not_change_original_session(self):
        for failure in ('summary', 'database'):
            with self.subTest(failure=failure):
                bot = self.make_bot()
                source = bot.current_entry()
                bot._entry_history(source)
                before = dict(source)
                if failure == 'database':
                    bot.db.side_effect = OSError('database down')
                with patch('bot.core_compact.summarize', return_value=None if failure == 'summary' else '摘要'):
                    if failure == 'database':
                        with self.assertRaises(OSError):
                            bot._do_compact('m', entry=source)
                    else:
                        self.assertFalse(bot._do_compact('m', entry=source))
                self.assertEqual(source, before)
                self.assertEqual(len(bot.sessions['list']), 1)

    def test_db_failure_rolls_back_permission_toggle(self):
        bot = self.make_bot()
        bot.unsafe_mode = True
        bot.db.side_effect = OSError('database down')
        with self.assertRaises(OSError):
            bot.cmd_permit({'id': 'm'}, '')
        self.assertEqual((bot.permit_mode, bot.unsafe_mode), (False, True))

    def test_name_with_newline_is_parsed_and_long_name_is_rejected(self):
        bot = self.make_bot()
        bot.pending = None
        bot.commands = {'/name': Mock()}
        bot.handle_message({'id': 'm', 'text': '/name\n新的名字'})
        bot.commands['/name'].assert_called_once_with({'id': 'm', 'text': '/name\n新的名字'}, '新的名字')
        with patch('bot.feishu_api.reply_message') as reply:
            bot.cmd_name({'id': 'm'}, '名' * 256)
        self.assertIn('255', reply.call_args.args[2])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_seed_survives_restart_until_successful_first_turn(self):
        with self.sessions.begin() as s:
            row = db.insert_session(s, 'chat', '压缩对话', 0, cwd='/project', seed='关键事实')
            db.set_current(s, 'chat', row)
        with self.sessions() as s:
            state = db.load_state(s, 'chat')
        self.assertEqual(state['sessions']['list'][0]['seed'], '关键事实')
        self.assertEqual(state['sessions']['history'], [])
        with self.sessions.begin() as s:
            db.update_session(s, row, claude_sid='codex:real')
            db.append_message(s, row, 'chat', 'codex:real', 'user', '下一条任务')
            db.append_message(s, row, 'chat', 'codex:real', 'assistant', '结果')
        with self.sessions() as s:
            state = db.load_state(s, 'chat')
        self.assertNotIn('seed', state['sessions']['list'][0])
        self.assertEqual([m['role'] for m in state['sessions']['history']], ['user', 'assistant'])

    def test_failed_seed_transaction_leaves_no_orphan_session(self):
        with self.assertRaises(RuntimeError), self.sessions.begin() as s:
            db.insert_session(s, 'chat', '压缩', 0, seed='摘要')
            raise RuntimeError('simulate commit failure')
        with self.sessions() as s:
            self.assertEqual(db.load_state(s, 'chat')['sessions']['list'], [])

    def test_compaction_and_restart_keep_row_metadata_summary_and_old_messages(self):
        with self.sessions.begin() as s:
            row = db.insert_session(s, 'chat', '饮食方案', 3, claude_sid='codex:old',
                                    cwd='/project', external=True)
            db.set_session_model(s, row, 'gpt-test')
            db.set_current(s, 'chat', row)
            db.append_message(s, row, 'chat', 'codex:old', 'user', '早餐牛奶200ml')
            other = db.insert_session(s, 'chat', '其他项目', 4, claude_sid='codex:other')
        with self.sessions.begin() as s:
            db.compact_session(s, row, '关键事实')
        with self.sessions() as s:
            state = db.load_state(s, 'chat')['sessions']
            original = state['list'][0]
            self.assertEqual([e['_row_id'] for e in state['list']], [row, other])
            self.assertEqual(state['current'], 0)
            self.assertEqual(state['history'], [])
            self.assertEqual((original['label'], original['cwd'], original['model']),
                             ('饮食方案', '/project', 'gpt-test'))
            self.assertFalse(original['external'])
            self.assertIsNone(original['id'])
            self.assertEqual(original['seed'], '关键事实')
            self.assertEqual(s.get(Conversation, row).position, 3)
            self.assertEqual(db.load_history(s, 'chat', 'codex:old')[0]['content'], '早餐牛奶200ml')
        # 续聊后恢复最新摘要，第二次压缩仍更新同一行。
        with self.sessions.begin() as s:
            db.update_session(s, row, claude_sid='codex:new')
            db.append_message(s, row, 'chat', 'codex:new', 'user', '继续')
            db.append_message(s, row, 'chat', 'codex:new', 'assistant', '结果')
        with self.sessions() as s:
            state = db.load_state(s, 'chat')['sessions']
            self.assertNotIn('seed', state['list'][0])
            self.assertEqual(state['list'][0]['_summary'], '关键事实')
            self.assertEqual([m['content'] for m in state['history']], ['继续', '结果'])
        with self.sessions.begin() as s:
            db.compact_session(s, row, '第二份摘要')
        with self.sessions() as s:
            state = db.load_state(s, 'chat')['sessions']
            self.assertEqual(len(state['list']), 2)
            self.assertEqual(state['list'][0]['seed'], '第二份摘要')
            self.assertEqual(state['list'][0]['_row_id'], row)

    def test_failed_compaction_transaction_keeps_old_sid_and_no_summary(self):
        with self.sessions.begin() as s:
            row = db.insert_session(s, 'chat', '原对话', 0, claude_sid='codex:old', external=True)
        with self.assertRaises(RuntimeError), self.sessions.begin() as s:
            db.compact_session(s, row, '摘要')
            s.flush()
            raise RuntimeError('simulate commit failure')
        with self.sessions() as s:
            original = db.load_state(s, 'chat')['sessions']['list'][0]
            self.assertEqual(original['id'], 'codex:old')
            self.assertTrue(original['external'])
            self.assertNotIn('seed', original)
            self.assertEqual(s.query(Message).count(), 0)

    def test_automatic_compaction_persists_new_execution_sid_on_original_row(self):
        with self.sessions.begin() as s:
            row = db.insert_session(s, 'chat', '原对话', 0, claude_sid='codex:old')
            db.set_current(s, 'chat', row)
            db.append_message(s, row, 'chat', 'codex:old', 'user', '原事实')
        bot = SessionRaceTests().make_bot()
        bot.chat_id = 'chat'
        bot.Session = self.sessions
        bot.db = Bot.db.__get__(bot)
        bot.execute_codex = Bot.execute_codex.__get__(bot)
        bot.audit = Bot.audit.__get__(bot)
        with self.sessions() as s:
            bot.sessions = db.load_state(s, 'chat')['sessions']
        with SessionRaceTests().execution(), patch.object(bot, '_context_pct', return_value=80), \
             patch('bot.core_compact.summarize', return_value='原事实摘要'), \
             patch('bot.codex_runner.run_codex', return_value=('结果', 'codex:new', 'gpt-test')) as run:
            bot._execute_job({'text': '继续', 'msg_id': 'm', 'entry': bot.current_entry()})
        self.assertIn('原事实摘要', run.call_args.args[0])
        self.assertIsNone(run.call_args.args[3])
        with self.sessions() as s:
            state = db.load_state(s, 'chat')['sessions']
            self.assertEqual(len(state['list']), 1)
            self.assertEqual(state['list'][0]['_row_id'], row)
            self.assertEqual(state['list'][0]['id'], 'codex:new')
            self.assertEqual(state['list'][0]['_summary'], '原事实摘要')
            self.assertNotIn('seed', state['list'][0])
            self.assertEqual([m['content'] for m in state['history']], ['继续', '结果'])


class DeliveryTests(unittest.TestCase):
    def make_bot(self):
        bot = test_bot_execution.BotExecutionTests().make_bot()
        bot._push_reply = Bot._push_reply.__get__(bot)
        bot.client = None
        return bot

    def result(self, ok):
        return feishu_api.SendResult([{'ok': ok, 'code': 0 if ok else 500, 'msg': 'test'}])

    def test_only_failed_segment_is_retried(self):
        bot = self.make_bot()
        text = 'a' * 3900 + 'b' * 25
        with patch('bot.feishu_api.reply_message', side_effect=[self.result(True), self.result(False), self.result(True)]) as send, \
             patch('bot.time.sleep'):
            self.assertTrue(bot._push_reply('m', text))
        self.assertEqual([call.args[2] for call in send.call_args_list], ['a' * 3900, 'b' * 25, 'b' * 25])

    def test_failed_segment_stops_later_segments_and_attachments(self):
        bot = self.make_bot()
        bot._send_files = Mock()
        with tempfile.NamedTemporaryFile() as f, \
             patch('bot.feishu_api.reply_message', return_value=self.result(False)) as send:
            self.assertFalse(bot._push_reply('m', 'a' * 8000 + f'[[SEND_FILE:{f.name}]]', attempts=1))
        self.assertEqual(send.call_count, 1)
        bot._send_files.assert_not_called()

    def test_attachment_api_failure_is_reported_and_delivery_is_false(self):
        bot = self.make_bot()
        with patch('bot.feishu_api.upload_file', return_value='key'), \
             patch('bot.feishu_api.reply_file', return_value=self.result(False)), \
             patch('bot.feishu_api.reply_message', return_value=self.result(True)) as report:
            self.assertFalse(bot._send_files('m', ['/tmp/file']))
        self.assertIn('附件发送失败', report.call_args.args[2])


class WikiTests(unittest.TestCase):
    def test_fulltext_still_works_when_embedding_fails(self):
        idx = wiki_index.Index('space', [{'text': '解决方案', 'chunk_id': 'id'}], np.zeros((1, 2)))
        with patch('wiki_index._vector_hits', side_effect=OSError('ollama down')), \
             patch('wiki_index._fulltext_hits', return_value=[(0, 1.0)]):
            hits = wiki_index.search(idx, '问题', {}, session_factory=Mock())
        self.assertEqual(hits[0]['text'], '解决方案')
        self.assertIsNone(hits[0]['vec_score'])

    def test_failed_vector_without_fulltext_is_not_silent_no_matches(self):
        idx = wiki_index.Index('space', [{'text': 'x'}], np.zeros((1, 2)))
        with patch('wiki_index._vector_hits', side_effect=OSError('down')), self.assertRaises(RuntimeError):
            wiki_index.search(idx, '问题', {})

    def test_query_has_short_timeout_and_single_attempt(self):
        idx = wiki_index.Index('space', [{'text': 'x'}], np.ones((1, 2)))
        with patch('wiki_index.requests.post', side_effect=OSError('down')) as request, self.assertRaises(RuntimeError):
            wiki_index._vector_hits(idx, '问题', {'embed_url': 'http://local', 'embed_model': 'test'})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs['timeout'], 10)

    def test_empty_index_clears_fulltext_copy(self):
        with tempfile.TemporaryDirectory() as d, patch('wiki_index._sync_db') as sync:
            wiki_index.build(d, {'embed_model': 'test'}, session_factory=Mock(), log=lambda s: None)
        self.assertEqual(sync.call_args.args[2], [])

    def test_corrupted_numpy_file_is_recoverable(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, wiki_index.CHUNKS_FILE).write_text(json.dumps({'text': 'x'}) + '\n')
            Path(d, wiki_index.VECTORS_FILE).write_bytes(b'corrupt')
            self.assertIsNone(wiki_index.load(d))

    def test_incomplete_tree_does_not_delete_preexisting_docs(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'docs').mkdir()
            doc = Path(d, 'docs', 'existing.md')
            doc.write_text('keep me')
            manifest = {'existing': {'file': doc.name}}
            wiki_sync._write_manifest(d, manifest)
            client = Mock()
            data = NS(items=[], has_more=True, page_token='next')
            with patch('wiki_sync._api', return_value=data), patch('wiki_sync.MAX_PAGES', 1), self.assertRaises(RuntimeError):
                wiki_sync.sync_space(client, 'space', d)
            self.assertEqual(doc.read_text(), 'keep me')
            self.assertEqual(wiki_sync.read_manifest(d), manifest)

    def test_repeated_page_token_is_detected(self):
        with patch('wiki_sync._api', return_value=NS(items=[], has_more=True, page_token='same')), self.assertRaises(RuntimeError):
            wiki_sync._list_children(Mock(), 'space')

    def test_same_title_and_token_prefix_does_not_collide(self):
        self.assertNotEqual(wiki_sync.safe_filename('同名', 'prefix00-first'),
                            wiki_sync.safe_filename('同名', 'prefix00-second'))

    def test_index_lock_is_reentrant_and_blocks_other_processes(self):
        with tempfile.TemporaryDirectory() as d:
            with index_lock(d), index_lock(d):
                code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                result = subprocess.run(['python3', '-c', code, d + '.lock'], capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'BlockingIOError', result.stderr)
            Path(d + '.lock').unlink()


class UploadAndSocketTests(unittest.TestCase):
    def test_two_resources_in_same_message_have_distinct_paths(self):
        def download(client, message_id, key, resource_type, f, maximum):
            f.write(key.encode())
            return len(key), 'image/png'
        with tempfile.TemporaryDirectory() as d, patch('uploads.UPLOAD_ROOT', d), \
             patch('uploads.feishu_api.download_to_file', side_effect=download):
            a = uploads.save_upload(None, {'id': 'm', 'file_key': 'a', 'resource_type': 'image'}, 'chat')
            b = uploads.save_upload(None, {'id': 'm', 'file_key': 'b', 'resource_type': 'image'}, 'chat')
            self.assertNotEqual(a['path'], b['path'])
            self.assertEqual(Path(a['path']).read_text(), 'a')
            self.assertEqual(os.stat(a['path']).st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(d, 'chat').glob('*.part')), [])

    def test_long_chinese_filename_fits_filesystem_byte_limit(self):
        def download(client, message_id, key, resource_type, f, maximum):
            f.write(b'file')
            return 4, 'application/pdf'
        with tempfile.TemporaryDirectory() as d, patch('uploads.UPLOAD_ROOT', d), \
             patch('uploads.feishu_api.download_to_file', side_effect=download):
            info = uploads.save_upload(None, {'id': 'm', 'file_key': 'k', 'file_name': '中文' * 100}, 'chat')
            self.assertLessEqual(len(os.fsencode(info['file_name'])), 255)
            self.assertEqual(Path(info['path']).read_bytes(), b'file')

    def test_socket_does_not_remove_regular_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'socket')
            path.write_text('user data')
            with self.assertRaises(ValueError):
                tool_bridge.ToolServer(path)
            self.assertEqual(path.read_text(), 'user data')

    def test_restart_revocation_is_checked_in_active_context(self):
        bot = Mock(chat_id='c', unsafe_mode=True)
        ctx = tool_bridge.CallContext(bot, 'm', '')
        bot.unsafe_mode = False
        with self.assertRaises(ValueError):
            ctx.dispatch('restart_service', {})
        self.assertFalse(ctx.restart)


class ConfigTests(unittest.TestCase):
    def env(self):
        return {'DB_HOST': 'localhost', 'DB_NAME': 'db', 'DB_USER': 'u', 'DB_PASSWORD': 'fake',
                'TASK_TIMEOUT': '30', 'BOTS': json.dumps([{'name': 'bot', 'chat_id': 'chat',
                                                         'app_id': 'app', 'app_secret': 'fake'}])}

    def test_mysql_default_port(self):
        with patch('feishu_claude.load_env_file', return_value=self.env()):
            self.assertEqual(feishu_claude.load_config('test')['db']['port'], 3306)

    def test_zero_concurrency_and_timeout_fail_early(self):
        for field in ('MAX_CONCURRENT', 'TASK_TIMEOUT'):
            with self.subTest(field=field), patch('feishu_claude.load_env_file', return_value={**self.env(), field: '0'}), self.assertRaises(ValueError):
                feishu_claude.load_config('test')

    def test_duplicate_chat_is_rejected_before_starting_workers(self):
        env = self.env()
        bots = json.loads(env['BOTS'])
        bots.append({**bots[0], 'name': 'other'})
        env['BOTS'] = json.dumps(bots)
        with patch('feishu_claude.load_env_file', return_value=env), self.assertRaises(ValueError):
            feishu_claude.load_config('test')


class ProcessAndSnapshotTests(unittest.TestCase):
    def test_parent_guard_kills_detached_child_after_parent_exit(self):
        import sys
        import time
        guard = Path(__file__).resolve().parents[1] / 'core' / 'parent_guard.py'
        with tempfile.TemporaryDirectory() as d:
            ready = str(Path(d, 'ready'))
            child = "import os,time;from pathlib import Path;Path(%r).write_text(str(os.getpid()));time.sleep(30)" % ready
            parent = ("import os,subprocess,sys,time;from pathlib import Path;"
                      "p=subprocess.Popen([sys.executable,sys.argv[1],str(os.getpid()),sys.executable,'-c',sys.argv[3]],"
                      "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                      "deadline=time.monotonic()+3;"
                      "\nwhile not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.01)"
                      "\nprint(Path(sys.argv[2]).read_text(),flush=True);sys.stdin.read(1)")
            process = subprocess.Popen([sys.executable, '-c', parent, str(guard), ready, child],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = process.communicate('\n', timeout=5)
            self.assertEqual(process.returncode, 0, err)
            pid = int(out.strip())
            deadline = time.monotonic() + 3
            try:
                while time.monotonic() < deadline:
                    stat_path = Path(f'/proc/{pid}/stat')
                    if not stat_path.exists() or stat_path.read_text().split(') ', 1)[1].startswith('Z'):
                        break
                    time.sleep(.01)
                else:
                    self.fail('child survived parent exit')
            finally:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass

    def test_broken_stdin_does_not_mask_failure_or_skip_process_cleanup(self):
        import usage
        proc = Mock()
        import io
        proc.stdout = io.StringIO('')
        proc.stdin.close.side_effect = BrokenPipeError('already exited')
        proc.wait.side_effect = [subprocess.TimeoutExpired('app-server', 1), 0]
        proc.pid = 123
        with patch('usage.subprocess.Popen', return_value=proc), \
             patch('usage.os.killpg') as kill, \
             self.assertRaises(RuntimeError) as error:
            usage.fetch_usage()
        self.assertNotIsInstance(error.exception, BrokenPipeError)
        kill.assert_called_once()

    def test_running_old_code_cannot_promote_new_disk_code(self):
        from ops import snapshot
        with tempfile.TemporaryDirectory() as d, patch.object(snapshot, 'ROOT', d), \
             patch.object(snapshot, '_RUNTIME_FINGERPRINT', None), patch.object(snapshot, 'take') as take:
            path = Path(d, 'bot.py')
            path.write_text('working code')
            snapshot.capture_runtime()
            path.write_text('broken new code')
            self.assertIsNone(snapshot.take_quiet())
            take.assert_not_called()

    def test_copied_snapshot_must_match_loaded_code_even_after_task_edits(self):
        from ops import snapshot
        with tempfile.TemporaryDirectory() as d, patch.object(snapshot, 'ROOT', str(Path(d, 'root'))), \
             patch.object(snapshot, 'SNAP_DIR', str(Path(d, 'snap'))), \
             patch.object(snapshot, '_RUNTIME_FINGERPRINT', None), patch.object(snapshot, 'promote', return_value=True) as promote:
            root = Path(snapshot.ROOT)
            snap = Path(snapshot.SNAP_DIR, 'id')
            root.mkdir()
            snap.mkdir(parents=True)
            Path(root, 'bot.py').write_text('working')
            Path(snap, 'bot.py').write_text('working')
            snapshot.capture_runtime()
            Path(root, 'bot.py').write_text('task edited code')
            self.assertTrue(snapshot.promote_quiet('id'))
            promote.assert_called_once_with('id')
            Path(snap, 'bot.py').write_text('unverified')
            self.assertFalse(snapshot.promote_quiet('id'))
            self.assertEqual(promote.call_count, 1)

    def test_metadata_read_failure_keeps_successful_model_answer(self):
        import codex_runner
        proc = Mock(returncode=0)
        proc.communicate.return_value = (json.dumps({'type': 'turn.completed'}), '')
        def popen(cmd, **kw):
            Path(cmd[cmd.index('--output-last-message') + 1]).write_text('任务完成')
            return proc
        with patch('codex_runner.require_chatgpt_login'), patch('codex_runner.subprocess.Popen', side_effect=popen), \
             patch('codex_runner.session_context', side_effect=OSError('metadata missing')):
            result, _, _ = codex_runner.run_codex('task', '/tmp', 5)
        self.assertEqual(result, '任务完成')

    def test_session_tail_ignores_assistant_commentary_and_invalid_events(self):
        import codex_runner
        def event(role, text, phase=None):
            return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                    'phase': phase, 'content': [{'type': 'output_text', 'text': text}]}}
        events = [event('user', '用户问题'), event('assistant', '过程更新', 'commentary'),
                  {'payload': ['invalid']}, event('assistant', '最终结果', 'final_answer')]
        with patch('codex_runner._events', return_value=iter(events)), patch('codex_runner._session_path'):
            tail = codex_runner.session_tail('codex:id', '/tmp', rounds=1)
        self.assertEqual([turn['content'] for turn in tail], ['用户问题', '最终结果'])

    def test_explicit_wiki_tool_error_is_distinct_from_no_matches(self):
        bot = test_bot_execution.BotExecutionTests().make_bot()
        bot.wiki_index = Mock()
        bot.wiki_index.__len__ = Mock(return_value=1)
        bot.wiki_cfg = {}
        bot.wiki_top_k = 5
        bot.Session = Mock()
        with patch('bot.wiki_index.search', side_effect=OSError('unavailable')), self.assertRaises(RuntimeError):
            bot._wiki_reference('query', strict=True)
