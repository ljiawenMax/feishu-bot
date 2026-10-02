import json
import os
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import codex_runner as runner
import usage
from core import context


class RunnerTests(unittest.TestCase):
    def execute(self, events, *, returncode=0, final='最终答复', **kwargs):
        proc = Mock(returncode=returncode)
        self.proc = proc

        def popen(cmd, **options):
            self.cmd, self.options = cmd, options
            if final is not None:
                Path(cmd[cmd.index('--output-last-message') + 1]).write_text(final)
            return proc

        proc.communicate.return_value = ('\n'.join(json.dumps(e) for e in events), 'stderr')
        with patch.object(runner, 'require_chatgpt_login'), \
             patch.object(runner.subprocess, 'Popen', side_effect=popen), \
             patch.object(runner, 'session_context', return_value={'model': 'gpt-test'}):
            return runner.run_codex('task', '/tmp', 30, **kwargs)

    def test_uses_final_answer_and_reports_actions(self):
        result, sid, model = self.execute([
            {'type': 'thread.started', 'thread_id': '123'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '过程消息'}},
            {'type': 'item.completed', 'item': {'type': 'file_change', 'status': 'completed',
                                               'changes': [{'path': '/tmp/a.py'}]}},
            {'type': 'turn.completed'},
        ])
        self.assertTrue(result.startswith('最终答复'))
        self.assertNotIn('过程消息', result)
        self.assertIn('a.py', result)
        self.assertEqual((sid, model), ('codex:123', 'gpt-test'))

    def test_failure_does_not_become_success_or_retry(self):
        result, _, _ = self.execute([{'type': 'turn.failed', 'error': {'message': 'usage limit'}}],
                                    returncode=1, final=None)
        self.assertTrue(result.startswith('[error]'))
        self.assertIn('额度已用尽', result)

    def test_legacy_session_restores_history_and_discards_claude_model(self):
        result, _, _ = self.execute([{'type': 'turn.completed'}], session_id='legacy-uuid',
                                    history=[{'role': 'user', 'content': '之前的事实'}], model='haiku')
        self.assertNotIn('resume', self.cmd)
        self.assertNotIn('--model', self.cmd)
        self.assertIn('之前的事实', self.proc.communicate.call_args.args[0])
        self.assertIn('model_provider="openai"', self.cmd)
        self.assertIn('sandbox_mode="read-only"', self.cmd)
        self.assertFalse(result.startswith('[error]'))

    def test_resume_reapplies_permissions_and_keeps_reference_dirs_readonly(self):
        sid = 'codex:01a0fd3f-6512-7e11-a602-ee301c76e661'
        with patch.object(runner, '_session_path', return_value='/existing'):
            self.execute([{'type': 'turn.completed'}], session_id=sid, permit=True,
                         extra_dirs=['/outside/reference'])
        self.assertIn('resume', self.cmd)
        self.assertIn(sid[6:], self.cmd)
        self.assertIn('sandbox_mode="workspace-write"', self.cmd)
        self.assertIn('approval_policy="never"', self.cmd)
        self.assertNotIn('--add-dir', self.cmd)
        self.assertEqual(self.options['cwd'], '/tmp')

    def test_timeout_kills_process_group_and_is_error(self):
        proc = Mock(pid=123, returncode=-9)
        proc.communicate.side_effect = [subprocess.TimeoutExpired('codex', 1), ('', '')]
        with patch.object(runner, 'require_chatgpt_login'), \
             patch.object(runner.subprocess, 'Popen', return_value=proc), \
             patch.object(runner.os, 'killpg') as kill, \
             patch.object(runner, 'session_context', return_value=None):
            result, _, _ = runner.run_codex('task', '/tmp', 1)
        kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertTrue(result.startswith('[error]'))
        self.assertIn('超时', result)

    def test_api_keys_removed_from_child_environment(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret',
                                     'OPENAI_BASE_URL': 'other', 'CODEX_HOME': '/auth-location'}):
            env = runner.cli_env()
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertNotIn('OPENAI_BASE_URL', env)
        self.assertEqual(env['CODEX_HOME'], '/auth-location')

    def test_token_count_uses_reported_window_and_counts_cache_once(self):
        events = [
            {'type': 'turn_context', 'payload': {'model': 'gpt-test'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'model_context_window': 100000,
                'last_token_usage': {'input_tokens': 60000, 'cached_input_tokens': 50000}}}},
        ]
        with patch.object(runner, '_events', return_value=iter(events)), \
             patch.object(runner, '_session_path', return_value='/file'):
            info = runner.session_context('codex:01a0fd3f-6512-7e11-a602-ee301c76e661', '/tmp')
        self.assertEqual(info['total_input'], 60000)
        with patch.object(runner, 'session_context', return_value=info):
            self.assertEqual(context.pct('sid', '/tmp'), 60)
        with patch.object(runner, 'session_context', return_value={'model': 'gpt-test'}):
            self.assertIsNone(context.pct('sid', '/tmp'))

    def test_delete_never_touches_legacy_files(self):
        with patch.object(runner.os, 'remove') as remove:
            runner.delete_codex_session('legacy-uuid', '/tmp')
        remove.assert_not_called()

    def test_invalid_session_id_cannot_traverse_files(self):
        self.assertIsNone(runner._session_path('codex:../../secret'))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.data = {'rateLimitsByLimitId': {'codex': {
            'primary': {'usedPercent': 42, 'windowDurationMins': 300, 'resetsAt': 1790971200},
            'secondary': {'usedPercent': 12, 'windowDurationMins': 10080, 'resetsAt': 1791558000}}}}

    def test_formats_server_windows_and_beijing_timezone(self):
        text = usage.format_usage(self.data, 'plus')
        self.assertIn('5 小时', text)
        self.assertIn('7 天', text)
        self.assertIn('42%', text)
        self.assertIn('10-03 04:00', text)
        self.assertIn('plus', text)

    def test_inline_never_queries_and_cache_is_shared(self):
        with patch.dict(usage._cache, {'data': None, 'ts': 0, 'sub': None}), \
             patch.object(usage, 'fetch_usage', return_value=(self.data, 'plus')) as fetch:
            self.assertIsNone(usage.report_inline())
            self.assertIn('42%', usage.report())
            self.assertIn('42%', usage.report_inline())
            self.assertIn('缓存', usage.report())
            self.assertEqual(fetch.call_count, 1)

    def test_query_failure_preserves_cache_with_label(self):
        with patch.dict(usage._cache, {'data': self.data, 'ts': 0, 'sub': 'plus'}), \
             patch.object(usage, '_blocked_until', 0), \
             patch.object(usage, 'fetch_usage', side_effect=TimeoutError('timeout')):
            self.assertIn('查询失败', usage.report())


class GuardTests(unittest.TestCase):
    def test_subscription_probe_targets_chatgpt(self):
        from ops import guard
        with patch.object(guard.subprocess, 'run', return_value=Mock(stdout='401')) as run:
            code, healthy = guard.probe_openai()
        self.assertTrue(healthy)
        self.assertEqual(code, '401')
        self.assertIn('https://chatgpt.com/backend-api/codex/models', run.call_args.args[0])

    def test_quota_failure_never_triggers_rollback(self):
        from ops import guard
        with patch.object(guard, 'probe_openai') as probe:
            rollback, _ = guard.triage({'chat': {'last_fail': '额度已用尽'}})
        self.assertFalse(rollback)
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
