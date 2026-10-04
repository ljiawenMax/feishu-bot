"""独立辩论的执行、只读 RPC、取消与失败回执；外发始终隔离。"""
import contextlib
import json
import io
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core import debate
import codex_runner
import tool_bridge

ROOT = Path(__file__).resolve().parents[1]


class DebateCoreTests(unittest.TestCase):
    def test_explicit_topic_requires_real_debate_but_inspection_does_not(self):
        for text in ('辩论话题:测试', ' 辩题：测试', '$feishu-debate 测试'):
            self.assertTrue(debate.required(text))
        for text in ('检查 feishu-debate 的错误', '辩论话题：', '怎么触发辩论模式？'):
            self.assertFalse(debate.required(text))

    def test_sides_keep_separate_history_and_exchange_only_final_statements(self):
        prompts = []
        def run(prompt, *args, **kwargs):
            prompts.append(prompt)
            self.assertTrue(kwargs['lightweight'])
            self.assertTrue(kwargs['parent_bound'])
            self.assertFalse(kwargs['report_actions'])
            self.assertNotIn('tool_env', kwargs)
            return f'发言{len(prompts)}', None, 'test-model'
        emit = Mock()
        with patch('codex_runner.run_codex', side_effect=run):
            result = debate.run('测试', '/tmp', emit, rounds=2)
        self.assertTrue(result['completed'])
        self.assertEqual(emit.call_count, 4)
        self.assertNotIn('你是反方', prompts[2])
        self.assertNotIn('你是正方', prompts[3])
        self.assertIn('发言2', prompts[2])
        self.assertIn('发言3', prompts[3])

    def test_partial_model_failure_stops_without_fabricating_or_retrying(self):
        emit = Mock()
        with patch('codex_runner.run_codex', side_effect=[('成功', None, 'm'),
                                                       ('[error] 429', None, None)]) as run:
            with self.assertRaisesRegex(RuntimeError, '反方第1轮失败.*429'):
                debate.run('测试', '/tmp', emit)
        self.assertEqual(run.call_count, 2)
        emit.assert_called_once()

    def test_push_failure_stops_next_model_call(self):
        emit = Mock(side_effect=RuntimeError('推送失败'))
        with patch('codex_runner.run_codex', return_value=('发言', None, 'm')) as run:
            with self.assertRaisesRegex(RuntimeError, '推送失败'):
                debate.run('测试', '/tmp', emit)
        run.assert_called_once()

    def test_deadline_and_cancellation_prevent_calls_and_late_pushes(self):
        with patch('codex_runner.run_codex') as run:
            with self.assertRaisesRegex(RuntimeError, '总时限'):
                debate.run('测试', '/tmp', Mock(), deadline=time.monotonic()-1)
            run.assert_not_called()
        cancelled = threading.Event()
        emit = Mock()
        def finish(*args, **kwargs):
            cancelled.set()
            return '发言', None, 'm'
        with patch('codex_runner.run_codex', side_effect=finish) as run:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                debate.run('测试', '/tmp', emit, cancelled=cancelled)
            run.assert_called_once()
        emit.assert_not_called()

    def test_child_timeout_is_limited_to_remaining_task_time(self):
        with patch('core.debate.time.monotonic', return_value=100), \
             patch('codex_runner.run_codex', return_value=('[error] 超时', None, None)) as run:
            with self.assertRaises(RuntimeError):
                debate.run('测试', '/tmp', Mock(), timeout=180, deadline=103)
        self.assertEqual(run.call_args.args[2], 3)

    def test_compatibility_script_delegates_and_reports_rpc_failure(self):
        module = runpy.run_path(str(ROOT/'.agents/skills/feishu-debate/scripts/debate.py'))
        call = Mock(return_value={'ok': True, 'result': {'completed': True}})
        with patch('runpy.run_path', return_value={'call': call}):
            self.assertTrue(module['debate']('测试')['completed'])
            call.assert_called_once_with('debate', {'topic': '测试'})
            call.return_value = {'ok': False, 'error': '独立调用失败'}
            with self.assertRaisesRegex(RuntimeError, '独立调用失败'):
                module['debate']('测试')


class DebateSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = tool_bridge.start(str(Path(self.tmp.name)/'tools.sock'))
        self.bot = Mock(chat_id='debate-chat', unsafe_mode=False, task_timeout=30)
        self.bot.current_cwd.return_value = self.tmp.name
        self.bot._push_reply.return_value = True
        self.binding = tool_bridge.bind(self.bot, 'debate-message')
        self.ctx = self.binding.__enter__()

    def tearDown(self):
        self.binding.__exit__(None, None, None)
        self.server.shutdown()
        self.server.server_close()
        tool_bridge._SERVER = None
        self.tmp.cleanup()

    def cli(self, *, sandbox=False):
        cmd = [str(ROOT/'fbtool'), 'debate', '--topic', '测试辩题']
        if sandbox:
            cmd = ['codex', 'sandbox', '-c', 'sandbox_mode="read-only"', '--', *cmd]
        proc = subprocess.run(cmd, env={**os.environ, **self.ctx.env},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def assert_complete(self, response, run):
        self.assertTrue(response['result']['completed'])
        self.assertEqual(len(response['result']['statements']), 10)
        self.assertEqual(run.call_count, 10)
        self.assertEqual(self.ctx.debate['sent'], 10)
        self.assertEqual(self.bot._push_reply.call_count, 10)
        self.assertEqual(run.call_args.args[1], self.tmp.name)
        tags = [c.kwargs['tag'] for c in self.bot._push_reply.call_args_list]
        self.assertEqual(tags, [f'debate-{side}-r{turn}' for turn in range(1,6)
                               for side in ('pro', 'con')])
        self.assertTrue(all(c.args[0] == 'debate-message' for c in self.bot._push_reply.call_args_list))

    def test_cli_broker_runs_ten_isolated_calls_and_pushes(self):
        with patch('codex_runner.run_codex', return_value=('最终发言', None, 'test-model')) as run:
            self.assert_complete(self.cli(), run)
        with self.assertRaisesRegex(ValueError, '不重复'):
            self.ctx.dispatch('debate', {'topic':'测试'})

    def mcp(self):
        requests = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call',
             'params':{'name':'debate','arguments':{'topic':'测试辩题'}}},
        ]
        proc = subprocess.run([str(ROOT/'fbtool'), '--mcp'],
                              input=''.join(json.dumps(r)+'\n' for r in requests),
                              env={**os.environ, **self.ctx.env},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1,2,3])
        self.assertIn('debate', [t['name'] for t in responses[1]['result']['tools']])
        self.assertFalse(responses[2]['result']['isError'])
        return json.loads(responses[2]['result']['content'][0]['text'])

    def test_stdio_mcp_routes_authenticated_debate_calls(self):
        with patch('codex_runner.run_codex', return_value=('最终发言', None, 'test-model')) as run:
            self.assert_complete(self.mcp(), run)

    @unittest.skipUnless(shutil.which('codex') and shutil.which('bwrap'), '需要主机沙箱工具')
    def test_readonly_blocks_cli_socket_but_host_mcp_routes_tool(self):
        cmd = ['codex', 'sandbox', '-c', 'sandbox_mode="read-only"', '--',
               str(ROOT/'fbtool'), 'debate', '--topic', '测试辩题']
        proc = subprocess.run(cmd, env={**os.environ, **self.ctx.env},
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('Operation not permitted', proc.stdout)
        self.assertIsNone(self.ctx.debate)
        with patch('codex_runner.run_codex', return_value=('最终发言', None, 'test-model')) as run:
            self.assert_complete(self.mcp(), run)
        self.assertFalse(self.ctx.unsafe)

    def test_validation_partial_failure_and_audit(self):
        for topic in ('', 'a'*2001, 42):
            with self.assertRaises(ValueError):
                self.ctx.dispatch('debate', {'topic':topic})
        with patch('codex_runner.run_codex', side_effect=[('发言', None, 'm'),
                                                       ('[error] 模型不可用', None, None)]):
            with self.assertRaisesRegex(RuntimeError, '反方第1轮失败'):
                self.ctx.dispatch('debate', {'topic':'测试'})
        self.assertEqual(self.ctx.debate['sent'], 1)
        self.assertFalse(self.ctx.debate['completed'])
        self.assertEqual(self.bot.db.call_args.args[3], 'debate-error')
        self.assertFalse(self.bot.db.call_args.kwargs['ok'])

    def test_cancelled_context_rejects_all_operations(self):
        self.ctx.cancelled.set()
        for op, args in [('send_text', {'text':'已结束'}), ('debate', {'topic':'测试'})]:
            with self.assertRaisesRegex(ValueError, '任务已结束'):
                self.ctx.dispatch(op, args)
        self.bot._push_reply.assert_not_called()

    def test_bind_exit_cancels_running_debate_without_waiting_for_full_round_timeout(self):
        started = threading.Event()
        outcomes = []
        def run(topic, cwd, emit, *, deadline, cancelled):
            started.set()
            if not cancelled.wait(2):
                raise AssertionError('任务收尾没有取消辩论')
            raise RuntimeError('辩论已取消')
        def request():
            with self.ctx.lock:
                try:
                    self.ctx.dispatch('debate', {'topic':'测试'})
                except RuntimeError as e:
                    outcomes.append(str(e))
        with patch('core.debate.run', side_effect=run):
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(started.wait(2))
            self.binding.__exit__(None, None, None)
            self.binding = contextlib.nullcontext()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ['辩论已取消'])
        self.bot._push_reply.assert_not_called()


