import argparse
import json
import os
import subprocess
import time

import requests

BASE_DIR = os.path.dirname(__file__)


def load_env_file(env_name):
    env_file = os.path.join(BASE_DIR, f".env.{env_name}")
    env_vars = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


_token_cache = {"token": None, "expires_at": 0.0}


def get_token(app_id, app_secret):
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get token failed: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200) - 60  # 提前 60s 刷新
    return _token_cache["token"]


def fetch_new_messages(token, chat_id, since_ts):
    """拉取 since_ts 之后的用户消息，返回 list of {id, text, create_time}"""
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": since_ts,
        "page_size": 20,
    }
    resp = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"fetch messages failed: {data}")

    items = data.get("data", {}).get("items", [])
    messages = []
    for item in items:
        if item.get("msg_type") != "text":
            continue
        # 过滤 bot 自身发出的消息
        if item.get("sender", {}).get("sender_type") != "user":
            continue
        try:
            content = json.loads(item["body"]["content"])
            text = content.get("text", "").strip()
        except Exception:
            continue
        if not text:
            continue
        messages.append({
            "id": item["message_id"],
            "text": text,
            "create_time": item["create_time"],
        })
    return messages


def build_dir_prompt(dir_names):
    lines = ["请选择工作目录，回复序号："]
    for i, name in enumerate(dir_names, 1):
        lines.append(f"{i}. {name}")
    return "\n".join(lines)


def run_claude(task, work_dir, timeout):
    result = subprocess.run(
        ["claude", "-p", task],
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout.strip()
    if not output and result.stderr.strip():
        output = result.stderr.strip()
    return output or "(no output)"


def reply_message(token, message_id, text):
    MAX_LEN = 3900
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n\n[输出过长，已截断]"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        headers=headers,
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        print(f"[warn] reply failed: {data}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="prod", choices=["test", "prod"], help="运行环境")
    args = parser.parse_args()

    env = load_env_file(args.env)

    app_id = env["APP_ID"]
    app_secret = env["APP_SECRET"]
    chat_id = env["CHAT_ID"]
    work_dirs = json.loads(env["WORK_DIRS"])
    dir_names = list(work_dirs.keys())
    poll_interval = int(env["POLL_INTERVAL"])
    task_timeout = int(env["TASK_TIMEOUT"])

    default_name = "daily-assistant"
    last_dir = {
        "name": default_name,
        "path": work_dirs[default_name],
    }
    awaiting_dir_selection = False
    last_ts = str(int(time.time()))
    current_interval = 5

    print(f"[feishu-claude-bot] env={args.env}, backoff 5s~{poll_interval}s")
    print(f"  work_dirs: {dir_names}")
    print(f"  default:   {default_name}")
    print(f"  chat_id:   {chat_id}")

    while True:
        try:
            token = get_token(app_id, app_secret)
            messages = fetch_new_messages(token, chat_id, last_ts)

            if messages:
                current_interval = 5
            else:
                current_interval = min(current_interval * 4, poll_interval)

            for msg in messages:
                text = msg["text"]
                last_ts = str(int(msg["create_time"]) // 1000 + 1)

                if text.strip().lower() == "ls":
                    # 触发目录选择菜单
                    awaiting_dir_selection = True
                    reply_message(token, msg["id"], build_dir_prompt(dir_names))
                    continue

                if awaiting_dir_selection and text.strip().isdigit():
                    # 选择目录
                    idx = int(text.strip()) - 1
                    if 0 <= idx < len(dir_names):
                        last_dir = {"name": dir_names[idx], "path": work_dirs[dir_names[idx]]}
                        awaiting_dir_selection = False
                        reply_message(token, msg["id"], f"已切换到 [{last_dir['name']}]")
                        print(f"[dir] switched to {last_dir['name']}")
                    else:
                        reply_message(token, msg["id"], f"无效序号，请输入 1～{len(dir_names)} 之间的数字")
                    continue

                # 其他消息均视为任务，在当前目录执行
                awaiting_dir_selection = False
                print(f"[task] dir={last_dir['name']} | {text[:60]}")
                reply_message(token, msg["id"], f"正在 [{last_dir['name']}] 执行任务，请稍候…")
                try:
                    result = run_claude(text, last_dir["path"], task_timeout)
                except subprocess.TimeoutExpired:
                    result = f"[error] 任务超时（>{task_timeout}s）"
                except Exception as e:
                    result = f"[error] 执行失败: {e}"
                reply_message(token, msg["id"], result)
                print(f"[done] replied to {msg['id']}")

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(5 if awaiting_dir_selection else current_interval)


if __name__ == "__main__":
    main()
