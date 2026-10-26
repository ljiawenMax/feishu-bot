"""输入转换的字面量边界、真实词库及飞书任务接入回归。"""

import builtins
import contextlib
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from bot import Bot
from input_conversion import InputConverter
import test_bot_execution


def to_tw(text):
    for source, target in (("软件", "軟體"), ("用户", "使用者"),
                           ("优化", "最佳化"), ("辩论话题", "辯論話題")):
        text = text.replace(source, target)
    return text


class InputConversionTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch('input_conversion._CONVERTERS', threading.local())
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_real_opencc_vocabulary_and_taiwan_phrases_config(self):
        log = Mock()
        converter = InputConverter(log=log)
        self.assertEqual(converter.convert('服务器上的软件和鼠标'), '伺服器上的軟體和滑鼠')
        log.assert_not_called()
        with patch('input_conversion._CONVERTERS', threading.local()), \
             patch('opencc.OpenCC') as factory:
            factory.return_value.convert.return_value = '軟體'
            self.assertEqual(converter.convert('软件'), '軟體')
            self.assertEqual(converter.convert('软件'), '軟體')
        factory.assert_called_once_with('s2twp.json')

    def test_literal_formats_preserved_while_surrounding_prose_converts(self):
        literals = ['`软件用户`', '`软件\n用户`', '``软件 `用户` ``', '"软件用户"', "'软件用户'",
                    '「软件用户」', '『软件用户』', '“软件用户”', '‘软件用户’',
                    '「用户「软件」用户」', '『用户『软件』用户』',
                    r'"软件\"用户"', 'https://example.com/软件?q=用户',
                    '用户@example.com', '/tmp/软件/用户.json', './软件/用户',
                    '../软件/用户', '~/软件/用户', '软件/用户',
                    r'C:\软件\用户.json', r'软件\用户.json', '软件用户.json',
                    '"/tmp/软件 用户/用户.json"']
        with patch('input_conversion._convert_prose', side_effect=to_tw):
            for literal in literals:
                with self.subTest(literal=literal):
                    self.assertEqual(InputConverter().convert(f'优化软件 {literal} 用户'),
                                     f'最佳化軟體 {literal} 使用者')

    def test_fences_including_unclosed_and_longer_delimiters(self):
        blocks = ['```python\n软件 = "用户"\n```\n',
                  '~~~text\n软件用户\n~~~\n',
                  '````md\n```\n软件用户\n```\n````\n',
                  '   ```\n软件用户\n   ```\n']
        with patch('input_conversion._convert_prose', side_effect=to_tw):
            for block in blocks:
                with self.subTest(block=block):
                    self.assertEqual(InputConverter().convert('优化软件\n' + block + '用户'),
                                     '最佳化軟體\n' + block + '使用者')
            self.assertEqual(InputConverter().convert('优化软件\n```\n软件用户'),
                             '最佳化軟體\n```\n软件用户')

    def test_disabled_and_empty_never_attempt_conversion(self):
        with patch('input_conversion._convert_prose') as convert:
            self.assertEqual(InputConverter(False).convert('软件'), '软件')
            self.assertEqual(InputConverter().convert(''), '')
        convert.assert_not_called()

    def test_failure_after_partial_conversion_returns_whole_original(self):
        text = '软件 "用户" 优化软件'
        log = Mock()
        with patch('input_conversion._convert_prose',
                   side_effect=['軟體 ', RuntimeError('sensitive user text')]):
            self.assertEqual(InputConverter(log=log).convert(text), text)
        self.assertIn('[input-conversion-error] RuntimeError', log.call_args.args[0])
        self.assertNotIn('sensitive user text', log.call_args.args[0])

    def test_missing_dependency_falls_back_but_ascii_needs_no_dependency(self):
        original_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == 'opencc':
                raise ModuleNotFoundError('opencc')
            return original_import(name, *args, **kwargs)
        log = Mock()
        with patch('builtins.__import__', side_effect=unavailable):
            self.assertEqual(InputConverter(log=log).convert('软件'), '软件')
            self.assertEqual(InputConverter(log=log).convert('Hello 😀'), 'Hello 😀')
        log.assert_called_once()

    def test_config_requires_boolean_and_defaults_to_enabled(self):
        for enabled in ('false', 0, None, {}):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                InputConverter(enabled)
        for setting in ({}, {'input_zh_tw': False}):
            with self.subTest(setting=setting), patch.object(Bot, 'db', return_value={}):
                bot = Bot(dict(name='test', app_id='app', app_secret='secret',
                               chat_id='chat', **setting), Mock(), 10)
            self.assertEqual(bot.input_converter.enabled, setting.get('input_zh_tw', True))


