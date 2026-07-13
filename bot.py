"""Bot：单个飞书机器人（一个 chat_id）的消息处理与会话编排。

每个 Bot 实例对应一个 chat_id，状态持久化在 MySQL。消息由长连接事件推来，经
on_event（跑在 ws 事件循环里、必须快速返回）按 message_id 去重后丢进 inbox 队列；
inbox 线程串行处理（命令回复/上传下载/把长任务入队），claude 长任务再交给 worker 线程。
同一 chat_id 天然保序；不同 chat_id 各有自己的线程可并行，全局 claude 并发由 run_slot 约束。
"""

import collections
import os
import queue
import subprocess
import threading
import time

import claude_runner
import db
import feishu_api
import uploads
import usage

# Claude 标准上下文窗口大小（token）；用于 /context 计算占用百分比
CONTEXT_WINDOW = 200000

HELP_TEXT = "\n".join([
    "可用命令：",
    "/sessions  — 列出对话，回复序号切换",
    "/new       — 新建对话（清空历史）",
    "/name <名称> — 重命名当前对话",
    "/del <序号> — 删除指定对话（序号见 /sessions）",
    "/permit    — 开关工作区文件读写（acceptEdits，不跑命令）",
    "/unsafe    — 开关最高权限（跳过全部权限校验，可执行 bash 命令，谨慎）",
    "/model     — 选择当前对话使用的模型",
    "/context   — 查看当前对话的 context 窗口占用",
    "/usage     — 查看 Claude 订阅用量（5 小时窗 / 7 天）",
    "/retry     — 用当前权限重跑上一条任务",
    "/help      — 显示此帮助",
])


