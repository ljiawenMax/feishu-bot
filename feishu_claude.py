"""入口：解析 --env <name>，读取 .env.<name>（默认 local），单进程内驱动多机器人。

一个进程跑 1~N 个机器人（.env 里的 BOTS 列表），共享数据库 engine。消息接收改用
飞书长连接（WebSocket）：长连接是「每 app 一条、cluster 模式、不广播」，故按 app_id
分组，每个 app_id 开一条连接，收到事件后按 chat_id 路由到对应 Bot。多个群共用一个
app_id 时共享这一条连接。每个 Bot 有自己的 inbox+worker 线程，同一 chat 保序、
不同 chat 并行，全局 Codex 并发由 run_slot 上限约束。
"""

import argparse
import json
import re
import os
import subprocess
import threading
import time
from collections import OrderedDict

import db
import feishu_api
import tool_bridge
from bot import Bot

BASE_DIR = os.path.dirname(__file__)

WS_POLL_INTERVAL = 0.5  # 轮询长连接就绪状态的间隔（秒）


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


def _as_bool(value, default):
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def load_config(env_name):
    """解析 .env.<name> 为 {db, task_timeout, bots:[...]}。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", env_name):
        raise ValueError("环境名称仅允许字母、数字、下划线和横线")
    env = load_env_file(env_name)
    cfg = {
        "db": {
            "host": env["DB_HOST"],
            "port": int(env.get("DB_PORT", 3306)),
            "dbname": env["DB_NAME"],
            "user": env["DB_USER"],
            "password": env["DB_PASSWORD"],
        },
        "task_timeout": int(env["TASK_TIMEOUT"]),
        "heartbeat_interval": int(env.get("HEARTBEAT_INTERVAL", 60)),
        "max_concurrent": int(env.get("MAX_CONCURRENT", 3)),  # 全局同时运行的 Codex 上限
        # 续聊 context 占用达此百分比即自动压缩（摘要后开新对话）；0 关闭，改由硬闸兜底
        "auto_compact_threshold": int(env.get("AUTO_COMPACT_THRESHOLD", 75)),
        # 长连接就绪后往群里推一条「已启动」通知（断线恢复后推「已恢复」）；改完重启时用它确认服务真起来了
        "startup_notify": _as_bool(env.get("STARTUP_NOTIFY"), True),
        # 维护重启可只跳过首次通知，仍保留之后的断线恢复通知。
        "startup_silent": _as_bool(env.get("STARTUP_SILENT"), False),
        # 只通知这些机器人（按 name 或 chat_id，逗号分隔）；留空=全部群都通知。
        # 多个群共用一条长连接时，重启一次会把所有群都刷一遍，用它收敛到一个「运维群」
        "startup_notify_bots": [x.strip() for x in (env.get("STARTUP_NOTIFY_BOTS") or "").split(",")
                                if x.strip()],
        # 知识库 RAG 共享项：只有 BOTS 里配了 wiki 字段的机器人才会用到
        "wiki": {
            # 索引缓存目录：必须在 work_dir 之外——gitsync 每轮 `git add -A`，
            # 镜像落进 work_dir 会被整个提交进用户的 git 历史
            "cache_dir": env.get("WIKI_CACHE_DIR") or "~/.feishu-bot/wiki",
            "sync_interval": int(env.get("WIKI_SYNC_INTERVAL", 1800)),
            "embed_url": env.get("WIKI_EMBED_URL") or "http://localhost:11434",
            "embed_model": env.get("WIKI_EMBED_MODEL") or "quentinz/bge-small-zh-v1.5",
            "keep_alive": env.get("WIKI_EMBED_KEEP_ALIVE") or "30m",
        },
        "bots": json.loads(env["BOTS"]),
    }

    if cfg["max_concurrent"] < 1 or cfg["task_timeout"] < 1:
        raise ValueError("MAX_CONCURRENT 和 TASK_TIMEOUT 必须大于 0")
    if cfg["heartbeat_interval"] < 0 or cfg["wiki"]["sync_interval"] < 1:
        raise ValueError("HEARTBEAT_INTERVAL 必须非负，WIKI_SYNC_INTERVAL 必须大于 0")
    if not 0 <= cfg["auto_compact_threshold"] <= 100:
        raise ValueError("AUTO_COMPACT_THRESHOLD 必须在 0～100 之间")
    chats, names, secrets = set(), set(), {}
    if not isinstance(cfg["bots"], list):
        raise ValueError("BOTS 必须是列表")
    if not cfg["bots"]:
        raise ValueError("BOTS 不能为空")
    for bot in cfg["bots"]:
        if not isinstance(bot, dict):
            raise ValueError("BOTS 列表的每项必须是对象")
        if any(not bot.get(field) for field in ("name", "chat_id", "app_id", "app_secret")):
            raise ValueError("每个机器人都须配置 name、chat_id、app_id、app_secret")
        if bot["chat_id"] in chats or bot["name"] in names:
            raise ValueError("机器人 name 和 chat_id 必须唯一")
        if bot["app_id"] in secrets and secrets[bot["app_id"]] != bot["app_secret"]:
            raise ValueError("同一 app_id 的 app_secret 必须一致")
        chats.add(bot["chat_id"])
        names.add(bot["name"])
        secrets[bot["app_id"]] = bot["app_secret"]
    return cfg


# ------------------------------------------------------------------ 启动 / 恢复通知

def code_version():
    """本服务代码的版本（HEAD 短 hash + 提交标题），非 git 仓库/取不到时返回空串。
    通知里带上它，才能一眼确认「群里跑的是不是我刚改的那版」。"""
    try:
        r = subprocess.run(["git", "-C", BASE_DIR, "log", "-1", "--format=%h %s"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode:
            return ""
        dirty = subprocess.run(["git", "-C", BASE_DIR, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        return r.stdout.strip() + ("（含未提交改动）" if dirty.stdout.strip() else "")
    except Exception:
        return ""


def ws_connected(ws):
    """长连接是否已建立。SDK 没有公开的连接状态，只能读私有 `_conn`；
    读不到属性（SDK 改版）时按「已就绪」处理——宁可通知早一点，也不要永远不通知。"""
    return getattr(ws, "_conn", "unknown") is not None


def pick_notify_bots(bots, allow):
    """从一条长连接下的 bots 里挑出要通知的：allow 为空=全部，否则按 name / chat_id 精确匹配。"""
    if not allow:
        return list(bots)
    return [b for b in bots if b.name in allow or b.chat_id in allow]


def notify_chats(bots, text_of, tag):
    """往每个 bot 的群发一条通知。text_of(bot) 返回该群的文本——同一条长连接下
    可能挂着多个群，各群只该看到自己那台机器人的信息。"""
    for b in bots:
        feishu_api.send_text(b.client, b.chat_id, text_of(b), tag=tag)


def watch_connection(ws, bots, env_name, started_at, enabled=True, quiet_startup=False):
    """为一条长连接挂上通知：就绪后往其下各群推「已启动」，之后每次断线重连成功推「已恢复」。

    首连成功由后台线程轮询 `_conn` 发现（SDK 只在*重连*成功时回调 on_reconnected，
    首连不回调），不设超时——连上多晚都发，进程退出时 daemon 线程自然结束。"""
    if not enabled or not bots:
        return
    ready = threading.Event()
    version = code_version()

    def watch():
        while not ws_connected(ws):
            time.sleep(WS_POLL_INTERVAL)
        ready.set()
        head = ["✅ feishu-bot 已启动，长连接就绪",
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
                f"（启动耗时 {time.time() - started_at:.1f}s）"]
        tail = ([f"版本：{version}"] if version else []) + \
               ["提示：进程未运行期间发的消息不会补处理，如有请重发。"]

        def text_of(b):
            return "\n".join(head + [f"机器人：{b.name}（env={env_name} pid={os.getpid()}）"] + tail)

        if not quiet_startup:
            notify_chats(bots, text_of, "startup")

    threading.Thread(target=watch, name=f"notify-{bots[0].app_id[:8]}", daemon=True).start()

    def on_reconnected():
        # 该回调跑在 ws 的事件循环里、必须立刻返回；发消息是阻塞 HTTP，丢到线程做。
        # 首连尚未就绪时不发（首连若走了重连路径，交给上面的 watch 发「已启动」，避免重复）。
        if not ready.is_set():
            return
        text = ("🔄 feishu-bot 长连接已恢复\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "提示：断线期间飞书不补发事件，那段时间发的消息不会被处理，如有请重发。")
        threading.Thread(target=notify_chats, args=(bots, lambda b: text, "reconnect"),
                         daemon=True).start()

    ws.on_reconnected = on_reconnected


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

    started_at = time.time()
    cfg = load_config(args.env)
    engine = db.init_engine(cfg["db"])
    db.create_all(engine)
    db.migrate(engine)
    session_factory = db.make_session_factory(engine)

    # 全局并发闸：所有 bot 共享一个信号量，限制同时运行的 Codex 进程数（不同 chat_id 并行但有上限）
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
        b = Bot(bc, session_factory, cfg["task_timeout"],
                cfg["heartbeat_interval"], run_slot=run_slot, client=api_clients[aid],
                auto_compact_threshold=cfg["auto_compact_threshold"],
                wiki_cfg=cfg["wiki"])
        app_chatmaps[aid][bc["chat_id"]] = b
        bots.append(b)
    print(f"[feishu-bot] env={args.env}, bots={[b.name for b in bots]}, "
          f"apps={len(api_clients)}, max_concurrent={cfg['max_concurrent']}")

    from ops import snapshot
    snapshot.capture_runtime()
    tools = tool_bridge.start(os.path.expanduser(f"~/run/feishu_bot/tools-{args.env}.sock"))
    print(f"[fbtool] listening {tools.server_address}")
    pidf = pid_file(args.env)
    write_pid(pidf)
    # 先起每个 Bot 的 inbox+worker 线程，再为每个 app 开一条长连接（start() 阻塞、自动重连）
    for b in bots:
        b.start()
    ws_clients = [feishu_api.build_ws_client(aid, app_secrets[aid], app_chatmaps[aid])
                  for aid in api_clients]
    # 每条连接就绪/恢复后往其下各群推通知（改完重启时不用盯日志也知道服务起没起来）
    for aid, ws in zip(api_clients, ws_clients):
        watch_connection(ws, pick_notify_bots(app_chatmaps[aid].values(),
                                              cfg["startup_notify_bots"]),
                         args.env, started_at, enabled=cfg["startup_notify"],
                         quiet_startup=cfg["startup_silent"])
    threads = [threading.Thread(target=ws.start, name=f"ws-{i}", daemon=True)
               for i, ws in enumerate(ws_clients)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[feishu-bot] stopped")
    finally:
        tools.shutdown()
        tools.server_close()
        remove_pid(pidf)


if __name__ == "__main__":
    main()
