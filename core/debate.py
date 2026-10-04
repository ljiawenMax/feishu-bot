"""独立正反方的纯文本辩论；调用方负责逐轮外发与审计。"""
import re
import time

import codex_runner

TOPIC_RE = re.compile(r'^(?:辩论话题|辩题)\s*[:：]\s*\S', re.S)


def required(text):
    return bool(TOPIC_RE.match(text.strip())
                or re.match(r'^\$feishu-debate(?:\s|$)', text.strip()))


def run(topic, cwd, emit, *, rounds=5, timeout=180, deadline=None, cancelled=None):
    histories = {'正方': [], '反方': []}
    statements = []
    last = ''
    for turn in range(1, rounds + 1):
        for side in ('正方', '反方'):
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError('辩论已取消')
            remaining = deadline - time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise RuntimeError(f'辩论总时限已到（已完成 {len(statements)}/{rounds * 2} 条发言）')
            prompt = (f'辩题：{topic}\n你是{side}，只维护你方立场。第{turn}/{rounds}轮。'
                      '回应对方论据并提出你方论据，300字以内，只输出最终发言。'
                      + (f'\n对方上一轮最终发言：\n{last}' if last else '\n你先开篇立论。')
                      + ('\n最后一轮，请收束你方立场。' if turn == rounds else ''))
            started = time.monotonic()
            result, _, model = codex_runner.run_codex(
                codex_runner.build_task_with_history(prompt, histories[side]),
                cwd, min(timeout, remaining), report_actions=False, lightweight=True,
                parent_bound=True, cancel_event=cancelled)
            if result.startswith('[error]'):
                raise RuntimeError(f'{side}第{turn}轮失败：{result}')
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError('辩论已取消，不再推送发言')
            statement = {'side': side, 'round': turn, 'text': result, 'model': model,
                         'elapsed_ms': int((time.monotonic() - started) * 1000)}
            emit(statement)
            statements.append(statement)
            histories[side].extend([{'role': 'user', 'content': prompt},
                                    {'role': 'assistant', 'content': result}])
            last = result
    return {'completed': True, 'rounds': rounds, 'statements': statements}