class BotInputConversionTests(unittest.TestCase):
    def setUp(self):
        self.bot = test_bot_execution.BotExecutionTests().make_bot()
        self.bot.input_converter = InputConverter(log=Mock())
        self.bot.task_queue = queue.Queue()
        self.bot._drain_uploads = Mock(return_value=[])
        self.bot.client = None
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target in ('bot.ops_snapshot.take_quiet', 'bot.ops_snapshot.promote_quiet',
                       'bot.usage.report_inline', 'bot.feishu_api.reply_message', 'builtins.print'):
            stack.enter_context(patch(target, return_value=None))
        stack.enter_context(patch('bot.tool_bridge._SERVER', None))
        self.convert = stack.enter_context(
            patch('input_conversion._convert_prose', side_effect=to_tw))

    def test_only_model_input_changes_audit_history_label_and_reply_stay_original(self):
        entry = self.bot.current_entry()
        entry['id'] = None
        self.bot._execute_job({'text': '优化软件', 'msg_id': 'm'})
        call = self.bot.execute_codex.call_args
        self.assertEqual(call.kwargs['run_text'], '最佳化軟體')
        self.assertEqual(call.args[0], '优化软件')
        self.assertEqual(call.kwargs['first_task'], '优化软件')
        self.assertEqual(entry['_history'][-2]['content'], '优化软件')
        self.assertEqual(entry['_history'][-1]['content'], '最终结果')
        self.assertEqual(self.bot.audit.call_args.args[:2], ('优化软件', '最终结果'))
        self.assertIn('最终结果', self.bot._push_reply.call_args.args[1])
        self.assertNotIn('最終結果', self.bot._push_reply.call_args.args[1])

    def test_seed_and_settings_parsing_use_converted_body_without_control_prefix(self):
        entry = self.bot.current_entry()
        entry.update(id=None, seed='软件用户摘要')
        self.bot.run_task({'id': 'm'}, '这次用 gpt-test，effort high，优化软件')
        job = self.bot.task_queue.get_nowait()
        self.assertEqual(job['overrides'], {'model': 'gpt-test', 'effort': 'high'})
        self.bot._execute_job(job)
        prompt = self.bot.execute_codex.call_args.kwargs['run_text']
        self.assertIn('软件用户摘要', prompt)
        self.assertIn('[用户新消息]\n最佳化軟體', prompt)
        self.assertNotIn('这次用', prompt)

    def test_automatic_compaction_also_converts_new_body(self):
        entry = self.bot.current_entry()
        entry['seed'] = '软件用户摘要'
        self.bot._context_guard = Mock(return_value=entry)
        self.bot._execute_job({'text': '优化软件', 'msg_id': 'm'})
        prompt = self.bot.execute_codex.call_args.kwargs['run_text']
        self.assertIn('软件用户摘要', prompt)
        self.assertIn('[用户新消息]\n最佳化軟體', prompt)

    def test_upload_context_and_wiki_reference_remain_original(self):
        self.bot.wiki_space_id = 'wiki'
        self.bot._wiki_reference = Mock(return_value='软件用户参考资料')
        self.bot._drain_uploads.return_value = [
            {'path': '/tmp/软件 用户/截图.png', 'size_human': '1 KB'}]
        self.bot.run_task({'id': 'm', 'sender_id': 'sender'}, '优化软件')
        job = self.bot.task_queue.get_nowait()
        block = job['upload_context']
        self.bot._execute_job(job)
        prompt = self.bot.execute_codex.call_args.kwargs['run_text']
        self.assertIn('最佳化軟體' + block, prompt)
        self.assertIn('软件用户参考资料', prompt)
        self.assertTrue(prompt.startswith('[飞书用户: sender]'))
        self.bot._wiki_reference.assert_called_once_with(job['task_text'])
        self.assertEqual(self.bot.audit.call_args.args[0], '优化软件' + block)

    def test_direct_compact_is_recognized_before_conversion(self):
        self.bot._do_compact = Mock(return_value=True)
        self.bot._execute_job({'text': '压缩一下上下文', 'msg_id': 'm'})
        self.bot._do_compact.assert_called_once()
        self.bot.execute_codex.assert_not_called()
        self.convert.assert_not_called()

    def test_slash_command_arguments_remain_original(self):
        handler = Mock()
        self.bot.pending = None
        self.bot.commands = {'/name': handler}
        msg = {'id': 'm', 'text': '/name 软件用户'}
        self.bot.handle_message(msg)
        handler.assert_called_once_with(msg, '软件用户')
        self.convert.assert_not_called()

    def test_debate_is_recognized_before_conversion(self):
        self.bot._execute_job({'text': '辩论话题：软件', 'msg_id': 'm'})
        prompt = self.bot.execute_codex.call_args.kwargs['run_text']
        self.assertIn('调用 feishu_bot 的 MCP debate 工具', prompt)
        self.assertIn('辯論話題：軟體', prompt)

    def test_switch_off_and_failure_both_reach_model_with_original(self):
        for enabled, failure in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, failure=failure):
                self.bot.input_converter.enabled = enabled
                self.convert.side_effect = RuntimeError('failed') if failure else to_tw
                self.bot._execute_job({'text': '优化软件', 'msg_id': 'm'})
                self.assertEqual(self.bot.execute_codex.call_args.kwargs['run_text'], '优化软件')


if __name__ == '__main__':
    unittest.main()
