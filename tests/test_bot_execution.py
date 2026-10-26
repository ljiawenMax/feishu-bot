"""覆盖飞书 worker 的会话编排，外部发送与模型执行均隔离。"""
import threading
import unittest
from unittest.mock import Mock, patch

from bot import Bot
from input_conversion import InputConverter


class StopWorker(BaseException):
    pass


class BotExecutionTests(unittest.TestCase):
    def make_bot(self):
        bot = Bot.__new__(Bot)
        bot.name = 'test'
        bot.input_converter = InputConverter(enabled=False)
        bot.chat_id = 'test-chat'
        bot.work_dir = '/tmp'
        bot.sessions = {'list': [{'id': 'legacy-session', 'label': '旧会话', 'model': None}],
                        'current': 0,
                        'history': [{'role': 'user', 'content': '旧对话事实'}]}
        bot.lock = threading.RLock()
        bot.run_slot = threading.BoundedSemaphore(1)
        bot.permit_mode = False
        bot.unsafe_mode = False
        bot.auto_compact_threshold = 75
        bot.wiki_space_id = None
        bot._start_heartbeat = Mock(return_value=threading.Event())
        bot.execute_codex = Mock(return_value=('最终结果', 'codex:new-session', 'gpt-test'))
        bot.task_settings = Mock(return_value={'model': 'gpt-test', 'effort': 'high'})
        bot.audit = Mock()
        bot.db = Mock()
        bot._push_reply = Mock(return_value=True)
        return bot

    def test_existing_session_reaches_execution_and_final_reply(self):
        bot = self.make_bot()
        with patch('bot.ops_snapshot.take_quiet', return_value='snapshot'), \
             patch('bot.ops_snapshot.promote_quiet'), patch('bot.usage.report_inline', return_value=None), \
             patch('builtins.print'):
            bot._execute_job({'kind': 'task', 'text': '当前项目进度', 'msg_id': 'message'})
        bot.execute_codex.assert_called_once()
        self.assertEqual(bot.execute_codex.call_args.args[:2], ('当前项目进度', 'legacy-session'))
        self.assertEqual(bot.execute_codex.call_args.kwargs['history'],
                         [{'role': 'user', 'content': '旧对话事实'}])
        self.assertIn('最终结果', bot._push_reply.call_args.args[1])
        bot.audit.assert_called_once_with('当前项目进度', '最终结果', 'codex:new-session', 'gpt-test',
                                          entry=bot.sessions['list'][0], effort=None)
        self.assertTrue(bot._start_heartbeat.return_value.is_set())

    def test_context_read_failure_is_logged_and_does_not_block_task(self):
        bot = self.make_bot()
        with patch('core.context.codex_runner.session_context', side_effect=OSError('file missing')), \
             patch('builtins.print') as log:
            self.assertIsNone(bot._context_pct('codex:session'))
        self.assertIn('[test][context-pct-error]', log.call_args.args[0])

    def test_worker_failure_reports_and_continues_to_next_job(self):
        bot = self.make_bot()
        jobs = [{'kind': 'task', 'text': 'first', 'msg_id': 'first-id'},
                {'kind': 'task', 'text': 'second', 'msg_id': 'second-id'}]
        bot.task_queue = Mock()
        bot.task_queue.get.side_effect = [*jobs, StopWorker()]
        bot._execute_job = Mock(side_effect=[RuntimeError('unexpected failure'), None])
        with patch('builtins.print'), self.assertRaises(StopWorker):
            bot._worker()
        self.assertEqual(bot._execute_job.call_count, 2)
        self.assertEqual(bot.task_queue.task_done.call_count, 2)
        self.assertEqual(bot._push_reply.call_args.args[0], 'first-id')
        self.assertIn('unexpected failure', bot._push_reply.call_args.args[1])
        self.assertEqual(bot._push_reply.call_args.kwargs['tag'], 'worker-error')
        self.assertFalse(bot.db.call_args.kwargs['ok'])

    def test_audit_failure_does_not_prevent_error_reply(self):
        bot = self.make_bot()
        bot.task_queue = Mock()
        bot.task_queue.get.side_effect = [{'msg_id': 'message'}, StopWorker()]
        bot._execute_job = Mock(side_effect=RuntimeError('failure'))
        bot.db.side_effect = OSError('database unavailable')
        with patch('builtins.print'), self.assertRaises(StopWorker):
            bot._worker()
        bot._push_reply.assert_called_once()
        self.assertEqual(bot._push_reply.call_args.args[0], 'message')

    def test_reporting_failure_does_not_stop_worker(self):
        bot = self.make_bot()
        bot.task_queue = Mock()
        bot.task_queue.get.side_effect = [{'msg_id': 'first-id'}, {'msg_id': 'second-id'}, StopWorker()]
        bot._execute_job = Mock(side_effect=[RuntimeError('failure'), None])
        bot._push_reply.side_effect = OSError('network unavailable')
        with patch('builtins.print'), self.assertRaises(StopWorker):
            bot._worker()
        self.assertEqual(bot._execute_job.call_count, 2)
        self.assertEqual(bot.task_queue.task_done.call_count, 2)


if __name__ == '__main__':
    unittest.main()
