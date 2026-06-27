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
    session_factory = db.make_session_factory(engine)

    bots = [Bot(bc, session_factory, cfg["poll_interval"], cfg["task_timeout"]) for bc in cfg["bots"]]
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
