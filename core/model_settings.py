"""账户模型目录、有效默认配置和明确的本次任务设置。不会修改 Codex 配置。"""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import codex_runner

_cache = {}
_lock = threading.Lock()
_workspace_locks = {}
_TTL = 60
_CACHE_DIR = Path.home() / '.cache' / 'feishu-bot' / 'model-settings'


def _config_signature(key):
    """配置变更后重新确认默认值；缓存不读取凭证，也不保存完整配置。"""
    home, work_dir = map(Path, key)
    paths = {home / 'config.toml', home / 'requirements.toml',
             Path('/etc/codex/config.toml'), Path('/etc/codex/requirements.toml')}
    paths.update(home.glob('*.config.toml'))
    cwd = Path(work_dir)
    paths.update(parent / '.codex' / 'config.toml' for parent in (cwd, *cwd.parents))
    signature = []
    for path in sorted(paths):
        try:
            stat = path.stat()
            version = [stat.st_mtime_ns, stat.st_size, stat.st_ino]
        except FileNotFoundError:
            version = None
        signature.append([str(path), version])
    return signature


def _snapshot_path(key):
    digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
    return _CACHE_DIR / (digest + '.json')


def _load_snapshot(key, signature):
    path = _snapshot_path(key)
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        saved = json.loads(path.read_text())
        if (saved['version'] != 1 or saved['key'] != list(key)
                or saved['signature'] != signature):
            return None
        data = saved['data']
        if not isinstance(data['model'], str) or not isinstance(data['models'], list) or not data['models']:
            return None
        if data['effort'] is not None and not isinstance(data['effort'], str):
            return None
        for model in data['models']:
            if (not isinstance(model['model'], str) or not isinstance(model['efforts'], list)
                    or not all(isinstance(e, str) for e in model['efforts'])):
                return None
            if model['default_effort'] is not None and not isinstance(model['default_effort'], str):
                return None
        saved_at = float(saved['saved_at'])
        age = max(0, time.time() - saved_at)
        return {'data': data, 'signature': signature, 'saved_at': saved_at,
                'checked_at': time.monotonic() - age}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_snapshot(key, cached):
    """只保存目录与模型默认值；原子替换，重启时也可离线恢复。"""
    temp_path = None
    try:
        _CACHE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=_CACHE_DIR, delete=False) as f:
            temp_path = f.name
            json.dump({'version': 1, 'key': key, 'signature': cached['signature'],
                       'saved_at': cached['saved_at'], 'data': cached['data']}, f)
        os.replace(temp_path, _snapshot_path(key))
    except OSError as e:
        # 无法落盘不能阻止本进程继续使用已确认的内存缓存。
        print(f'[model-settings][cache-write-error] {e}')
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def fetch(work_dir):
    def query(rpc):
        config = rpc('config/read', 3, {'includeLayers': False, 'cwd': work_dir}).get('config') or {}
        models, cursor, seen = [], None, set()
        for ident in range(4, 24):
            params = {'limit': 100, 'includeHidden': False}
            if cursor:
                params['cursor'] = cursor
            page = rpc('model/list', ident, params)
            for item in page.get('data') or []:
                if item.get('hidden'):
                    continue
                models.append({
                    'model': item['model'], 'name': item.get('displayName') or item['model'],
                    'efforts': [v['reasoningEffort'] for v in item.get('supportedReasoningEfforts') or []],
                    'default_effort': item.get('defaultReasoningEffort'),
                    'is_default': bool(item.get('isDefault')),
                })
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError('模型列表分页游标重复')
            seen.add(cursor)
        else:
            raise RuntimeError('模型列表分页过多')
        if not models:
            raise RuntimeError('Codex 未返回可选模型')
        default = next((m for m in models if m['is_default']), models[0])
        return {'models': models, 'model': config.get('model') or default['model'],
                'effort': config.get('model_reasoning_effort')}

    data, _ = codex_runner.read_account_data(query, work_dir=work_dir)
    return data


