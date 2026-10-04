#!/usr/bin/env python3
"""兼容脚本入口：独立辩论在已认证的机器人工具服务内同步执行。"""
import argparse
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[4]


def debate(topic):
    call = runpy.run_path(str(ROOT / 'fbtool'))['call']
    response = call('debate', {'topic': topic})
    if not response.get('ok'):
        raise RuntimeError(response.get('error', '独立辩论失败'))
    return response['result']


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
