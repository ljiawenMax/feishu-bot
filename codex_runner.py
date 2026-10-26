"""ChatGPT 订阅执行层：Codex CLI JSONL、会话恢复与磁盘会话读取。

DB 保留历史列 claude_session_id；新 ID 加 codex: 前缀，防止把 Claude UUID
传给 Codex resume。旧记录只读恢复，不调用 Claude、也不删除旧文件。
"""

import collections
import queue
import threading
import time
import sys
import glob
import json
import os
import signal
import subprocess
import tempfile
import tomllib
import uuid

import claude_runner as legacy

SYNC_SYSTEM_PROMPT = (
    "你在一次性非交互任务中执行工作。必须在本轮内同步完成，并在最终回复输出结果；"
    "不要承诺后台运行或稍后通知。"
    "用户需要取文件时，可在最终正文单独一行写 [[SEND_FILE:/绝对路径]]；"
    "须为已存在普通文件，每个≤20MB，每轮≤3个。"
)
WIKI_SYSTEM_PROMPT = legacy.WIKI_SYSTEM_PROMPT
format_actions = legacy.format_actions
PREFIX = "codex:"
LEGACY_MODELS = {"fable", "opus", "sonnet", "haiku"}


def cli_env(tool_env=None):
    env = dict(os.environ)
    # 订阅接入不能被服务环境里的 API key 切换成按量计费。
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    # 内部摘要/辩手不能继承主任务的外发与重启能力。
    for key in list(env):
        if key.startswith('FEISHU_'):
            env.pop(key)
    if tool_env:
        env.update(tool_env)
    return env


class NoCredentials(Exception):
    pass


class TokenExpired(Exception):
    pass


class RateLimited(Exception):
    pass


def read_account_data(request, timeout=20, work_dir=None):
    """短生命周期只读 RPC：初始化、确认订阅身份、执行查询、关闭进程。"""
    try:
        proc = subprocess.Popen(["codex", "app-server", "-c", 'model_provider="openai"'],
                                env=cli_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True,
                                cwd=work_dir)
    except FileNotFoundError:
        raise NoCredentials("未安装 Codex CLI，请先安装并执行 codex login") from None
    incoming = queue.Queue()

    def read():
        try:
            for line in proc.stdout:
                try:
                    incoming.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            incoming.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def rpc(method, ident, params=None):
        send({"method": method, "id": ident, "params": params or {}})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex 查询超时（{method}）")
            try:
                message = incoming.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"Codex 查询超时（{method}）") from None
            if message is None:
                raise RuntimeError("Codex app-server 已退出")
            if not isinstance(message, dict):
                continue
            if message.get("id") != ident:
                continue  # 忽略异步通知；这里不启用会触发审批的任务
            if "error" in message:
                detail = message["error"].get("message", "Codex 查询失败")
                lower = detail.lower()
                if "401" in lower or "unauthorized" in lower:
                    raise TokenExpired("登录已失效，请执行 codex login 重新登录")
                if "429" in lower or "rate limit" in lower:
                    raise RateLimited(detail)
                raise RuntimeError(f'Codex {method} 失败：{detail}')
            return message.get("result") or {}

    try:
        rpc("initialize", 1, {"clientInfo": {"name": "feishu_bot", "version": "1.0"}})
        send({"method": "initialized", "params": {}})
        # 查询目录/额度无需强制刷新凭证；凭证到期由 Codex 的正常执行流程处理。
        account = rpc("account/read", 2, {"refreshToken": False}).get("account") or {}
        if account.get("type") != "chatgpt":
            raise NoCredentials("请在运行服务的用户下执行 codex login，以 ChatGPT 订阅登录")
        return request(rpc), account.get("planType")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        reader.join(timeout=1)
        proc.stdout.close()