def catalog(work_dir, *, for_task=False, refresh=False):
    """普通任务复用已确认的目录；显式查询才按 TTL 刷新，临时故障时保留旧目录。"""
    home = os.path.abspath(os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex'))
    key = (home, os.path.abspath(work_dir))
    with _lock:
        workspace_lock = _workspace_locks.setdefault(key, threading.Lock())
    # 相同目录共用缓存；不同群的独立工作目录查询不会互相阻塞。
    with workspace_lock:
        signature = _config_signature(key)
        cached = _cache.get(key)
        if cached and cached['signature'] != signature:
            cached = None
            _cache.pop(key, None)
        cached = cached or _load_snapshot(key, signature)
        if cached:
            _cache[key] = cached
            if not refresh and (for_task or time.monotonic() - cached['checked_at'] < _TTL):
                return cached['data']
        try:
            data = fetch(key[1])
        except (OSError, RuntimeError, codex_runner.RateLimited) as e:
            if not cached:
                raise
            print(f'[model-settings][refresh-error] cwd={key[1]} {type(e).__name__}: {e}')
            return dict(cached['data'], _warning='目录暂时无法刷新，使用上次确认的列表',
                        _cached_at=cached['saved_at'])
        # 查询过程中配置发生变更时，不能把旧结果绑定到新配置上。
        if _config_signature(key) != signature:
            raise RuntimeError('查询期间 Codex 配置已改变，请重试')
        cached = {'data': data, 'signature': signature,
                  'checked_at': time.monotonic(), 'saved_at': time.time()}
        _cache[key] = cached
        _save_snapshot(key, cached)
        return data


def resolve(entry, data, overrides=None):
    """分别按本次指定、会话设置、Codex 默认配置解析并校验两项设置。"""
    entry, overrides = entry or {}, overrides or {}
    model = overrides.get('model', entry.get('model')) or data['model']
    selected = next((m for m in data['models'] if m['model'] == model), None)
    if selected is None:
        raise ValueError(f'模型 {model} 不在当前 Codex 可选列表中；发送 /model 查看列表')
    effort = overrides.get('effort', entry.get('effort')) or data['effort'] or selected['default_effort']
    if effort not in selected['efforts']:
        raise ValueError(f'{model} 不支持 effort {effort}；可选：' + ' / '.join(selected['efforts']))
    effort_source = _source('effort', entry, overrides)
    if not data['effort'] and effort_source in ('Codex 默认配置', '本次使用默认'):
        effort_source = '模型默认档位'
    return {'model': model, 'effort': effort,
            'model_source': _source('model', entry, overrides),
            'effort_source': effort_source}


def _source(key, entry, overrides):
    if key in overrides:
        return '本次指定' if overrides[key] else '本次使用默认'
    return '当前对话' if entry.get(key) else 'Codex 默认配置'


_MODEL = re.compile(r'^(?:这次|本次|这一轮)(?:请)?(?:用|使用)\s*'
                    r'(?P<model>gpt-[A-Za-z0-9_.-]+|default|effort)(?=[\s，,；;：:]|$)', re.I)
_EFFORT = re.compile(r'^[\s，,；;]*(?:effort(?:\s+level)?|推理强度|思考强度)'
                     r'\s*(?:[=:：]|设为|用)?\s*(?P<effort>[A-Za-z][A-Za-z0-9_-]*)'
                     r'(?=[\s，,；;：:]|$)', re.I)
_ONCE_EFFORT = re.compile(r'^(?:这次|本次|这一轮)(?:请)?(?:用|使用)?\s*', re.I)


def parse_once(text):
    """仅解析消息开头的明确本次指令，不从正文、引用或历史中抽取设置。"""
    overrides, rest = {}, text
    match = _MODEL.match(rest)
    if match:
        # “这次用 effort high”属于仅指定 effort。
        if match['model'].lower() != 'effort':
            overrides['model'] = _value(match['model'])
            rest = rest[match.end():]
    if overrides:
        match = _EFFORT.match(rest)
    else:
        prefix = _ONCE_EFFORT.match(rest)
        match = _EFFORT.match(rest[prefix.end():]) if prefix else None
        if match:
            rest = rest[prefix.end():]
    if match:
        overrides['effort'] = _value(match['effort'])
        rest = rest[match.end():]
    if not overrides:
        return text, {}
    rest = rest.lstrip(' \t\r\n，,；;：:')
    if not rest:
        raise ValueError('单次指定后请附上任务，例如：这次用 gpt-6.1-sol，effort high，审查方案')
    return rest, overrides


def _value(value):
    value = value.lower()
    if len(value) > 64:
        raise ValueError('模型或 effort 名称过长')
    return None if value == 'default' else value
