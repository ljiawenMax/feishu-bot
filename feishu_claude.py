"""入口：解析 --env <name>，读取 .env.<name>，启动对应机器人。

每个机器人一份配置（.env.<name>），一个进程，独占自己的 PID 文件
（~/run/feishu_bot/<name>.pid），多机器人互不干扰。
"""

import argparse
import os

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
    parser.add_argument("--env", default="prod", help="机器人配置名（读取 .env.<name>）")
    args = parser.parse_args()

    pidf = pid_file(args.env)
    write_pid(pidf)
    try:
        env = load_env_file(args.env)
        Bot(env, env_name=args.env).run()
    except KeyboardInterrupt:
        print("\n[feishu-claude-bot] stopped")
    finally:
        remove_pid(pidf)


if __name__ == "__main__":
    main()
