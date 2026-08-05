"""看门狗：查 DB → 判定 → 分诊 → 回滚 → 重启 → 验证 → 推飞书。

**为什么不能靠 prompt 约定**：Claude 在飞书 bot 群里被调用时，它自己就是 feishu-bot.service
的子进程，`systemctl restart` 会连它一起杀掉（exit 144、无输出）——承诺「失败就回滚」的执行者
在能验证之前就死了。所以回滚必须交给一个**不在该 cgroup 内**的进程，即本文件（独立 unit + timer）。

**为什么按 chat 分组判定**：事故当天 10 个 bot 里只有 1 个全挂、其余 9 个全程正常。全局统计
「窗口内有没有成功」会被健康的 9 个稀释成「有成功」，看门狗根本不会触发。判据必须是
「**某一个** chat 在窗口内失败 ≥ 阈值且零成功」。

**为什么不在这里刷新基线**：基线由 bot.py 在任务前 take() / 成功后 promote()（见 snapshot.py
的模块注释）。看门狗只读基线，不写——它看到的永远是「某轮任务动手之前」的状态。

运行方式：oneshot + timer（不常驻）。每次跑完即退，没有长驻状态可泄漏；timer 保证它自己
不会悄悄死掉，等价于 Restart=。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import envfile, notify, snapshot

SERVICE = os.environ.get("GUARD_SERVICE", "feishu-bot.service")

# 判定窗口与阈值：事故当天 8 次失败摊在 11 小时里，窗口太短会每次只看到 1 次失败凑不够阈值
WINDOW_SECONDS = int(os.environ.get("GUARD_WINDOW", 6 * 3600))
FAIL_THRESHOLD = int(os.environ.get("GUARD_FAIL_THRESHOLD", 2))

# 额度类错误的判据（回滚治不了额度，只告警）
QUOTA_MARKERS = ("usage limit", "额度已用尽", "额度不足", "rate limit")

STATE_PATH = os.path.join(snapshot.STORE, "guard-state.json")
NOTIFY_COOLDOWN = int(os.environ.get("GUARD_NOTIFY_COOLDOWN", 3600))

VERIFY_WAIT = int(os.environ.get("GUARD_VERIFY_WAIT", 45))     # 重启后等服务连上飞书的时间
CANARY_TIMEOUT = int(os.environ.get("GUARD_CANARY_TIMEOUT", 150))


# ----------------------------------------------------------------- 状态（防抖）

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(snapshot.STORE, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def should_notify(state, key):
    """同一类告警 NOTIFY_COOLDOWN 秒内只发一次，避免 timer 每轮刷屏。"""
    last = state.get("notified", {}).get(key, 0)
    if time.time() - last < NOTIFY_COOLDOWN:
        return False
    state.setdefault("notified", {})[key] = time.time()
    return True


# ----------------------------------------------------------------- 健康信号

def query_llm_records(env, window=WINDOW_SECONDS):
    """读 audit_logs（kind='llm'）窗口内记录，按 chat_id 聚合。

    时间比较用 DB 自己的 NOW()，不用本机时间——RDS 与本机时区不一定一致，用本机时间算窗口
    会静默错位（差 8 小时就等于永远查不到记录、看门狗永远不触发）。
    """
    import pymysql
    conn = pymysql.connect(
        host=env["DB_HOST"], port=int(env.get("DB_PORT", 3306)),
        user=env["DB_USER"], password=env["DB_PASSWORD"], database=env["DB_NAME"],
        charset="utf8mb4", connect_timeout=15, read_timeout=30,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chat_id, ok, detail, created_at FROM audit_logs "
                "WHERE kind='llm' AND created_at >= NOW() - INTERVAL %s SECOND "
                "ORDER BY created_at",
                (window,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    stats = {}
    for chat_id, ok, detail, created_at in rows:
        s = stats.setdefault(chat_id, {"ok": 0, "fail": 0, "last_fail": None, "last_at": None})
        if ok:
            s["ok"] += 1
        else:
            s["fail"] += 1
            s["last_fail"] = detail or ""
        s["last_at"] = created_at
    return stats


def sick_chats(stats):
    """返回判定为「疑似故障」的 chat：窗口内失败 ≥ 阈值 **且** 一次都没成功过。

    「零成功」这个条件不能松成「失败比例高」——只要还有成功，说明这条通道本身是通的，
    失败多半是单轮任务自己的问题（超时、模型拒答），回滚解决不了。
    """
    return {cid: s for cid, s in stats.items()
            if s["fail"] >= FAIL_THRESHOLD and s["ok"] == 0}


# ----------------------------------------------------------------- 分诊

def probe_anthropic():
    """返回 (http_code, 是否健康)。401/405 = 通道健康（只是没带鉴权）；
    403 = 被当成中国大陆出口；000 = 代理挂了。"""
    try:
        out = subprocess.run(
            ["curl", "-o", "/dev/null", "-s", "-w", "%{http_code}", "--max-time", "20",
             "-X", "POST", "https://api.anthropic.com/v1/messages"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception:
        out = "000"
    return out, out in ("401", "405")


def triage(sick, down_state=None):
    """返回 (要不要回滚, 原因说明)。

    down_state 非空表示服务本身没跑起来（而不是「跑着但 LLM 一直失败」）。这种情况下
    额度判据用不上（压根没有错误文案），但网络判据**仍然要跑**：`feishu-bot.service` 的
    ExecStartPre 会先探代理，代理挂了服务就起不来——那是外部原因，回滚文件治不了。
    """
    details = " ".join((s.get("last_fail") or "") for s in sick.values()).lower()
    if any(m in details for m in QUOTA_MARKERS):
        return False, "错误文案指向订阅额度用尽——回滚治不了额度"
    code, healthy = probe_anthropic()
    if not healthy:
        hint = {"403": "出口 IP 被判为中国大陆", "000": "代理不通"}.get(code, "")
        extra = "（服务起不来很可能就是 ExecStartPre 探代理失败）" if down_state else ""
        return False, (f"api.anthropic.com 探测返回 {code}（期望 401/405）"
                       f"{('：' + hint) if hint else ''}{extra}——外部原因，回滚治不了")
    if down_state:
        return True, f"服务处于 {down_state}（非运行中）、网络正常 → 判定为本机状态问题"
    return True, f"api.anthropic.com 探测 {code} 正常、非额度问题 → 判定为本机状态问题"


# ----------------------------------------------------------------- 回滚与验证

def restart_service():
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def service_state():
    r = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def service_active():
    return service_state() == "active"


# 服务状态三分。中间态必须单列出来：ExecStartPre 会最多花 90 秒探代理就绪，这期间
# is-active 返回 activating——把它当成「服务死了」会在每次正常重启的窗口里误触发回滚。
TRANSIENT_STATES = ("activating", "reloading", "deactivating")


def service_verdict():
    """返回 ('up'|'down'|'transient', 原始状态串)。"""
    st = service_state()
    if st == "active":
        return "up", st
    if st in TRANSIENT_STATES:
        return "transient", st
    return "down", st


def canary():
    """真正验证 LLM 通道：在 ROOT 里跑一次 claude。这是唯一能证明「回滚确实修好了」的证据——
    服务 active 只说明进程活着，事故当天进程也一直 active。"""
    env = dict(os.environ)
    env["HOME"] = os.path.expanduser("~")
    env["PATH"] = f"{env['HOME']}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    try:
        r = subprocess.run(
            ["claude", "-p", "reply with the single word: ok", "--output-format", "json"],
            cwd=snapshot.ROOT, env=env, capture_output=True, text=True, timeout=CANARY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"canary 超时（>{CANARY_TIMEOUT}s）"
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return False, f"canary 输出无法解析: {(r.stdout or r.stderr)[:200]}"
    if data.get("is_error"):
        return False, f"canary 返回错误: {str(data.get('result'))[:200]}"
    return True, str(data.get("result", ""))[:100]


# ----------------------------------------------------------------- 主流程

def chat_label(env, chat_id):
    for b in envfile.bots(env):
        if b.get("chat_id") == chat_id:
            return f"{b.get('name')}({chat_id[:12]}…)"
    return chat_id


def run(env, dry_run=False):
    state = load_state()

    # 先看服务本身死没死。**这条是 audit_logs 判定盖不住的洞**：服务整个起不来时不会再产生
    # 任何 LLM 记录，窗口里空空如也 → sick 为空 → 看门狗会一声不吭地放过去。
    verdict, raw_state = service_verdict()
    if verdict == "transient":
        print(f"[guard] 服务处于 {raw_state}（正在启停），本轮跳过判定")
        return 0
    down_state = raw_state if verdict == "down" else None

    try:
        stats = query_llm_records(env)
    except Exception as e:
        # 连不上 DB 本身也是故障，但回滚治不了它（凭证在 .env.local，倒是可能被改坏……
        # 保守起见只告警：误回滚的代价高于晚一点人工介入）。
        # 但服务同时也死了的话不能就此收手——那已经是硬故障，继续走后面的分诊+回滚。
        msg = f"⚠️ feishu-bot guard：查 DB 失败，本轮无法按 chat 判定\n{e}"
        print(msg)
        if not down_state:
            if should_notify(state, "db-error"):
                notify.send(env, msg)
            save_state(state)
            return 1
        stats = {}

    sick = sick_chats(stats)
    summary = " | ".join(f"{chat_label(env, c)} ok={s['ok']} fail={s['fail']}"
                         for c, s in sorted(stats.items()))
    print(f"[guard] service={raw_state} window={WINDOW_SECONDS}s chats={len(stats)} "
          f"sick={len(sick)} :: {summary or '(窗口内无 LLM 调用)'}")

    if not sick and not down_state:
        # 恢复了就清掉防抖标记，下次故障才能再回滚一次
        if state.pop("rollback", None):
            print("[guard] 通道已恢复，清除回滚防抖标记")
        state.pop("notified", None)
        save_state(state)
        return 0

    names = ("、".join(chat_label(env, c) for c in sick)
             or (f"整个服务（{raw_state}）" if down_state else "?"))
    detail = next((s.get("last_fail") for s in sick.values() if s.get("last_fail")), "") or ""
    do_rollback, reason = triage(sick, down_state=down_state)
    print(f"[guard] sick={names} rollback={do_rollback} reason={reason}")

    if not do_rollback:
        msg = (f"⚠️ feishu-bot 异常但**不回滚**\n"
               f"受影响：{names}\n判定：{reason}\n最近错误：{detail[:300]}")
        if should_notify(state, f"noroll:{down_state}:{sorted(sick)}"):
            notify.send(env, msg)
        save_state(state)
        return 1

    kg = snapshot.known_good_id()
    if not kg:
        msg = (f"🚨 feishu-bot 判定为本机状态问题，但**没有 known-good 基线**可回滚\n"
               f"受影响：{names}\n最近错误：{detail[:300]}\n需要人工介入。")
        if should_notify(state, "no-baseline"):
            notify.send(env, msg)
        save_state(state)
        return 1

    # 防抖：同一个基线只回滚一次。回滚完还失败说明问题不在这些文件里，再滚一遍只是徒劳的重启循环
    if state.get("rollback", {}).get("known_good") == kg:
        prev = state["rollback"]
        msg = (f"🚨 feishu-bot 回滚后仍然失败，已停手等人工\n"
               f"受影响：{names}\n上次回滚：{prev.get('at')} → 基线 {kg}\n"
               f"最近错误：{detail[:300]}\n"
               f"回滚前现场：{prev.get('rejected', '见 ' + snapshot.STORE)}")
        if should_notify(state, f"stuck:{kg}"):
            notify.send(env, msg)
        save_state(state)
        return 1

    changed = snapshot.diff_against_known_good()
    if dry_run:
        print(f"[guard][dry-run] 将回滚到 {kg}，与基线有差异的文件：")
        for f in changed[:50]:
            print(f"  {f}")
        return 0

    ok, info = snapshot.rollback()
    print(f"[guard] rollback ok={ok} {info}")
    if not ok:
        if should_notify(state, "rollback-failed"):
            notify.send(env, f"🚨 feishu-bot 回滚失败：{info}\n受影响：{names}")
        save_state(state)
        return 1

    state["rollback"] = {"known_good": kg, "at": datetime.now().isoformat(timespec="seconds"),
                         "rejected": info, "chats": sorted(sick), "service": raw_state}
    save_state(state)

    rok, rinfo = restart_service()
    time.sleep(VERIFY_WAIT)
    active = service_active()
    cok, cinfo = canary()

    scope = (f"（窗口内失败 {max(s['fail'] for s in sick.values())} 次、零成功）" if sick
             else "（服务未在运行，窗口内已无 LLM 记录可判）")
    lines = [
        ("✅ feishu-bot 已自动回滚并恢复" if (rok and active and cok)
         else "🚨 feishu-bot 已自动回滚，但验证未通过"),
        f"受影响：{names}{scope}",
        f"判定：{reason}",
        f"回滚到基线：{kg}",
        f"重启：{'成功' if rok else '失败 ' + rinfo}　服务：{'active' if active else 'NOT active'}",
        f"LLM 自检：{'通过' if cok else '未通过 — ' + cinfo}",
        f"与基线有差异的文件（回滚掉的改动）：{('、'.join(changed[:10]) or '无')}"
        + (f" 等 {len(changed)} 项" if len(changed) > 10 else ""),
        info,
    ]
    if not (rok and active and cok):
        lines.append("回滚未能修复，看门狗已停手（同一基线不再重复回滚），需要人工介入。")
    notify.send(env, "\n".join(lines))
    print("\n".join(lines))
    return 0 if (rok and active and cok) else 1


def main():
    ap = argparse.ArgumentParser(description="feishu-bot 看门狗：连续失败即回滚到 known-good")
    ap.add_argument("--env", default=os.environ.get("GUARD_ENV", "local"))
    ap.add_argument("--dry-run", action="store_true", help="只判定与打印，不回滚不重启")
    ap.add_argument("--status", action="store_true", help="只打印窗口内各 chat 的 LLM 成败统计")
    args = ap.parse_args()

    env = envfile.load(args.env)
    if args.status:
        stats = query_llm_records(env)
        kg = snapshot.known_good_id()
        verdict, raw_state = service_verdict()
        flag = {"up": "", "transient": "  <-- 正在启停，本轮会跳过",
                "down": "  <-- 服务未运行，会直接进入分诊"}[verdict]
        print(f"{SERVICE}: {raw_state}{flag}")
        print(f"known-good: {kg or '(无)'}   快照数: {len(snapshot.list_snapshots())}")
        for cid, s in sorted(stats.items()):
            flag = "  <-- 疑似故障" if (s["fail"] >= FAIL_THRESHOLD and s["ok"] == 0) else ""
            print(f"  {chat_label(env, cid):40s} ok={s['ok']:3d} fail={s['fail']:3d}{flag}")
        if not stats:
            print("  (窗口内无 LLM 调用记录)")
        print(f"防抖状态: {load_state().get('rollback') or '无'}")
        return 0
    return run(env, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
