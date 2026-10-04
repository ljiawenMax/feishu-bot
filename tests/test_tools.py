"""验证真实 CLI/socket 路由、失效令牌、重启时序及语音错误。"""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

from bot import Bot
from core import sessions, uploads
import codex_runner
import speaker
import tool_bridge

ROOT = Path(__file__).resolve().parents[1]


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = tool_bridge.start(str(Path(self.tmp.name) / 'tools.sock'))
        self.bot = Mock(chat_id='chat-a', unsafe_mode=False)
        self.bot.current_cwd.return_value = self.tmp.name
        self.bot._push_reply.return_value = True
        self.bot.db.return_value = []
        self.bot.pending_uploads_snapshot.return_value = []
        self.binding = tool_bridge.bind(self.bot, 'msg-a')
        self.ctx = self.binding.__enter__()

    def tearDown(self):
        self.binding.__exit__(None, None, None)
        self.server.shutdown()
        self.server.server_close()
        tool_bridge._SERVER = None
        self.tmp.cleanup()

    def cli(self, *args, env=None):
        proc = subprocess.run([str(ROOT/'fbtool'), *args], env={**os.environ, **(env or self.ctx.env)},
                              capture_output=True, text=True, timeout=5)
        return proc.returncode, json.loads(proc.stdout)

    def test_socket_mode_and_cli_dispatch(self):
        self.assertEqual(os.stat(self.server.server_address).st_mode & 0o777, 0o600)
        rc, result = self.cli('send_text', '正在验证')
        self.assertEqual(rc, 0)
        self.assertTrue(result['ok'])
        self.bot._push_reply.assert_called_once_with('msg-a', '正在验证', tag='tool-progress')

    def test_mismatched_chat_message_and_expired_token_cannot_send(self):
        for field in ('FEISHU_CHAT_ID', 'FEISHU_MSG_ID', 'FEISHU_TOOL_TOKEN'):
            env = {**self.ctx.env, field: 'wrong'}
            rc, result = self.cli('send_text', '不能发送', env=env)
            self.assertEqual(rc, 1)
            self.assertFalse(result['ok'])
        self.binding.__exit__(None, None, None)
        rc, result = self.cli('send_text', '已结束')
        self.assertEqual(rc, 1)
        self.bot._push_reply.assert_not_called()
        # tearDown 可再次 exit：contextmanager 不重复进入。
        self.binding = contextlib.nullcontext()

    def test_second_server_cannot_unlink_live_socket(self):
        with self.assertRaises(BlockingIOError):
            tool_bridge.ToolServer(self.server.server_address)
        self.assertEqual(self.cli('list_uploads')[0], 0)

    def test_restart_requires_user_unsafe_and_only_records_intent(self):
        with patch('tool_bridge.schedule_restart') as run:
            self.assertEqual(self.cli('restart_service')[0], 1)
            self.ctx.unsafe = True
            self.bot.unsafe_mode = True
            self.assertEqual(self.cli('restart_service')[0], 0)
            self.assertTrue(self.ctx.restart)
            run.assert_not_called()

    def test_file_validation_count_and_no_markers_in_progress(self):
        path = Path(self.tmp.name)/'a.txt'
        path.write_text('附件')
        success = Mock(sends=[], **{'__bool__': Mock(return_value=True)})
        with patch('tool_bridge.feishu_api.upload_file', return_value='file'), \
             patch('tool_bridge.feishu_api.reply_file', return_value=success):
            self.assertEqual(self.cli('send_file', 'relative')[0], 1)
            for _ in range(3):
                self.assertEqual(self.cli('send_file', str(path))[0], 0)
            self.assertEqual(self.cli('send_file', str(path))[0], 1)
        self.assertEqual(self.cli('send_text', f'[[SEND_FILE:{path}]]')[0], 1)

    def test_unknown_tool_and_bounded_requests(self):
        self.assertRaises(ValueError, self.ctx.dispatch, 'unsafe', {})
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(self.server.server_address)
            conn.sendall(b'x' * (tool_bridge.MAX_REQUEST + 1))
            response = json.loads(conn.makefile('rb').readline())
        self.assertFalse(response['ok'])

    def test_git_status_is_read_only_and_for_correct_cwd(self):
        with patch('core.git.subprocess.run', return_value=Mock(returncode=0, stdout=' M a.py\n')) as run:
            result = self.ctx.dispatch('git_local_changes', {})
        self.assertEqual(result['changes'], ' M a.py\n')
        self.assertEqual(run.call_args.kwargs['cwd'], self.tmp.name)
        self.assertIn('--no-optional-locks', run.call_args.args[0])


