"""只转换发给模型的自然语言；代码、引用及资源标识保持原样。"""

import logging
import re
import threading


_HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_FENCE = re.compile(r"(?m)^[ \t]{0,3}(?P<mark>`{3,}|~{3,})[^\n]*(?:\n|$)")
# 引用也是用户标记「必须原样保留」的方式。文件名含空格时应加引号。
_LITERAL = re.compile(r'''
    (?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`)
    | "(?:\\.|[^"\\])*"
    | '(?:\\.|[^'\\])*'
    | (?P<quote>[「『“‘])
    | [a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"'`「」『』“”‘’，。；！？、（）]+
    | [\w.+-]+@[\w.-]+\.[\w-]+
    | (?<![\w])(?:[a-zA-Z]:[\\/]|[~.]*/|[\w@+.-]+[/\\])
      [^\s<>"'`「」『』“”‘’，。；：！？、（）(){}\[\]]*
    | (?<![\w])[\w@+~-]+(?:\.[\w-]+)+(?![\w])
''', re.VERBOSE)
_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”", "‘": "’"}
_CONVERTERS = threading.local()


def _get_converter():
    converter = getattr(_CONVERTERS, 'opencc', None)
    if converter is None:
        # 延迟导入，每个 worker 复用自己的词库实例，不跨线程共享原生对象。
        from opencc import OpenCC
        converter = OpenCC('s2twp.json')
        _CONVERTERS.opencc = converter
    return converter


def _convert_prose(text):
    if not _HAN.search(text):
        return text
    return _get_converter().convert(text)


def _convert_outside_literals(text):
    parts, start = [], 0
    for match in _LITERAL.finditer(text):
        if match.start() < start:
            continue
        end = match.end()
        if opening := match.group('quote'):
            # 支持同类引号嵌套；未闭合时保守地保护到正文结尾。
            depth, end = 1, len(text)
            for pos in range(match.end(), len(text)):
                if text[pos] == opening:
                    depth += 1
                elif text[pos] == _QUOTE_PAIRS[opening]:
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
        parts.extend((_convert_prose(text[start:match.start()]), text[match.start():end]))
        start = end
    parts.append(_convert_prose(text[start:]))
    return "".join(parts)


def _convert_message(text):
    parts, start = [], 0
    while opening := _FENCE.search(text, start):
        mark = opening.group("mark")
        closing = re.compile(
            r"(?m)^[ \t]{0,3}" + re.escape(mark[0])
            + "{" + str(len(mark)) + r",}[ \t]*(?:\n|$)"
        ).search(text, opening.end())
        end = closing.end() if closing else len(text)
        parts.extend((_convert_outside_literals(text[start:opening.start()]),
                      text[opening.start():end]))
        start = end
    parts.append(_convert_outside_literals(text[start:]))
    return "".join(parts)


class InputConverter:
    def __init__(self, enabled=True, log=None):
        if not isinstance(enabled, bool):
            raise ValueError("input_zh_tw 必须是 JSON 布尔值 true 或 false")
        self.enabled = enabled
        self.log = log or logging.getLogger(__name__).warning

    def convert(self, text):
        if not self.enabled or not text:
            return text
        try:
            return _convert_message(text)
        except Exception as exc:
            # 不记录异常正文，避免第三方异常把用户消息或敏感字串带进日志。
            self.log(f"[input-conversion-error] {type(exc).__name__}，沿用用户原文")
            return text