class Bot:
    """飞书消息驱动 Claude Code 的轮询服务，状态持久化在 MySQL。"""

    def __init__(self, bot_cfg, session_factory, task_timeout, models=None,
                 heartbeat_interval=60, run_slot=None, client=None):
        self.name = bot_cfg["name"]
        self.app_id = bot_cfg["app_id"]
        self.app_secret = bot_cfg["app_secret"]
        self.chat_id = bot_cfg["chat_id"]
        # 一 chat 一 work_dir；省略时回退家目录，配置值支持 ~
        self.work_dir = os.path.expanduser(bot_cfg.get("work_dir") or "~")
        # 发送/下载用的 SDK 客户端（同 app_id 的多个 Bot 共用一个；token 由其内部管理）
        self.client = client
        self.task_timeout = task_timeout
        self.models = models or ["opus", "sonnet", "haiku"]  # /model 可选清单
        self.heartbeat_interval = heartbeat_interval  # 长任务心跳间隔秒数，0=禁用
        # 全局并发闸（所有 bot 共享一个）：限制同时运行的 claude 进程数；未传时不限流
        self.run_slot = run_slot or threading.BoundedSemaphore(1000)

        self.Session = session_factory

        # 恢复持久化状态（单一会话列表 + 单一权限开关）
        state = self.db(db.load_state, self.chat_id) or {}
        # {"list": [{id, label, model, _row_id}, ...], "current": idx, "history": [...]}
        self.sessions = state.get("sessions") or {"list": [], "current": 0, "history": []}
        self.permit_mode = state.get("permit", False)  # acceptEdits：工作区内读写文件
        self.unsafe_mode = state.get("unsafe", False)  # 跳过全部权限校验

        self.pending = None   # "session" | "model"，当前等待用户回复序号的类型
        self.last_task = None  # {"text", "msg_id", "session_id"}，用于 /retry 重跑

        # inbox：长连接事件先入此队列（on_event 快速返回），inbox 线程串行处理命令/上传/入队
        self.inbox = queue.Queue()
        # 幂等去重：长连接事件可能重复投递，记最近处理过的 message_id（有界 LRU）
        self._seen_ids = collections.OrderedDict()
        self._seen_max = 500
        # 异步执行：claude 长任务丢后台 worker 串行跑，inbox 线程不阻塞；lock 护会话状态短临界区
        self.task_queue = queue.Queue()
        self.lock = threading.RLock()

        # 命令分发表：命令 -> 处理方法(msg, arg)
        self.commands = {
            "/sessions": self.cmd_sessions,
            "/help": self.cmd_help,
            "/name": self.cmd_name,
            "/new": self.cmd_new,
            "/permit": self.cmd_permit,
            "/unsafe": self.cmd_unsafe,
            "/model": self.cmd_model,
            "/context": self.cmd_context,
            "/usage": self.cmd_usage,
            "/del": self.cmd_del,
            "/retry": self.cmd_retry,
        }

    # ------------------------------------------------------------------ DB

    def db(self, fn, *a, **kw):
        """开一个短生命周期 Session 执行 DB 操作（每次独立、线程安全），失败不致命。"""
        with self.Session() as s:
            try:
                r = fn(s, *a, **kw)
                s.commit()
                return r
            except Exception as e:
                s.rollback()
                print(f"[{self.name}][db-error] {e}")
                return None

    def save_bot_state(self):
        self.db(db.save_bot_state, self.chat_id, self.permit_mode, self.unsafe_mode)

    # -------------------------------------------------------------- session

    def current_session_id(self):
        data = self.sessions
        if not data["list"]:
            return None
        return data["list"][data["current"]]["id"]

    def new_entry(self, label=None):
        ts = time.strftime("%m-%d %H:%M")
        return {"id": None, "label": label or f"新对话 {ts}", "model": None, "_row_id": None}

    def current_entry(self):
        data = self.sessions
        if not data["list"]:
            return None
        return data["list"][data["current"]]

    def make_session(self, label=None):
        """创建内存 session 条目并在 DB 中建行，返回条目（含 _row_id）。"""
        entry = self.new_entry(label)
        position = len(self.sessions["list"])
        entry["_row_id"] = self.db(db.insert_session, self.chat_id, entry["label"], position)
        return entry

    def set_current(self, entry):
        if entry.get("_row_id"):
            self.db(db.set_current, self.chat_id, entry["_row_id"])

    def load_history(self, claude_sid):
        return self.db(db.load_history, self.chat_id, claude_sid) or []

    def update_session(self, session_id, first_task=None):
        """首次执行时写入 session_id 和标签，并同步到 DB。
        在 worker 线程于长调用之后运行，加锁与主循环的会话增删改互斥。"""
        with self.lock:
            data = self.sessions
            if not data["list"]:
                return
            entry = data["list"][data["current"]]
            entry["id"] = session_id
            new_label = None
            if first_task and entry["label"].startswith("新对话"):
                ts = time.strftime("%m-%d %H:%M")
                entry["label"] = f"{ts} {first_task[:20]}{'…' if len(first_task) > 20 else ''}"
                new_label = entry["label"]
            if entry.get("_row_id"):
                self.db(db.update_session, entry["_row_id"], claude_sid=session_id, label=new_label)

    def audit(self, user_text, result, new_session_id, used_model=None):
        """把一轮对话（用户输入 + LLM 输出）写入审计表。"""
        entry = self.current_entry()
        row_id = entry.get("_row_id") if entry else None
        sid = (entry.get("id") if entry else None) or new_session_id
        self.db(db.append_message, row_id, self.chat_id, sid, "user", user_text)
        self.db(db.append_message, row_id, self.chat_id, sid, "assistant", result,
                result.startswith("[error]"), "超时" in result, model=used_model)

    def execute_claude(self, text, session_id, history=None, first_task=None):
        """调用 Claude Code，处理超时/异常，成功时同步 session_id 到 DB。
        返回 (result, new_sid, used_model)。"""
        permit = self.permit_mode
        unsafe = self.unsafe_mode
        # 始终把该聊天上传目录纳入工作区（存在才加），让 Claude 能读上传文件
        up = uploads.chat_dir(self.chat_id)
        extra_dirs = [up] if os.path.isdir(up) else []
        entry = self.current_entry()
        model = entry.get("model") if entry else None
        try:
            result, new_sid, used_model = claude_runner.run_claude(
                text, self.work_dir, self.task_timeout,
                session_id, permit=permit, extra_dirs=extra_dirs, history=history,
                model=model, unsafe=unsafe,
            )
            if new_sid:
                self.update_session(new_sid, first_task=first_task)
        except subprocess.TimeoutExpired:
            result, new_sid, used_model = f"[error] 任务超时（>{self.task_timeout}s）", None, None
        except Exception as e:
            result, new_sid, used_model = f"[error] 执行失败: {e}", None, None
        return result, new_sid, used_model

    # ------------------------------------------------------------- commands

    def cmd_sessions(self, msg, arg):
        data = self.sessions
        if len(data["list"]) <= 1:
            feishu_api.reply_message(self.client, msg["id"], "当前只有 1 个对话，发送 /new 创建新对话")
        else:
            self.pending = "session"
            feishu_api.reply_message(self.client, msg["id"], feishu_api.build_sessions_prompt(data["list"], data["current"]))

    def cmd_help(self, msg, arg):
        feishu_api.reply_message(self.client, msg["id"], HELP_TEXT)

    def cmd_name(self, msg, arg):
        if not arg:
            feishu_api.reply_message(self.client, msg["id"], "用法：/name <名称>")
            return
        data = self.sessions
        if data["list"]:
            entry = data["list"][data["current"]]
            entry["label"] = arg
            if entry.get("_row_id"):
                self.db(db.update_session, entry["_row_id"], label=arg)
            feishu_api.reply_message(self.client, msg["id"], f"当前对话已命名为「{arg}」")
        else:
            feishu_api.reply_message(self.client, msg["id"], "当前没有活跃的对话")

    def cmd_new(self, msg, arg):
        with self.lock:  # 与 worker 的 update_session 互斥，防会话列表被并发改乱
            data = self.sessions
            entry = self.make_session()
            data["list"].append(entry)
            data["current"] = len(data["list"]) - 1
            data["history"] = []  # 清空历史，真正从零开始
            self.set_current(entry)
            total = len(data["list"])
        self.pending = None
        feishu_api.reply_message(self.client, msg["id"], f"已创建新对话（共 {total} 个），发送任务即可开始")
        print(f"[{self.name}][new-session] total={total}")

    def cmd_permit(self, msg, arg):
        self.permit_mode = not self.permit_mode
        on = self.permit_mode
        # 与 unsafe 互斥：开 permit 则关掉 unsafe
        cleared = on and self.unsafe_mode
        if on:
            self.unsafe_mode = False
        self.save_bot_state()
        status = "已开启（可在工作区读写/修改文件，不执行命令）" if on else "已关闭"
        note = "（已自动关闭最高权限 unsafe）" if cleared else ""
        feishu_api.reply_message(self.client, msg["id"], f"权限模式 {status}{note}")
        print(f"[{self.name}][permit]={on}")
        if on and self.last_task:
            feishu_api.reply_message(self.client, msg["id"], "是否用新权限重跑上一条任务？发送 /retry 确认")

    def cmd_unsafe(self, msg, arg):
        self.unsafe_mode = not self.unsafe_mode
        on = self.unsafe_mode
        # 与 permit 互斥：开 unsafe 则关掉 permit
        cleared = on and self.permit_mode
        if on:
            self.permit_mode = False
        self.save_bot_state()
        status = ("已开启（跳过全部权限校验，可执行任意命令含 bash，请谨慎）"
                  if on else "已关闭")
        note = "（已自动关闭文件读写 permit）" if cleared else ""
        feishu_api.reply_message(self.client, msg["id"], f"最高权限 {status}{note}")
        print(f"[{self.name}][unsafe]={on}")
        if on and self.last_task:
            feishu_api.reply_message(self.client, msg["id"], "是否用新权限重跑上一条任务？发送 /retry 确认")

    def cmd_model(self, msg, arg):
        """列出可选模型，回复序号选择（作用于当前对话）。"""
        # 确保有一个 session 承载模型选择（无则按首用初始化）
        if not self.sessions["list"]:
            entry = self.make_session()
            self.sessions = {"list": [entry], "current": 0, "history": []}
            self.set_current(entry)
        cur = self.current_entry()
        cur_model = cur.get("model") if cur else None
        options = ["默认（不指定）"] + list(self.models)  # 序号 1=默认，2..=各模型
        # 当前选中项在 options 里的下标
        cur_pos = 0 if not cur_model else (self.models.index(cur_model) + 1 if cur_model in self.models else -1)
        lines = ["选择当前对话使用的模型，回复序号："]
        for i, name in enumerate(options, 1):
            lines.append(f"{i}. {name}{' ◀ 当前' if i - 1 == cur_pos else ''}")
        self.pending = "model"
        feishu_api.reply_message(self.client, msg["id"], "\n".join(lines))

    def cmd_context(self, msg, arg):
        """查看当前对话的 context 窗口占用（读磁盘 session 文件的最近一轮用量）。"""
        entry = self.current_entry()
        if not entry or not entry.get("id"):
            feishu_api.reply_message(self.client, msg["id"],
                                     "当前对话还没开始，暂无 context 信息（先发一条任务）")
            return
        info = claude_runner.session_context(entry["id"], self.work_dir)
        if not info:
            feishu_api.reply_message(self.client, msg["id"],
                                     "读不到当前对话的 context 记录（session 文件不存在或尚无用量）")
            return
        feishu_api.reply_message(self.client, msg["id"], self._format_context(entry, info))
        print(f"[{self.name}][context] used={info['total_input']} model={info.get('model')}")

    @staticmethod
    def _fmt_tokens(n):
        """token 数人性化：42299→42.3k、200000→200k、856→856。"""
        if n >= 1000:
            v = n / 1000
            return f"{v:.0f}k" if v == int(v) else f"{v:.1f}k"
        return str(n)

    def _format_context(self, entry, info):
        used = info["total_input"]
        pct = min(used / CONTEXT_WINDOW * 100, 100)
        filled = round(pct / 10)
        bar = "▓" * filled + "░" * (10 - filled)
        model = info.get("model") or entry.get("model") or "默认"
        if model.startswith("claude-"):
            model = model[7:]
        # 本地历史每轮 = user+assistant 两条
        turns = len(self.sessions.get("history", [])) // 2
        return "\n".join([
            f"📊 当前对话「{entry['label']}」",
            f"模型：{model}",
            f"Context：{self._fmt_tokens(used)} / {self._fmt_tokens(CONTEXT_WINDOW)} tokens（{pct:.0f}%）",
            f"{bar} {pct:.0f}%",
            f"├ 缓存读取：{self._fmt_tokens(info['cache_read'])}",
            f"├ 缓存创建：{self._fmt_tokens(info['cache_creation'])}",
            f"└ 新输入：{self._fmt_tokens(info['input'])}",
            f"本地历史：{turns} 轮（session 失效时用于重建 context）",
        ])

    def cmd_usage(self, msg, arg):
        """查看 Claude 订阅用量（官方 oauth/usage 端点，按需+缓存）。"""
        try:
            text = usage.report()
        except usage.NoCredentials as e:
            text = f"查不到用量：{e}"
        except usage.TokenExpired as e:
            text = str(e)
        except usage.RateLimited:
            text = "用量端点暂时限流，请稍后再试"
        except Exception as e:
            text = f"[error] 查询用量失败: {e}"
        feishu_api.reply_message(self.client, msg["id"], text)
        print(f"[{self.name}][usage] queried")

    def cmd_del(self, msg, arg):
        data = self.sessions
        if not arg.isdigit():
            feishu_api.reply_message(self.client, msg["id"], "用法：/del <序号>（序号见 /sessions）")
            return
        if not data["list"]:
            feishu_api.reply_message(self.client, msg["id"], "当前没有可删除的对话")
            return
        didx = int(arg) - 1
        if not (0 <= didx < len(data["list"])):
            feishu_api.reply_message(self.client, msg["id"], f"无效序号，请输入 1～{len(data['list'])}")
            return
        with self.lock:  # 与 worker 的 update_session 互斥，防列表 pop/重排与写入相撞
            removed = data["list"].pop(didx)
            # 删磁盘上的 Claude session 文件
            if removed["id"]:
                try:
                    claude_runner.delete_claude_session(removed["id"], self.work_dir)
                except Exception as e:
                    print(f"[del] remove claude session file failed: {e}")
            # 删 DB session 行（审计记录经 ON DELETE SET NULL 保留）
            if removed.get("_row_id"):
                self.db(db.delete_session, removed["_row_id"])
            # 修正 current 指针
            if not data["list"]:
                new_entry = self.make_session()  # 删空了，补一个新的空对话
                data["list"].append(new_entry)
                data["current"] = 0
                data["history"] = []
                self.set_current(new_entry)
            else:
                if didx <= data["current"]:
                    data["current"] = max(0, data["current"] - 1)
                cur_entry = data["list"][data["current"]]
                self.set_current(cur_entry)
                data["history"] = self.load_history(cur_entry["id"])
            remaining = len(data["list"])
        self.pending = None
        feishu_api.reply_message(self.client, msg["id"], f"已删除对话「{removed['label']}」（剩 {remaining} 个）")
        print(f"[{self.name}][del] removed idx={didx} row={removed.get('_row_id')}")

    @staticmethod
    def _model_suffix(used_model):
        """成功结果末尾追加「（模型：…）」；去掉 claude- 前缀。"""
        if not used_model:
            return ""
        short = used_model[7:] if used_model.startswith("claude-") else used_model
        return f"\n\n（模型：{short}）"

    def _permit_banner(self):
        """若开着 permit/unsafe，返回提醒文案（防止开启后忘关）。互斥后至多一条。"""
        if self.unsafe_mode:
            return "⚠️ 最高权限(unsafe)开启中，可执行任意命令 — 用完发 /unsafe 关闭"
        if self.permit_mode:
            return "🔓 文件读写(permit)开启中 — 用完发 /permit 关闭"
        return ""

    def _start_heartbeat(self, msg_id):
        """长任务期间周期回「仍在处理」，返回 stop_event；调用方在 finally 里 .set()。
        token 由 SDK 客户端内部自动刷新，长任务无需担心 token 过期。"""
        stop = threading.Event()
        if self.heartbeat_interval <= 0:
            return stop
        started = time.time()

        def beat():
            interval = self.heartbeat_interval
            while not stop.wait(interval):          # 被 set 时立即返回 True 退出
                elapsed = int(time.time() - started)
                try:
                    feishu_api.reply_message(self.client, msg_id, f"⏳ 仍在处理中，已用 {self._human_elapsed(elapsed)}…", tag="heartbeat")
                except Exception as e:
                    print(f"[{self.name}][heartbeat-error] {e}")
                interval = min(interval * 2, 600)   # 逐步加倍、上限 10 分钟，防刷屏

        threading.Thread(target=beat, name="heartbeat", daemon=True).start()
        return stop

    @staticmethod
    def _human_elapsed(seconds):
        """把秒数转成人类友好的时长：45s / 12m / 3h05m（长任务用小时更直观）。"""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"

    def cmd_retry(self, msg, arg):
        with self.lock:
            t = self.last_task
        if not t:
            feishu_api.reply_message(self.client, msg["id"], "没有可重跑的任务")
            return
        self._enqueue({"kind": "retry", "text": t["text"], "msg_id": t["msg_id"],
                       "session_id": t["session_id"]},
                      msg["id"], verb="重跑")

    # --------------------------------------------------------------- 任务执行（异步）

    def run_task(self, msg, text):
        """主线程只做入队 + 立即回执，真正执行在后台 worker，故长任务不冻结 bot。"""
        self.pending = None
        with self.lock:
            self.last_task = {"text": text, "msg_id": msg["id"],
                              "session_id": self.current_session_id()}
        self._enqueue({"kind": "task", "text": text, "msg_id": msg["id"]},
                      msg["id"], verb="执行")

    def _enqueue(self, job, msg_id, verb):
        """把任务丢后台队列，立即回执（不阻塞主循环）。"""
        ahead = self.task_queue.qsize()
        self.task_queue.put(job)
        lines = [f"✅ 已接收，任务进入后台{verb}，完成后把结果发给你"]
        if ahead > 0:
            lines.append(f"（前面还有 {ahead} 个任务在排队）")
        banner = self._permit_banner()
        if banner:
            lines.append(banner)
        feishu_api.reply_message(self.client, msg_id, "\n".join(lines))
        print(f"[{self.name}][enqueue] {verb} qsize={ahead + 1} | {job['text'][:60]}")

    def _worker(self):
        """后台执行 claude 任务：同一 chat_id 由本 worker 单线程串行处理、保序；
        不同 chat_id 各有自己的 worker 可并行——真正的同时并发数由全局 run_slot 上限约束。"""
        while True:
            job = self.task_queue.get()
            try:
                self._execute_job(job)
            except Exception as e:
                print(f"[{self.name}][worker-error] {e}")
            finally:
                self.task_queue.task_done()

    def _execute_job(self, job):
        text, msg_id = job["text"], job["msg_id"]
        is_retry = job["kind"] == "retry"

        # 锁内：准备会话状态、快照 history（短临界区，不含长调用）
        with self.lock:
            if is_retry:
                session_id, history, first_task = job.get("session_id"), None, None
                is_new = session_id is None
            else:
                is_new = self.current_session_id() is None
                if not self.sessions["list"]:  # 首用，建 session 条目
                    entry = self.make_session()
                    self.sessions = {"list": [entry], "current": 0, "history": []}
                    self.set_current(entry)
                session_id = self.current_session_id()
                history = list(self.sessions.get("history", []))
                first_task = text if is_new else None
            permit_mode = self.permit_mode
            unsafe_mode = self.unsafe_mode

        print(f"[{self.name}][{'retry' if is_retry else 'task'}] permit={permit_mode} "
              f"unsafe={unsafe_mode} session={'new' if is_new else str(session_id)[:8] + '…'} | {text[:60]}")

        # 锁外：长耗时执行 + 心跳（此期间主循环命令不被阻塞）
        # 全局并发闸：不同 chat_id 可并行，但同时运行的 claude 进程数受 run_slot 上限约束；
        # 心跳在闸外启动，等待空位期间也会回「仍在处理中」。elapsed 只计实际执行、不含排队等待。
        stop = self._start_heartbeat(msg_id)
        try:
            with self.run_slot:
                t0 = time.time()
                result, new_sid, used_model = self.execute_claude(
                    text, session_id, history=history, first_task=first_task,
                )
                elapsed = time.time() - t0
        finally:
            stop.set()

        # LLM 响应审计：耗时 / 模型 / 是否出错 / 输出规模 / 新 session（同时落库）
        is_err = result.startswith("[error]")
        print(f"[{self.name}][llm] elapsed={self._human_elapsed(int(elapsed))} "
              f"model={used_model} error={is_err} timed_out={'超时' in result} chars={len(result)} "
              f"sid={str(new_sid)[:8] + '…' if new_sid else '-'} | {result[:120].replace(chr(10), ' ')}")
        self.db(db.record_audit, self.chat_id, "llm", "retry" if is_retry else "task",
                message_id=msg_id, ok=not is_err, model=used_model,
                elapsed_ms=int(elapsed * 1000), chars=len(result), detail=result[:2000])

        # 锁内：更新对话历史（保留最近 20 轮）+ 审计
        with self.lock:
            if not is_retry and not result.startswith("[error]"):
                data = self.sessions
                data.setdefault("history", [])
                data["history"].append({"role": "user", "content": text})
                data["history"].append({"role": "assistant", "content": result})
                if len(data["history"]) > 40:  # 20 轮 × 2
                    data["history"] = data["history"][-40:]
            self.audit(text, result, new_sid, used_model)

        # 主动推送结果（带失败重试，避免网络抖动丢结果）
        banner = self._permit_banner()
        suffix = "" if result.startswith("[error]") else self._model_suffix(used_model)
        self._push_reply(msg_id, result + suffix + (("\n\n" + banner) if banner else ""))
        print(f"[{self.name}][done] pushed to {msg_id} model={used_model}")

    def _push_reply(self, msg_id, text, attempts=3):
        """主动推送结果，失败重试几次（后台任务跑很久，不能因一次网络抖动就丢结果）。"""
        for i in range(attempts):
            try:
                res = feishu_api.reply_message(self.client, msg_id, text, tag="push")
                self._audit_sends(msg_id, "push", res)
                if res:
                    return True
            except Exception as e:
                print(f"[{self.name}][push-error] attempt {i + 1}/{attempts}: {e}")
            if i < attempts - 1:
                time.sleep(min(5 * (i + 1), 30))
        print(f"[{self.name}][push-failed] 结果推送失败 msg={msg_id}（结果已存审计表，可查库）")
        return False

    def _audit_sends(self, msg_id, tag, res):
        """把一次飞书外发的每段响应写入审计表。"""
        for s in res.sends:
            self.db(db.record_audit, self.chat_id, "feishu", tag, message_id=msg_id,
                    ok=s["ok"], code=s["code"], chars=s.get("chars"),
                    detail=(f"sent_id={s.get('sent_id')}" if s["ok"] else s.get("msg")))

    # ---------------------------------------------------------- 消息处理 / 主循环

    def handle_select(self, msg, idx):
        """pending 状态下用数字消息选择对话 / 模型。"""
        if self.pending == "session":
            sessions = self.sessions["list"]
            if 0 <= idx < len(sessions):
                with self.lock:  # 与 worker 的 update_session 互斥
                    self.sessions["current"] = idx
                    entry = sessions[idx]
                    self.set_current(entry)
                    self.sessions["history"] = self.load_history(entry["id"])
                self.pending = None
                feishu_api.reply_message(self.client, msg["id"], f"已切换到对话 {idx + 1}：{entry['label']}")
                print(f"[{self.name}][session] switched to idx={idx}")
            else:
                feishu_api.reply_message(self.client, msg["id"], f"无效序号，请输入 1～{len(sessions)}")
        elif self.pending == "model":
            options = ["默认（不指定）"] + list(self.models)  # idx 0=默认
            if 0 <= idx < len(options):
                model = None if idx == 0 else self.models[idx - 1]
                entry = self.current_entry()
                self.pending = None
                if entry:
                    entry["model"] = model
                    if entry.get("_row_id"):
                        self.db(db.set_session_model, entry["_row_id"], model)
                feishu_api.reply_message(self.client, msg["id"], f"当前对话模型已设为：{options[idx]}")
                print(f"[{self.name}][model] -> {model}")
            else:
                feishu_api.reply_message(self.client, msg["id"], f"无效序号，请输入 1～{len(options)}")

    def handle_upload(self, msg):
        """下载并安全存储上传的图片/文件/压缩包，写台账，回复保存路径。"""
        try:
            info = uploads.save_upload(self.client, msg, self.chat_id)
        except uploads.TooLarge:
            mb = uploads.MAX_UPLOAD_BYTES // (1024 * 1024)
            feishu_api.reply_message(self.client, msg["id"], f"文件超过大小上限（>{mb}MB），未保存")
            return
        except Exception as e:
            feishu_api.reply_message(self.client, msg["id"], f"[error] 保存上传文件失败: {e}")
            print(f"[{self.name}][upload-error] {e}")
            return
        self.db(db.record_upload, msg["id"], self.chat_id, msg.get("resource_type"),
                info["file_name"], info["path"], info["size"], info["content_type"])
        feishu_api.reply_message(self.client, msg["id"], "\n".join([
            "已保存上传文件：",
            info["path"],
            f"大小 {info['size_human']}",
            "（如需分析，发任务引用此路径）",
        ]))
        print(f"[{self.name}][upload] {info['path']} ({info['size_human']})")

    def handle_post_images(self, msg):
        """下载 post 富文本里内嵌的图片，写台账，返回 (文字, [本地路径,...])。"""
        text = msg.get("text", "")
        paths = []
        for idx, image_key in enumerate(msg.get("images", []), 1):
            synth = {"id": msg["id"], "file_key": image_key,
                     "resource_type": "image", "file_name": f"img{idx}"}
            try:
                info = uploads.save_upload(self.client, synth, self.chat_id)
            except Exception as e:
                print(f"[{self.name}][post-img-error] {e}")
                continue
            self.db(db.record_upload, msg["id"], self.chat_id, "image",
                    info["file_name"], info["path"], info["size"], info["content_type"])
            paths.append(info["path"])
        return text, paths

    def handle_unsupported(self, msg):
        """处理不了的消息（不支持的类型）：留痕到 DB + 回提示。"""
        mt = msg.get("msg_type", "?")
        self.db(db.record_unhandled, msg["id"], self.chat_id, mt, msg.get("raw", ""))
        feishu_api.reply_message(self.client, msg["id"], "不支持处理当前消息类型")
        print(f"[{self.name}][unhandled] msg_type={mt} id={msg['id']}")

    def handle_message(self, msg):
        # 文件类消息（图片/文件/压缩包）先分流（这些消息没有 text 字段）
        if msg.get("kind") == "file":
            self.handle_upload(msg)
            return
        # 处理不了的类型：留痕 + 提示
        if msg.get("kind") == "unsupported":
            self.handle_unsupported(msg)
            return
        # 富文本 post：下载内嵌图片；有图则作为带附件的任务，纯文本则按文本继续
        if msg.get("kind") == "post":
            ptext, paths = self.handle_post_images(msg)
            if paths:
                full = (ptext or "请查看随附的图片").rstrip()
                full += "\n\n[随消息附带的图片，可直接读取]\n" + "\n".join(paths)
                self.run_task(msg, full)
                return
            msg = {**msg, "text": ptext}  # 纯文本 post，降级为普通文本处理
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # 等待序号时，数字消息用于选择
        if self.pending and text.isdigit():
            self.handle_select(msg, int(text) - 1)
            return
        # 命令分发（首词为命令，其余为参数）
        head, _, rest = text.partition(" ")
        handler = self.commands.get(head.lower())
        if handler:
            handler(msg, rest.strip())
            return
        # 普通任务
        self.run_task(msg, text)

    # ------------------------------------------------------------ 长连接入口 / 启动

    def start(self):
        """启动后台线程：inbox 分发（命令回复/上传下载/入队）+ worker（跑 claude 长任务）。
        事件由长连接推来，绝不在 ws 事件循环里做网络 IO。"""
        threading.Thread(target=self._inbox_worker, name=f"{self.name}-inbox", daemon=True).start()
        threading.Thread(target=self._worker, name=f"{self.name}-worker", daemon=True).start()
        print(f"[{self.name}] ready, chat_id={self.chat_id}, work_dir={self.work_dir}")

    def on_event(self, msg):
        """长连接事件入口：跑在 ws 事件循环线程里，必须快速返回。
        按 message_id 幂等去重（飞书事件可能重投）后丢进 inbox，实际处理在 inbox 线程。"""
        mid = msg.get("id")
        if mid is not None:
            if mid in self._seen_ids:
                print(f"[{self.name}][dup] ignore redelivered msg {mid}")
                return
            self._seen_ids[mid] = True
            if len(self._seen_ids) > self._seen_max:
                self._seen_ids.popitem(last=False)
        self.inbox.put(msg)

    def _inbox_worker(self):
        """串行处理本 chat 的消息（命令回复、上传下载、把长任务入 task_queue）：
        同一 chat 保序，不阻塞 ws 事件循环。等价于旧轮询线程里 handle_message 的角色。"""
        while True:
            msg = self.inbox.get()
            try:
                self.handle_message(msg)
            except Exception as e:
                print(f"[{self.name}][inbox-error] {e}")
            finally:
                self.inbox.task_done()
