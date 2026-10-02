"""看门狗：查 DB → 判定 → 分诊 → 回滚 → 重启 → 验证 → 推飞书。

**为什么不能靠 prompt 约定**：Claude 在飞书 bot 群里被调用时，它自己就是 feishu-bot.service
的子进程，`systemctl restart` 会连它一起杀掉（exit 144、无输出）——承诺「失败就回滚」的执行者
在能验证之前就死了。所以回滚必须交给一个**不在该 cgroup 内**的进程，即本文件（独立 unit + timer）。

**为什么按 chat 分组判定**：事故当天 10 个 bot 里只有 1 个全挂、其余 9 个全程正常。全局统计
「窗口内有没有成功」会被健康的 9 个稀释成「有成功」，看门狗根本不会触发。判据必须是
「**某一个** chat 在窗口内失败 ≥ 阈值且零成功」。

**两类故障要两个独立信号**（2026-08-15 补）：
  类 B（2026-08-04）服务 active、只有 LLM 通道死 → 用真实流量（audit_logs）当信号；
  类 A（2026-08-15）服务根本起不来 → DB 判据对它**天生失明**：一次 LLM 调用都不会发生，
  一条记录都不会写，每个 chat 都是 fail=0，看上去「全都很健康」。
类 A 只能靠 systemd 自己的状态来看（service_verdict），而且**不能只看 `is-active`**——
崩溃循环和正常启动都叫 activating，那次事故里看门狗每轮都判成中间态跳过，全程零动作。

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

# 崩溃循环判据（见 service_verdict）：跨轮 NRestarts 增长多少算「在循环」，
# 以及「已经稳住了」至少要 active 多久
CRASH_RESTART_DELTA = int(os.environ.get("GUARD_CRASH_DELTA", 2))
STABLE_SECONDS = int(os.environ.get("GUARD_STABLE_SECONDS", 120))
VERIFY_STABLE = int(os.environ.get("GUARD_VERIFY_STABLE", 30))  # 回滚后要求连续 active 的时长


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

def probe_openai():
    """返回 (http_code, 是否健康)。401/405 = Codex 订阅服务可达（未携带凭证）；
    这里只判断网络，不判断订阅或模型授权。000 = 无法连接。"""
    try:
        out = subprocess.run(
            ["curl", "-o", "/dev/null", "-s", "-w", "%{http_code}", "--max-time", "20",
             "https://chatgpt.com/backend-api/codex/models"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception:
        out = "000"
    return out, out in ("401", "405")


def triage(sick, down_state=None):
    """返回 (要不要回滚, 原因说明)。

    down_state 非空表示服务本身没跑起来（崩溃循环或未运行，而不是「跑着但 LLM 一直失败」）。这种情况下
    额度判据用不上（压根没有错误文案），但网络判据**仍然要跑**：`feishu-bot.service` 的
    ExecStartPre 会先探代理，代理挂了服务就起不来——那是外部原因，回滚文件治不了。
    """
    details = " ".join((s.get("last_fail") or "") for s in sick.values()).lower()
    if any(m in details for m in QUOTA_MARKERS):
        return False, "错误文案指向订阅额度用尽——回滚治不了额度"
    code, healthy = probe_openai()
    if not healthy:
        hint = {"403": "订阅服务访问受限", "000": "代理不通"}.get(code, "")
        extra = "（服务起不来很可能就是 ExecStartPre 探代理失败）" if down_state else ""
        return False, (f"chatgpt.com 探测返回 {code}（期望 401/405）"
                       f"{('：' + hint) if hint else ''}{extra}——外部原因，回滚治不了")
    if down_state:
        return True, f"服务{down_state}、网络正常 → 判定为本机状态问题"
    return True, f"chatgpt.com 探测 {code} 正常、非额度问题 → 判定为本机状态问题"


# ----------------------------------------------------------------- 回滚与验证

def restart_service():
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


# 判别服务死活要用的全部字段。**不能只用 `systemctl is-active`**：它只给一个词，
# 而「正在启动」和「崩了正在重试」都叫 activating——2026-08-15 的盲区就出在这里，
# 崩溃循环整整 4 分钟里看门狗每轮都判成中间态跳过、全程零动作，最后是人工修好的。
SHOW_PROPS = ("ActiveState", "SubState", "Result", "NRestarts", "ActiveEnterTimestampMonotonic")


def service_info():
    """一次 `systemctl show` 拿全判别字段，返回 dict（拿不到时返回空 dict）。"""
    cmd = ["systemctl", "show", SERVICE]
    for p in SHOW_PROPS:
        cmd += ["-p", p]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        out = ""
    info = {}
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        info[k.strip()] = v.strip()
    return info


def _int(info, key):
    try:
        return int(info.get(key) or 0)
    except ValueError:
        return 0


def active_seconds(info):
    """服务已经连续 active 多久（秒）。systemd 的 *TimestampMonotonic 与 CLOCK_MONOTONIC 同源，
    可以直接减——不用去解析带时区和 locale 的人类可读时间串。"""
    us = _int(info, "ActiveEnterTimestampMonotonic")
    if not us:
        return 0.0
    return max(0.0, time.clock_gettime(time.CLOCK_MONOTONIC) - us / 1e6)


def describe(info):
    return (f"{info.get('ActiveState', '?')}/{info.get('SubState', '?')}"
            f"(Result={info.get('Result', '?')} NRestarts={info.get('NRestarts', '?')})")


def service_verdict(info, prev_restarts=None):
    """服务状态四分：('up'|'starting'|'crashloop'|'down', 判据说明)。

    三分（up/down/transient）不够用：`Restart=on-failure` 的崩溃循环**恒等于 activating**，
    和「ExecStartPre 正在探代理」是同一个词，只看 is-active 分不出来 → 崩多久瞎多久。
    实测两者的区别在 SubState/Result：

        正在启动    SubState=start-pre|start|start-post   Result=success
        崩溃循环    SubState=auto-restart                 Result=exit-code

    `starting` 仍然跳过：feishu-bot 的 ExecStartPre 探代理最多花 90 秒，把这段当成
    「服务死了」会在每次正常重启的窗口里误触发回滚。Result 在每次重新 start 时会被清回
    success（实测），所以它不会把「崩过一次、现在正常启动」误判成崩溃循环。
    """
    st = info.get("ActiveState") or "unknown"
    sub = info.get("SubState") or "?"
    result = info.get("Result") or ""
    n = _int(info, "NRestarts")

    # ① 最硬的证据：auto-restart = 崩了，正在等 RestartSec 重来
    if sub == "auto-restart":
        return "crashloop", f"SubState=auto-restart（Result={result}，已自动重启 {n} 次）"

    # ② 采样落在崩溃循环的「运行窗口」里：瞬时字段全是健康的（active/running、Result=success），
    #    只有跨轮 NRestarts 增长看得出来。实测 12s 周期的崩溃循环里有 2s 长这样，10 分钟一轮的
    #    timer 完全可能正好踩中。已经稳住 STABLE_SECONDS 的不算——那是「重启过但现在好了」。
    if prev_restarts is not None and n - prev_restarts >= CRASH_RESTART_DELTA:
        age = active_seconds(info)
        if not (st == "active" and age >= STABLE_SECONDS):
            return "crashloop", (f"上轮以来 NRestarts {prev_restarts} → {n}"
                                 f"（当前 {st}/{sub}，已 active {age:.0f}s）")

    if st == "active":
        return "up", f"active/{sub}"
    if sub in ("start-pre", "start", "start-post") or st in ("activating", "reloading", "deactivating"):
        return "starting", f"{st}/{sub}"
    if st == "failed" or (result and result != "success"):
        return "crashloop", f"{st}/{sub}（Result={result}，systemd 已放弃重启）"
    return "down", f"{st}/{sub}"


VERDICT_LABEL = {"up": "运行中", "starting": "正在启动", "crashloop": "陷入崩溃循环", "down": "未在运行"}


def verify_service_up(window=VERIFY_STABLE, interval=5):
    """回滚后验证服务**真的起来了**：连续 window 秒 active 且 NRestarts 不再增长。

    这条是 canary() 顶不了的：canary 在 ROOT 里跑 Codex，与服务起没起来无关，
    服务崩着它照样通过。反过来 NRestarts 只看增量不看绝对值——
    手动 restart 不会把它清零（本机实测停在 31）。
    """
    info = service_info()
    n0 = _int(info, "NRestarts")
    deadline = time.time() + window
    while True:
        st, sub = info.get("ActiveState"), info.get("SubState")
        if st != "active":
            return False, f"服务处于 {st}/{sub}（期望 active/running）"
        if _int(info, "NRestarts") != n0:
            return False, f"服务仍在重启循环（NRestarts {n0} → {info.get('NRestarts')}）"
        left = deadline - time.time()
        if left <= 0:
            return True, f"连续 {window}s active/{sub}，NRestarts 稳定在 {n0}"
        time.sleep(min(interval, left))
        info = service_info()


def config_error(env_name):
    """廉价预检：`.env.<name>` 的 BOTS 还能不能解析。返回原因串（None = 正常）。

    崩溃循环时用来分辨「配置写坏了」和「崩因在别处」：解析失败几乎可以断定是配置问题、
    回滚命中率极高；解析正常则崩因可能是依赖缺失/代码语法错/DB 连不上，回滚未必治得了。
    2026-08-15 那次就是 BOTS 里少一个双引号，服务从来没起来过。
    """
    try:
        raw = envfile.load(env_name)
    except Exception as e:
        return f"读不了 .env.{env_name}：{type(e).__name__}: {e}"
    if not (raw.get("BOTS") or "").strip():
        return "BOTS 缺失或为空"
    try:
        parsed = json.loads(raw["BOTS"])
    except Exception as e:
        return f"BOTS JSON 解析失败：{e}"
    if not isinstance(parsed, list) or not parsed:
        return "BOTS 不是非空列表"
    return None


def canary():
    """真正验证 LLM 通道：在 ROOT 里跑一次 Codex。这是唯一能证明「回滚确实修好了」的证据——
    服务 active 只说明进程活着，事故当天进程也一直 active。"""
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    env["PATH"] = f"{os.path.expanduser('~')}/.local/bin:" + env.get("PATH", "/usr/bin:/bin")
    try:
        login = subprocess.run(["codex", "login", "status"], env=env,
                               capture_output=True, text=True, timeout=10)
        if login.returncode or "chatgpt" not in (login.stdout + login.stderr).lower():
            return False, "canary 未使用 ChatGPT 订阅登录，请执行 codex login"
        r = subprocess.run(
            ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
             "-c", 'model_provider="openai"', "-c", 'approval_policy="never"',
             "-c", 'sandbox_mode="read-only"',
             "reply with the single word: ok; do not use tools"],
            cwd=snapshot.ROOT, env=env, capture_output=True, text=True, timeout=CANARY_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"canary 执行失败: {e}"
    completed, answer, error = False, "", ""
    for line in r.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            completed = True
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = item.get("text") or ""
        elif event.get("type") in ("turn.failed", "error"):
            error = str(event.get("error") or event.get("message") or "Codex error")
    if r.returncode or not completed or answer.strip().lower() != "ok":
        return False, f"canary 返回错误: {(error or answer or r.stderr)[:200]}"
    return True, answer[:100]


# ----------------------------------------------------------------- 主流程

def chat_label(env, chat_id):
    for b in envfile.bots(env):
        if b.get("chat_id") == chat_id:
            return f"{b.get('name')}({chat_id[:12]}…)"
    return chat_id


def fmt_snapshot_id(sid):
    try:
        return datetime.strptime(sid[:15], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return sid


def newest_change_time(paths):
    """被回滚掉的那批文件里最后一次改动的时间。**必须在 rollback() 之前算**，之后就被覆盖了。
    告警要能说清「你几点做的那次改动被撤销了」，否则用户会二次困惑（我明明加了 ipcp，怎么没了）。"""
    ts = 0.0
    for p in paths:
        try:
            ts = max(ts, os.stat(os.path.join(snapshot.ROOT, p)).st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def run(env, dry_run=False, env_name="local"):
    state = load_state()

    # 先看服务本身死没死。**这条是 audit_logs 判定盖不住的洞**：服务整个起不来时不会再产生
    # 任何 LLM 记录，窗口里空空如也 → sick 为空 → 看门狗会一声不吭地放过去。
    info = service_info()
    svc_state = state.setdefault("service", {})
    # 上轮的 NRestarts 读数按 unit 名分开记：拿 GUARD_SERVICE 指着测试 unit 演练时不污染生产读数
    prev_restarts = (svc_state.get(SERVICE) or {}).get("nrestarts")
    verdict, why = service_verdict(info, prev_restarts)
    raw_state = describe(info)
    # 立刻落盘：跨轮 NRestarts 比对要求每一轮都留下读数，包括下面直接 return 的那几条路。
    svc_state[SERVICE] = {"nrestarts": _int(info, "NRestarts"), "verdict": verdict,
                          "state": info.get("ActiveState"), "sub": info.get("SubState"),
                          "at": datetime.now().isoformat(timespec="seconds")}
    save_state(state)

    if verdict == "starting":
        print(f"[guard] 服务 {raw_state} 正在启动，本轮跳过判定（{why}）")
        return 0
    crashloop = verdict == "crashloop"
    down_state = None if verdict == "up" else f"{VERDICT_LABEL[verdict]}（{why}）"

    if crashloop:
        # 崩溃循环下 DB 判据天生失明：进程起不来 → 一次 LLM 调用都不会发生 → 一条记录都不会写
        # → 每个 chat 都是 fail=0 → sick=0 → 「所有 chat 都很健康」。所以这里根本不查 DB，
        # 直接拿「服务本身死了」当信号进分诊（2026-08-15 事故的根本盲区，见模块注释）。
        stats = {}
        print(f"[guard] service={raw_state} → 崩溃循环：{why}；跳过 DB 判定")
    else:
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
    if not crashloop:
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

    # 配置预检只在服务没起来时有意义（服务活着说明 BOTS 早就解析过了），且只用来调整置信度，
    # 不改变分诊结论——外部原因（额度/代理）该不回滚还是不回滚。
    if down_state:
        cfg_err = config_error(env_name)
        precheck = (f"配置预检：{cfg_err} → 几乎可以断定是配置问题，回滚命中率高" if cfg_err
                    else "配置预检：BOTS 能正常解析 → 崩因可能在别处（依赖/代码/DB），回滚把握较小")
        reason = f"{reason}；{precheck}"
    print(f"[guard] sick={names} rollback={do_rollback} reason={reason}")

    if not do_rollback:
        msg = (f"⚠️ feishu-bot 异常但**不回滚**\n"
               f"受影响：{names}\n判定：{reason}\n最近错误：{detail[:300]}")
        if should_notify(state, f"noroll:{down_state}:{sorted(sick)}"):
            notify.send(env, msg)
        print(msg)
        save_state(state)
        return 1

    kg = snapshot.known_good_id()
    if not kg:
        msg = (f"🚨 feishu-bot 判定为本机状态问题，但**没有 known-good 基线**可回滚\n"
               f"受影响：{names}\n最近错误：{detail[:300]}\n需要人工介入。")
        if should_notify(state, "no-baseline"):
            notify.send(env, msg)
        print(msg)
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
        print(msg)
        save_state(state)
        return 1

    changed = snapshot.diff_against_known_good()
    changed_at = newest_change_time(changed)   # 必须在回滚前算，之后文件就被覆盖了
    file_list = ("、".join(changed[:10]) or "无") + (f" 等 {len(changed)} 项" if len(changed) > 10 else "")

    # 与基线一字不差 → 回滚是空操作，问题不在被保护的这些文件里（.venv/.git 不在快照范围内）。
    # 白回滚一次只会白白吃掉「同一基线只回滚一次」的名额，还会掩盖真正的原因。
    if not changed:
        msg = (f"🚨 feishu-bot 异常，但当前状态与 known-good 基线**无差异**，回滚是空操作\n"
               f"受影响：{names}\n判定：{reason}\n基线：{kg}（{fmt_snapshot_id(kg)}）\n"
               f"崩因不在被保护的文件里（.venv / .git / 外部依赖不在快照范围），需要人工介入。")
        if should_notify(state, f"nodiff:{kg}"):
            notify.send(env, msg)
        print(msg)
        save_state(state)
        return 1

    if dry_run:
        print(f"[guard][dry-run] 将回滚到 {kg}（{fmt_snapshot_id(kg)}），"
              f"会撤销 {changed_at or '未知时间'} 及之前的改动，与基线有差异的文件：")
        for f in changed[:50]:
            print(f"  {f}")
        return 0

    ok, rbinfo = snapshot.rollback()
    print(f"[guard] rollback ok={ok} {rbinfo}")
    if not ok:
        if should_notify(state, "rollback-failed"):
            notify.send(env, f"🚨 feishu-bot 回滚失败：{rbinfo}\n受影响：{names}")
        save_state(state)
        return 1

    state["rollback"] = {"known_good": kg, "at": datetime.now().isoformat(timespec="seconds"),
                         "rejected": rbinfo, "chats": sorted(sick), "service": raw_state}
    save_state(state)

    rok, rinfo = restart_service()
    time.sleep(VERIFY_WAIT)
    # 服务死了那一类（crashloop/down）的验证判据是「服务真的稳住了」，canary 只作附加确认：
    # canary 在 ROOT 里跑 Codex，与服务起没起来无关，服务崩着它照样通过。
    sok, sinfo = verify_service_up()
    cok, cinfo = canary()
    recovered = rok and sok and (cok or bool(down_state))

    scope = (f"（窗口内失败 {max(s['fail'] for s in sick.values())} 次、零成功）" if sick
             else "（服务未在运行，窗口内已无 LLM 记录可判）")
    lines = [
        ("✅ feishu-bot 已自动回滚并恢复" if (rok and sok and cok)
         else "⚠️ feishu-bot 已自动回滚，服务已恢复运行，但 LLM 自检未通过" if recovered
         else "🚨 feishu-bot 已自动回滚，但验证未通过"),
        f"受影响：{names}{scope}",
        f"判定：{reason}",
        f"回滚到基线：{kg}（{fmt_snapshot_id(kg)}）",
        # 回滚会把用户刚做的**合法**改动一起撤销（2026-08-15 那次就是新加的 ipcp 配置），
        # 不说清楚用户会二次困惑：「我明明加了 ipcp，怎么没了」。
        f"⚠️ 你在 {changed_at or '基线之后'} 的改动已被撤销，修好后需要重新应用：{file_list}",
        f"重启：{'成功' if rok else '失败 ' + rinfo}　服务：{sinfo}",
        f"LLM 自检：{'通过' if cok else '未通过 — ' + cinfo}"
        + ("（canary 只证明 LLM 通道，证明不了服务起没起来）" if down_state else ""),
        rbinfo,
    ]
    if not recovered:
        lines.append("回滚未能修复，看门狗已停手（同一基线不再重复回滚），需要人工介入。")
    notify.send(env, "\n".join(lines))
    print("\n".join(lines))
    return 0 if recovered else 1


def main():
    ap = argparse.ArgumentParser(description="feishu-bot 看门狗：连续失败即回滚到 known-good")
    ap.add_argument("--env", default=os.environ.get("GUARD_ENV", "local"))
    ap.add_argument("--dry-run", action="store_true", help="只判定与打印，不回滚不重启")
    ap.add_argument("--status", action="store_true", help="只打印窗口内各 chat 的 LLM 成败统计")
    args = ap.parse_args()

    env = envfile.load(args.env)
    if args.status:
        kg = snapshot.known_good_id()
        state = load_state()
        info = service_info()
        prev_restarts = ((state.get("service") or {}).get(SERVICE) or {}).get("nrestarts")
        verdict, why = service_verdict(info, prev_restarts)
        flag = {"up": "",
                "starting": "  <-- 正在启动（ExecStartPre 探代理），本轮会跳过",
                "crashloop": "  <-- 崩溃循环，会跳过 DB 判定直接进分诊",
                "down": "  <-- 服务未运行，会直接进入分诊"}[verdict]
        print(f"{SERVICE}: {describe(info)} → {verdict}{flag}")
        print(f"  判据: {why}   上轮 NRestarts: {prev_restarts if prev_restarts is not None else '(无记录)'}")
        if verdict != "up":
            print(f"  配置预检: {config_error(args.env) or 'BOTS 解析正常'}")
        stats = query_llm_records(env)
        print(f"known-good: {kg or '(无)'}   快照数: {len(snapshot.list_snapshots())}")
        for cid, s in sorted(stats.items()):
            flag = "  <-- 疑似故障" if (s["fail"] >= FAIL_THRESHOLD and s["ok"] == 0) else ""
            print(f"  {chat_label(env, cid):40s} ok={s['ok']:3d} fail={s['fail']:3d}{flag}")
        if not stats:
            print("  (窗口内无 LLM 调用记录)")
        print(f"防抖状态: {state.get('rollback') or '无'}")
        return 0
    return run(env, dry_run=args.dry_run, env_name=args.env)


if __name__ == "__main__":
    sys.exit(main())
