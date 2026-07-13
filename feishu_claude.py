"""入口：解析 --env <name>，读取 .env.<name>（默认 local），单进程内驱动多机器人。

一个进程跑 1~N 个机器人（.env 里的 BOTS 列表），共享数据库 engine。消息接收改用
飞书长连接（WebSocket）：长连接是「每 app 一条、cluster 模式、不广播」，故按 app_id
分组，每个 app_id 开一条连接，收到事件后按 chat_id 路由到对应 Bot。多个群共用一个
app_id 时共享这一条连接。每个 Bot 有自己的 inbox+worker 线程，同一 chat 保序、
不同 chat 并行，全局 claude 并发由 run_slot 上限约束。
"""

import argparse
import json
import os
import threading
from collections import OrderedDict

import db
import feishu_api
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
    """解析 .env.<name> 为 {db, task_timeout, bots:[...]}。"""
    env = load_env_file(env_name)
    return {
        "db": {
            "host": env["DB_HOST"],
            "port": int(env.get("DB_PORT", 5432)),
            "dbname": env["DB_NAME"],
            "user": env["DB_USER"],
            "password": env["DB_PASSWORD"],
        },
        "task_timeout": int(env["TASK_TIMEOUT"]),
        "heartbeat_interval": int(env.get("HEARTBEAT_INTERVAL", 60)),
        "max_concurrent": int(env.get("MAX_CONCURRENT", 3)),  # 全局同时运行的 claude 上限
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

    # 全局并发闸：所有 bot 共享一个信号量，限制同时运行的 claude 进程数（不同 chat_id 并行但有上限）
    run_slot = threading.BoundedSemaphore(cfg["max_concurrent"])

    # 按 app_id 分组：每个 app 一个 SDK 客户端（发送/下载）+ 一条长连接；chat_id -> Bot 供路由
    api_clients = OrderedDict()     # app_id -> lark.Client
    app_secrets = {}               # app_id -> app_secret
    app_chatmaps = {}              # app_id -> {chat_id: Bot}
    bots = []
    for bc in cfg["bots"]:
        aid = bc["app_id"]
        if aid not in api_clients:
            api_clients[aid] = feishu_api.build_client(aid, bc["app_secret"])
            app_secrets[aid] = bc["app_secret"]
            app_chatmaps[aid] = {}
        b = Bot(bc, session_factory, cfg["task_timeout"], cfg["models"],
                cfg["heartbeat_interval"], run_slot=run_slot, client=api_clients[aid])
        app_chatmaps[aid][bc["chat_id"]] = b
        bots.append(b)
    print(f"[feishu-claude-bot] env={args.env}, bots={[b.name for b in bots]}, "
          f"apps={len(api_clients)}, max_concurrent={cfg['max_concurrent']}")

    pidf = pid_file(args.env)
    write_pid(pidf)
    # 先起每个 Bot 的 inbox+worker 线程，再为每个 app 开一条长连接（start() 阻塞、自动重连）
    for b in bots:
        b.start()
    ws_clients = [feishu_api.build_ws_client(aid, app_secrets[aid], app_chatmaps[aid])
                  for aid in api_clients]
    threads = [threading.Thread(target=ws.start, name=f"ws-{i}", daemon=True)
               for i, ws in enumerate(ws_clients)]
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
