"""入口：解析 --env <name>，读取 .env.<name>（默认 local），单进程内为每个机器人
起一个线程。

一个进程跑 1~N 个机器人（.env 里的 BOTS 列表），共享数据库 engine；每个机器人
（一个 chat_id）在自己的线程里串行处理消息，互不并发也互不阻塞。
"""

import argparse
import json
import os
import threading

import db
from bot import Bot

BASE_DIR = os.path.dirname(__file__)


def _bracket_balance(text):
    """返回 text 中未闭合的 [ / { 数量（字符串内的括号不计），用于识别跨行 JSON 值。"""
    depth = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth


def load_env_file(env_name):
    env_file = os.path.join(BASE_DIR, f".env.{env_name}")
    env_vars = {}
    with open(env_file) as f:
        lines = f.readlines()
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        i += 1
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        # 值含未闭合的 [ 或 {（如多行 JSON 的 BOTS/MODELS），继续拼接后续行直到括号闭合
        if _bracket_balance(value) > 0:
            parts = [value]
            while i < n and _bracket_balance("".join(parts)) > 0:
                parts.append(lines[i].rstrip("\n"))
                i += 1
            value = "".join(parts).strip()
        env_vars[key] = value
    return env_vars


def load_config(env_name):
    """解析 .env.<name> 为 {db, poll_interval, task_timeout, bots:[...]}。"""
    env = load_env_file(env_name)
    return {
        "db": {
            "host": env["DB_HOST"],
            "port": int(env.get("DB_PORT", 5432)),
            "dbname": env["DB_NAME"],
            "user": env["DB_USER"],
            "password": env["DB_PASSWORD"],
        },
        "poll_interval": int(env["POLL_INTERVAL"]),
        "task_timeout": int(env["TASK_TIMEOUT"]),
        "heartbeat_interval": int(env.get("HEARTBEAT_INTERVAL", 60)),
        "models": json.loads(env.get("MODELS") or '["opus","sonnet","haiku"]'),
        "bots": json.loads(env["BOTS"]),
    }


def pid_file(name):
    return os.path.expanduser(f"~/run/feishu_bot/{name}.pid")


def write_pid(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(os.getpid()))


def remove_pid(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="local", help="配置名（读取 .env.<name>）")
    args = parser.parse_args()

    cfg = load_config(args.env)
    engine = db.init_engine(cfg["db"])
    db.create_all(engine)
    db.migrate(engine)
    session_factory = db.make_session_factory(engine)

    bots = [Bot(bc, session_factory, cfg["poll_interval"], cfg["task_timeout"], cfg["models"],
                cfg["heartbeat_interval"])
            for bc in cfg["bots"]]
    print(f"[feishu-claude-bot] env={args.env}, bots={[b.name for b in bots]}")

    pidf = pid_file(args.env)
    write_pid(pidf)
    threads = [threading.Thread(target=b.run, name=b.name, daemon=True) for b in bots]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[feishu-claude-bot] stopped")
    finally:
        remove_pid(pidf)


if __name__ == "__main__":
    main()