class RestartOrderingTests(unittest.TestCase):
    def test_only_successful_final_delivery_schedules_restart(self):
        from test_bot_execution import BotExecutionTests
        for failed, delivered in ((False, True), (False, False), (True, True)):
            with self.subTest(failed=failed, delivered=delivered):
                bot = BotExecutionTests().make_bot()
                bot.unsafe_mode = True
                ctx = Mock(env={}, restart=True, compact=False, files=0)
                order = []
                bot.execute_codex.return_value = ('[error] 失败' if failed else '完成', None, None)
                bot._push_reply.side_effect = lambda *a, **kw: order.append('reply') or delivered
                with patch('bot.tool_bridge.bind', return_value=contextlib.nullcontext(ctx)), \
                     patch('bot.tool_bridge.schedule_restart', side_effect=lambda: order.append('restart')), \
                     patch('bot.ops_snapshot.take_quiet'), patch('bot.ops_snapshot.promote_quiet'), \
                     patch('bot.usage.report_inline', return_value=None), patch('builtins.print'):
                    bot._execute_job({'kind': 'task', 'text': '重启', 'msg_id': 'msg'})
                self.assertEqual(order, ['reply', 'restart'] if not failed and delivered else ['reply'])


class CoreTests(unittest.TestCase):
    def test_pending_snapshot_expires_without_consuming_or_sharing_mutable_entries(self):
        bot = Bot.__new__(Bot)
        bot.upload_lock = threading.RLock()
        bot.pending_uploads = [{'path': '/recent', 'ts': 10000}, {'path': '/expired', 'ts': 0}]
        with patch('bot.time.time', return_value=10001):
            snapshot = bot.pending_uploads_snapshot()
            self.assertEqual([u['path'] for u in snapshot], ['/recent'])
            snapshot[0]['path'] = '/changed'
            self.assertEqual(bot.pending_uploads[0]['path'], '/recent')
            drained = bot._drain_uploads()
        self.assertEqual([u['path'] for u in drained], ['/recent'])
        self.assertEqual(bot.pending_uploads, [])

    def test_remove_keeps_current_entry_and_resets_empty_pointer(self):
        data = {'list': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}], 'current': 2}
        sessions.remove_at(data, 0)
        self.assertEqual(sessions.current_sid(data), 'c')
        sessions.remove_at(data, 1)
        self.assertEqual(sessions.current_sid(data), 'b')
        sessions.remove_at(data, 0)
        self.assertEqual(data['current'], 0)
        self.assertIsNone(sessions.current(data))

    def test_file_markers_deduplicate_and_reject_nonfiles(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'a'; p.write_text('a')
            text, paths, rejected = uploads.extract_files(f'结果\n[[SEND_FILE:{p}]]\n[[SEND_FILE:{p}]]\n[[SEND_FILE:{d}]]')
        self.assertEqual(paths, [str(p)])
        self.assertEqual(text, '结果')
        self.assertEqual(len(rejected), 1)

    def test_internal_child_does_not_inherit_task_capability(self):
        with patch.dict(os.environ, {'FEISHU_TOOL_TOKEN': 'parent-token', 'FEISHU_MSG_ID': 'parent'}):
            env = codex_runner.cli_env()
        self.assertNotIn('FEISHU_TOOL_TOKEN', env)
        self.assertNotIn('FEISHU_MSG_ID', env)

    def test_bot_instantiation_and_restored_commands(self):
        with patch('bot.db.load_state', return_value={}), patch.object(Bot, 'db', return_value={}):
            bot = Bot({'name':'test','app_id':'app','app_secret':'secret','chat_id':'chat',
                       'work_dir':'/tmp'}, Mock(), 10, client=Mock())
        for cmd in ('/allsessions','/name','/permit'):
            self.assertIn(cmd, bot.commands)
        self.assertNotIn('/debate', bot.commands)


class SpeakerTests(unittest.TestCase):
    def test_429_backoff_and_failure_are_explicit(self):
        error = urllib.error.HTTPError('https://tts', 429, 'limit', {}, None)
        with patch('speaker.urllib.request.urlopen', side_effect=error) as request, \
             patch('speaker.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '429'):
                speaker.synthesize('测试', {'key':'test'})
        self.assertEqual(request.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])

    def test_missing_sink_and_ssh_failure_are_not_success(self):
        with patch('speaker.subprocess.run', return_value=Mock(returncode=99)):
            with self.assertRaisesRegex(RuntimeError, 'rc=99'):
                speaker.play(b'RIFF', {'remote':'user@host', 'sink':'missing'})

    def test_long_text_is_truncated_and_synchronous(self):
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json'; config.write_text('{"key":"test"}')
            with patch('speaker.synthesize', return_value=b'RIFF') as synth, \
                 patch('speaker.play') as play, patch.dict(os.environ, {'XDG_RUNTIME_DIR':d}):
                result=speaker.speak('字'*250, str(config))
            self.assertTrue(result['played'])
            self.assertTrue(result['truncated'])
            self.assertEqual(len(synth.call_args.args[0]), 200)
            play.assert_called_once()




if __name__ == '__main__':
    unittest.main()
