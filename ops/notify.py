"""独立的飞书告警通道：自带凭证、只用标准库，**不依赖 bot 进程活着**。

服务挂了照样要能通知——这正是最需要通知的时刻，所以这里不碰 lark-oapi、不 import
项目模块，直接 urllib 打两个 HTTP：换 tenant_access_token → 发消息。

告警发到哪个群：`.env.local` 的 `GUARD_NOTIFY_BOT`（值 = BOTS 里某项的 name），
缺省取 BOTS 第一项。
"""

import json
import os
import sys
import urllib.request

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"

MAX_CHARS = 1800   # 飞书单条文本上限约 2000，留余量；超出截断（告警不追求完整正文）


def _post(url, payload, token=None):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def target(env):
    """返回 (app_id, app_secret, chat_id, name)；配不出来时返回 None。"""
    from ops import envfile  # 延迟导入，便于本文件被单独 import 时不强制包上下文
    bots = envfile.bots(env)
    if not bots:
        return None
    want = (env.get("GUARD_NOTIFY_BOT") or "").strip()
    chosen = next((b for b in bots if b.get("name") == want), None) if want else None
    chosen = chosen or bots[0]
    if not (chosen.get("app_id") and chosen.get("app_secret") and chosen.get("chat_id")):
        return None
    return chosen["app_id"], chosen["app_secret"], chosen["chat_id"], chosen.get("name")


def send(env, text):
    """发一条文本告警。返回 (ok, 说明)。任何异常都收敛成返回值——告警失败不该再炸一次。"""
    try:
        t = target(env)
        if not t:
            return False, "未找到可用的告警目标（检查 BOTS / GUARD_NOTIFY_BOT）"
        app_id, app_secret, chat_id, _ = t
        r = _post(TOKEN_URL, {"app_id": app_id, "app_secret": app_secret})
        token = r.get("tenant_access_token")
        if not token:
            return False, f"取 token 失败: {r}"
        body = {"receive_id": chat_id, "msg_type": "text",
                "content": json.dumps({"text": text[:MAX_CHARS]})}
        r = _post(SEND_URL, body, token=token)
        if r.get("code") == 0:
            return True, r.get("data", {}).get("message_id")
        return False, f"发送失败: {r}"
    except Exception as e:
        return False, f"发送异常: {e}"


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ops import envfile
    msg = " ".join(sys.argv[1:]) or "🔔 feishu-bot guard 通道自检"
    ok, info = send(envfile.load(os.environ.get("GUARD_ENV", "local")), msg)
    print(("ok " if ok else "fail ") + str(info))
    sys.exit(0 if ok else 1)