def require_chatgpt_login():
    try:
        result = subprocess.run(["codex", "login", "status"], env=cli_env(),
                                capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError("未安装 Codex CLI，请先安装 @openai/codex") from None
    if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
        raise RuntimeError("请在运行服务的用户下执行 codex login，以 ChatGPT 订阅登录")


def _raw_id(session_id):
    if not session_id or not session_id.startswith(PREFIX):
        return None
    try:
        return str(uuid.UUID(session_id[len(PREFIX):]))
    except ValueError:
        return None


def _session_files():
    base = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return glob.glob(os.path.join(base, "sessions", "**", "rollout-*.jsonl"), recursive=True)


def _session_path(session_id):
    sid = _raw_id(session_id)
    if not sid:
        return None
    return next((p for p in _session_files() if p.endswith(f"-{sid}.jsonl")), None)


def _events(path):
    if not path:
        return
    try:
        with open(path) as f:
            for line in f:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        yield event
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def build_task_with_history(task, history):
    if not history:
        return task
    lines = ["[以下是之前的对话记录，请根据此继续]"]
    for turn in history:
        role = "用户" if turn["role"] == "user" else "助手"
        lines.append(f"{role}: {turn.get('content') or ''}")
    lines.append(f"\n[用户新消息]\n{task}")
    return "\n".join(lines)


def run_codex(task, work_dir, timeout, session_id=None, permit=False, extra_dirs=None,
              history=None, model=None, unsafe=False, extra_system=None, report_actions=True,
              tool_env=None, lightweight=False, parent_bound=False, cancel_event=None):
    try:
        require_chatgpt_login()
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return f"[error] {e}", None, None
    if model in LEGACY_MODELS or (model and model.startswith("claude-")):
        model = None  # 旧会话的 Claude 模型选择不适用于 GPT；沿用 Codex 默认模型
    sid = _raw_id(session_id)
    if session_id and not _session_path(session_id):
        recovery = history or session_tail(session_id, work_dir, rounds=20, max_chars=12000)
        task = build_task_with_history(task, recovery)
        sid = None
    instructions = SYNC_SYSTEM_PROMPT
    if extra_system:
        instructions += "\n\n" + extra_system
    if tool_env:
        instructions += (
            "\n\n本轮可用飞书工具：运行 \"$FEISHU_TOOL_CLI\" --help 查看入口。"
            "附件、知识库、压缩、重启、语音需求可读取 feishu-tools skill；"
            "本轮已提供 feishu_bot MCP 工具，优先直接调用，沙箱内 CLI 可能无法连接 socket。需要独立正反方辩论时读取 feishu-debate skill，调用 MCP debate 工具。"
            "脚本或工具失败时必须说明独立辩论未完成，不得自行扮演双方冒充独立辩论。"
        )
    if extra_dirs:
        # Codex 的 --add-dir 授予写权限，不能用于旧配置中承诺的只读资料目录。
        instructions += "\n\n可读取的附件/参考资料目录（请勿修改）：\n" + "\n".join(extra_dirs)
    sandbox = "danger-full-access" if unsafe else "workspace-write" if permit else "read-only"
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
           "-c", 'model_provider="openai"', "-c", 'approval_policy="never"',
           "-c", f'sandbox_mode="{sandbox}"',
           "-c", "sandbox_workspace_write.writable_roots=[]",
           "-c", "sandbox_workspace_write.network_access=false",
           "-c", "developer_instructions=" + json.dumps(instructions, ensure_ascii=False)]
    if tool_env and not lightweight:
        cli = os.path.join(os.path.dirname(__file__), 'fbtool')
        options = {'command': sys.executable, 'args': [cli, '--mcp'],
                   'env_vars': sorted(tool_env), 'startup_timeout_sec': 10,
                   'tool_timeout_sec': timeout + 15, 'required': True, 'enabled': True,
                   # 仅预授权本机工具入口；具体群、外发和重启权限仍由 bot 校验。
                   'default_tools_approval_mode': 'approve'}
        for key, value in options.items():
            cmd += ['-c', 'mcp_servers.feishu_bot.' + key + '=' + json.dumps(value)]
    if model:
        cmd += ["--model", model]
    if lightweight:
        # 内部纯文本调用不载入用户的 MCP/apps/plugins，也不持久化摘要会话。
        # CLI 没有 Claude 的 --tools 参数；使用本机 CLI 支持的 feature 开关。
        if not model:
            try:
                config_home = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
                with open(os.path.join(config_home, 'config.toml'), 'rb') as f:
                    default_model = tomllib.load(f).get('model')
                if default_model:
                    model = default_model
                    cmd += ['--model', default_model]
            except (OSError, ValueError):
                pass
        cmd += ['--ignore-user-config', '-c', 'web_search="disabled"']
        for feature in ('shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use',
                        'image_generation', 'computer_use', 'multi_agent', 'view_image',
                        'sleep_tool', 'goals', 'code_mode_host'):
            cmd += ['--disable', feature]
        if not sid:
            cmd += ['--ephemeral']
    # resume 没有 --sandbox / --add-dir 参数，用同一组 config 覆盖恢复时权限。
    if sid:
        cmd += ["resume", sid]
    with tempfile.TemporaryDirectory(prefix="feishu-codex-") as tmp:
        output = os.path.join(tmp, "reply.txt")
        cmd += ["--output-last-message", output, "-"]
        if parent_bound:
            if not sys.platform.startswith("linux"):
                raise RuntimeError("父进程退出保护需要 Linux")
            cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "core", "parent_guard.py"),
                   str(os.getpid()), *cmd]
        proc = subprocess.Popen(cmd, cwd=work_dir, env=cli_env(tool_env), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        timed_out = False
        try:
            if cancel_event is None:
                stdout, stderr = proc.communicate(task, timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                first = True
                while True:
                    if cancel_event.is_set():
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        stdout, stderr = proc.communicate()
                        return "[error] 任务已取消", None, None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    try:
                        stdout, stderr = proc.communicate(task if first else None,
                                                         timeout=min(remaining, 1))
                        break
                    except subprocess.TimeoutExpired:
                        first = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        events = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
        new_sid, messages, actions, errors, completed = None, [], [], [], False
        for event in events:
            kind = event.get("type")
            if kind == "thread.started" and event.get("thread_id"):
                new_sid = PREFIX + event["thread_id"]
            elif kind == "turn.completed":
                completed = True
            elif kind in ("turn.failed", "error"):
                err = event.get("error") or event.get("message") or "Codex 执行失败"
                errors.append(err.get("message", str(err)) if isinstance(err, dict) else str(err))
            elif kind == "item.completed":
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "agent_message":
                    messages.append(item.get("text") or "")
                elif item.get("type") == "command_execution":
                    actions.append(("bash", " ".join((item.get("command") or "").split())))
                elif item.get("type") == "file_change" and item.get("status") == "completed":
                    actions.extend(("write", c["path"]) for c in item.get("changes", []) if c.get("path"))
        result = ""
        if os.path.isfile(output):
            with open(output) as f:
                result = f.read().strip()
        result = result or (messages[-1].strip() if messages else "")
    try:
        info = session_context(new_sid or session_id, work_dir)
    except Exception as e:
        print(f"[codex][metadata-error] {e}")
        info = None
    used_model = (info or {}).get("model") or model
    if timed_out:
        result = f"[error] 任务超时（>{timeout}s），响应可能不完整\n{result}"
    elif proc.returncode or not completed:
        detail = "; ".join(errors) or stderr.strip() or result or "Codex 未完成本轮执行"
        result = "[error] " + format_codex_error(detail)
    elif not result:
        result = "[error] Codex 未返回回复正文"
    if report_actions:
        result += format_actions(actions, work_dir)
    return result, None if lightweight and not sid else new_sid, used_model


def format_codex_error(message):
    lower = message.lower()
    if "usage limit" in lower or "quota" in lower:
        return f"额度已用尽（ChatGPT 订阅使用限额），请等待重置：{message}"
    if "401" in lower or "unauthorized" in lower:
        return f"认证失败，请执行 codex login 重新登录：{message}"
    if "429" in lower or "rate limit" in lower:
        return f"请求受限（限流或额度不足）：{message}"
    return message


def session_context(session_id, work_dir):
    if not _raw_id(session_id):
        return None
    model, info, user_turns = None, None, 0
    for event in _events(_session_path(session_id)):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "turn_context":
            model = payload.get("model") or model
        elif event.get("type") == "event_msg":
            if payload.get("type") == "token_count" and payload.get("info"):
                info = payload["info"]
            elif payload.get("type") == "user_message":
                user_turns += 1
    if not info:
        return {"model": model} if model else None
    last = info.get("last_token_usage") or {}
    return {"input": last.get("input_tokens", 0), "cache_creation": 0,
            "cache_read": last.get("cached_input_tokens", 0),
            "output": last.get("output_tokens", 0),
            # Codex input_tokens 已含 cached_input_tokens，不能重复加。
            "total_input": last.get("input_tokens", 0),
            "context_window": info.get("model_context_window"),
            "model": model, "user_turns": user_turns}


def list_all_sessions():
    results = []
    for path in _session_files():
        meta, title = {}, None
        for event in _events(path):
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "session_meta":
                meta = payload
            elif event.get("type") == "event_msg" and payload.get("type") == "user_message":
                title = (payload.get("message") or "")[:80]
                break
        if not meta.get("id"):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        results.append({"project": meta.get("cwd") or "(未知目录)", "cwd": meta.get("cwd"),
                        "session_id": PREFIX + meta["id"], "title": title, "mtime": mtime})
    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results


def session_exists(session_id, work_dir):
    return bool(_session_path(session_id))


def session_tail(session_id, work_dir, rounds=3, max_chars=600):
    if session_id and not session_id.startswith(PREFIX):
        return legacy.session_tail(session_id, work_dir, rounds=rounds, max_chars=max_chars)
    turns = collections.deque(maxlen=max(rounds * 2, 0))
    for event in _events(_session_path(session_id)):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and payload.get("phase") not in (None, "final", "final_answer"):
            continue
        text = "\n".join(b.get("text", "") for b in payload.get("content", [])
                         if b.get("type") in ("input_text", "output_text")).strip()
        if not text or text.startswith(("<", "# AGENTS.md", "You are Codex")):
            continue
        turns.append({"role": role, "content": text[:max_chars]})
    return list(turns)


def delete_codex_session(session_id, work_dir):
    path = _session_path(session_id)
    if path:
        os.remove(path)
