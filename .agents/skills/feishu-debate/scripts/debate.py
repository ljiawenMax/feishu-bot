#!/usr/bin/env python3
"""两个独立上下文、仅交换最终发言，结果同步逐轮返回飞书。"""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import codex_runner


def debate(topic, rounds=5, timeout=180):
    if not all(os.environ.get(k) for k in ('FEISHU_TOOL_TOKEN', 'FEISHU_TOOL_CLI')):
        raise ValueError('辩论脚本只能在飞书任务上下文中执行')
    call = runpy.run_path(str(ROOT / 'fbtool'))['call']
    histories = {'正方': [], '反方': []}
    statements = []
    last = ''
    for turn in range(1, rounds + 1):
        for side in ('正方', '反方'):
            prompt = (f'辩题：{topic}\n你是{side}，只维护你方立场。第{turn}/{rounds}轮。'
                      '回应对方论据并提出你方论据，300字以内，只输出最终发言。'
                      + (f'\n对方上一轮最终发言：\n{last}' if last else '\n你先开篇立论。')
                      + ('\n最后一轮，请收束你方立场。' if turn == rounds else ''))
            result, _, _ = codex_runner.run_codex(
                codex_runner.build_task_with_history(prompt, histories[side]),
                os.getcwd(), timeout, report_actions=False, lightweight=True, parent_bound=True)
            if result.startswith('[error]'):
                raise RuntimeError(f'{side}第{turn}轮失败：{result}')
            response = call('send_text', {'text': f'🎙 {side} · 第{turn}/{rounds}轮\n\n{result}'})
            if not response.get('ok'):
                raise RuntimeError(response.get('error', '辩论推送失败'))
            histories[side].extend([{'role': 'user', 'content': prompt},
                                    {'role': 'assistant', 'content': result}])
            statements.append({'side': side, 'round': turn, 'text': result})
            last = result
    return {'completed': True, 'statements': statements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', required=True)
    args = parser.parse_args()
    try:
        result = debate(args.topic)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