class DebateBotTests(unittest.TestCase):
    def execute(self, text, *, status=None, result='模拟的双方发言', seed=None):
        from test_bot_execution import BotExecutionTests
        bot = BotExecutionTests().make_bot()
        if seed:
            bot.sessions['list'][0]['seed'] = seed
        bot.execute_codex.return_value = result, None, 'test-model'
        ctx = Mock(env={}, restart=False, compact=False, files=0, debate=status)
        with patch('bot.tool_bridge.bind', return_value=contextlib.nullcontext(ctx)), \
             patch('bot.ops_snapshot.take_quiet'), patch('bot.ops_snapshot.promote_quiet'), \
             patch('bot.usage.report_inline', return_value=None), patch('builtins.print'):
            bot._execute_job({'kind':'task', 'text':text, 'msg_id':'test-message'})
        return bot, bot._push_reply.call_args.args[1]

    def test_missing_real_debate_becomes_error_and_does_not_enter_history(self):
        bot, output = self.execute('辩论话题：测试')
        self.assertIn('[error] 独立辩论未完成', output)
        self.assertIn('0/10', output)
        self.assertNotIn('模拟的双方发言', output)
        self.assertEqual(bot.sessions['history'], [{'role':'user','content':'旧对话事实'}])

    def test_partial_failed_debate_overrides_simulated_fallback(self):
        _, output = self.execute('进行独立辩论', status={'completed':False,'sent':3,'error':'429'})
        self.assertIn('3/10', output)
        self.assertIn('429', output)
        self.assertNotIn('模拟', output)

    def test_completed_debate_retains_final_synthesis(self):
        _, output = self.execute('辩题：测试', status={'completed':True,'sent':10}, result='最终结论')
        self.assertIn('最终结论', output)
        self.assertNotIn('[error]', output)

    def test_main_model_error_is_preserved_if_debate_was_never_called(self):
        _, output = self.execute('辩论话题：测试', result='[error] 额度已用尽')
        self.assertIn('额度已用尽', output)
        self.assertNotIn('模型没有调用', output)

    def test_instruction_survives_compacted_seed_and_inspection_is_ordinary_task(self):
        bot, _ = self.execute('辩题：测试', seed='旧上下文')
        prompt = bot.execute_codex.call_args.kwargs['run_text']
        self.assertIn('要求真实的独立辩论', prompt)
        self.assertIn('旧上下文', prompt)
        _, output = self.execute('检查 feishu-debate 代码', result='检查完成')
        self.assertIn('检查完成', output)
        self.assertNotIn('[error]', output)


class McpProtocolTests(unittest.TestCase):
    def test_unknown_tools_cannot_escape_allowlist_and_rpc_errors_are_explicit(self):
        from core.mcp import serve
        call = Mock(return_value={'ok':False, 'error':'令牌失效'})
        requests = [{'jsonrpc':'2.0','id':1,'method':'tools/call',
                     'params':{'name':'unsafe','arguments':{}}},
                    {'jsonrpc':'2.0','id':2,'method':'tools/call',
                     'params':{'name':'debate','arguments':{'topic':'测试'}}}]
        source = io.StringIO(''.join(json.dumps(r)+'\n' for r in requests))
        sink = io.StringIO()
        serve(call, source, sink)
        responses = [json.loads(r) for r in sink.getvalue().splitlines()]
        self.assertIn('error', responses[0])
        self.assertTrue(responses[1]['result']['isError'])
        self.assertIn('令牌失效', responses[1]['result']['content'][0]['text'])
        call.assert_called_once_with('debate', {'topic':'测试'})


class DebateRunnerCancellationTests(unittest.TestCase):
    def test_cancellation_kills_process_group_and_tolerates_exit_race(self):
        for exited in (False, True):
            event = threading.Event()
            event.set()
            proc = Mock(pid=123, returncode=-9)
            proc.communicate.return_value = '', ''
            with patch('codex_runner.require_chatgpt_login'), \
                 patch('codex_runner.subprocess.Popen', return_value=proc), \
                 patch('codex_runner.os.killpg', side_effect=ProcessLookupError if exited else None) as kill:
                result, _, _ = codex_runner.run_codex('测试', '/tmp', 30, cancel_event=event)
            self.assertIn('任务已取消', result)
            kill.assert_called_once_with(123, signal.SIGKILL)

    def test_polling_does_not_resend_prompt(self):
        proc = Mock(pid=123, returncode=0)
        proc.communicate.side_effect = [subprocess.TimeoutExpired('codex', 1),
                                       ('{"type":"turn.completed"}\n', '')]
        def popen(cmd, **kwargs):
            Path(cmd[cmd.index('--output-last-message')+1]).write_text('发言')
            return proc
        with patch('codex_runner.require_chatgpt_login'), \
             patch('codex_runner.subprocess.Popen', side_effect=popen), \
             patch('codex_runner.session_context', return_value=None):
            result, _, _ = codex_runner.run_codex('测试', '/tmp', 30, cancel_event=threading.Event())
        self.assertEqual(result, '发言')
        self.assertEqual(proc.communicate.call_args_list[0].args, ('测试',))
        self.assertEqual(proc.communicate.call_args_list[1].args, (None,))


if __name__ == '__main__':
    unittest.main()
