"""Bot：单个飞书机器人（一个 chat_id）的消息处理与会话编排。

每个 Bot 实例对应一个 chat_id，状态持久化在 MySQL。同一 chat_id 的消息在
Bot.run() 的单循环里串行处理，天然无并发。多机器人通过多进程（每个一份 .env.<name>）
运行，互不阻塞。
"""

import os
import subprocess
import time

import claude_runner
import db
import feishu_api
import uploads
import usage

HELP_TEXT = "\n".join([
    "可用命令：",
    "/ls        — 列出工作目录，回复序号切换",
    "/sessions  — 列出当前目录的对话，回复序号切换",
    "/new       — 新建对话（清空历史）",
    "/name <名称> — 重命名当前对话",
    "/del <序号> — 删除指定对话（序号见 /sessions）",
    "/permit    — 开关当前目录的文件读写（acceptEdits，不跑命令）",
    "/model     — 选择当前对话使用的模型",
    "/usage     — 查看 Claude 订阅用量（5 小时窗 / 7 天）",
    "/retry     — 用当前权限重跑上一条任务",
    "/help      — 显示此帮助",
])


class Bot:
    """飞书消息驱动 Claude Code 的轮询服务，状态持久化在 MySQL。"""

    def __init__(self, bot_cfg, session_factory, poll_interval, task_timeout, models=None):
        self.name = bot_cfg["name"]
        self.app_id = bot_cfg["app_id"]
        self.app_secret = bot_cfg["app_secret"]
        self.chat_id = bot_cfg["chat_id"]
        self.work_dirs = bot_cfg["work_dirs"]  # 已是 dict
        self.dir_names = list(self.work_dirs.keys())
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.models = models or ["opus", "sonnet", "haiku"]  # /model 可选清单
        self.default_name = self.dir_names[0] if self.dir_names else "daily-assistant"

        self.Session = session_factory

        # 恢复持久化状态
        state = self.db(db.load_state, self.chat_id) or {}
        saved = state.get("last_dir_name")
        name = saved if saved in self.work_dirs else self.default_name
        self.last_dir = {"name": name, "path": self.work_dirs[name]}
        # {dir_name: {"list": [{id, label, _row_id}, ...], "current": idx, "history": [...]}}
        self.dir_sessions = state.get("dir_sessions", {})
        self.permit_modes = state.get("permit_modes", {})  # {dir_name: bool}

        self.pending = None   # "dir" | "session"，当前等待用户回复序号的类型
        self.last_task = None  # {"text", "msg_id", "dir_name", "session_id"}，用于 /retry 重跑

        # 命令分发表：命令 -> 处理方法(token, msg, arg)
        self.commands = {
            "/ls": self.cmd_ls,
            "/sessions": self.cmd_sessions,
            "/help": self.cmd_help,
            "/name": self.cmd_name,
            "/new": self.cmd_new,
            "/permit": self.cmd_permit,
            "/model": self.cmd_model,
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
        self.db(db.save_bot_state, self.chat_id, self.last_dir["name"], self.permit_modes)

    # -------------------------------------------------------------- session

    def current_session_id(self, dir_name):
        data = self.dir_sessions.get(dir_name)
        if not data:
            return None
        return data["list"][data["current"]]["id"]

    def new_entry(self, label=None):
        ts = time.strftime("%m-%d %H:%M")
        return {"id": None, "label": label or f"新对话 {ts}", "model": None, "_row_id": None}

    def current_entry(self, dir_name):
        data = self.dir_sessions.get(dir_name)
        if not data or not data.get("list"):
            return None
        return data["list"][data["current"]]

    def make_session(self, dir_name, label=None):
        """创建内存 session 条目并在 DB 中建行，返回条目（含 _row_id）。"""
        entry = self.new_entry(label)
        position = len(self.dir_sessions.get(dir_name, {}).get("list", []))
        entry["_row_id"] = self.db(db.insert_session, self.chat_id, dir_name, entry["label"], position)
        return entry

    def set_current(self, dir_name, entry):
        if entry.get("_row_id"):
            self.db(db.set_current, self.chat_id, dir_name, entry["_row_id"])

    def load_history(self, claude_sid):
        return self.db(db.load_history, self.chat_id, claude_sid) or []

    def update_session(self, dir_name, session_id, first_task=None):
        """首次执行时写入 session_id 和标签，并同步到 DB。"""
        data = self.dir_sessions.setdefault(dir_name, {"list": [], "current": 0})
        entry = data["list"][data["current"]]
        entry["id"] = session_id
        new_label = None
        if first_task and entry["label"].startswith("新对话"):
            ts = time.strftime("%m-%d %H:%M")
            entry["label"] = f"{ts} {first_task[:20]}{'…' if len(first_task) > 20 else ''}"
            new_label = entry["label"]
        if entry.get("_row_id"):
            self.db(db.update_session, entry["_row_id"], claude_sid=session_id, label=new_label)

    def audit(self, dir_name, user_text, result, new_session_id, used_model=None):
        """把一轮对话（用户输入 + LLM 输出）写入审计表。"""
        data = self.dir_sessions.get(dir_name, {})
        entry = data["list"][data["current"]] if data.get("list") else None
        row_id = entry.get("_row_id") if entry else None
        sid = (entry.get("id") if entry else None) or new_session_id
        self.db(db.append_message, row_id, self.chat_id, dir_name, sid, "user", user_text)
        self.db(db.append_message, row_id, self.chat_id, dir_name, sid, "assistant", result,
                result.startswith("[error]"), "超时" in result, model=used_model)

    def execute_claude(self, text, dir_name, session_id, history=None, first_task=None):
        """调用 Claude Code，处理超时/异常，成功时同步 session_id 到 DB。
        返回 (result, new_sid, used_model)。"""
        permit = self.permit_modes.get(dir_name, False)
        # 始终把该聊天上传目录纳入工作区（存在才加），让 Claude 能读上传文件
        up = uploads.chat_dir(self.chat_id)
        extra_dirs = [up] if os.path.isdir(up) else []
        entry = self.current_entry(dir_name)
        model = entry.get("model") if entry else None
        try:
            result, new_sid, used_model = claude_runner.run_claude(
                text, self.work_dirs[dir_name], self.task_timeout,
                session_id, permit=permit, extra_dirs=extra_dirs, history=history, model=model,
            )
            if new_sid:
                self.update_session(dir_name, new_sid, first_task=first_task)
        except subprocess.TimeoutExpired:
            result, new_sid, used_model = f"[error] 任务超时（>{self.task_timeout}s）", None, None
        except Exception as e:
            result, new_sid, used_model = f"[error] 执行失败: {e}", None, None
        return result, new_sid, used_model

    # ------------------------------------------------------------- commands

    def cmd_ls(self, token, msg, arg):
        self.pending = "dir"
        feishu_api.reply_message(token, msg["id"], feishu_api.build_dir_prompt(self.dir_names))

    def cmd_sessions(self, token, msg, arg):
        dir_name = self.last_dir["name"]
        data = self.dir_sessions.get(dir_name)
        if not data or not data["list"]:
            feishu_api.reply_message(token, msg["id"], f"[{dir_name}] 当前只有 1 个对话，发送 /new 创建新对话")
        else:
            self.pending = "session"
            feishu_api.reply_message(token, msg["id"], feishu_api.build_sessions_prompt(dir_name, data["list"], data["current"]))

    def cmd_help(self, token, msg, arg):
        feishu_api.reply_message(token, msg["id"], HELP_TEXT)

    def cmd_name(self, token, msg, arg):
        if not arg:
            feishu_api.reply_message(token, msg["id"], "用法：/name <名称>")
            return
        dir_name = self.last_dir["name"]
        data = self.dir_sessions.get(dir_name)
        if data and data["list"]:
            entry = data["list"][data["current"]]
            entry["label"] = arg
            if entry.get("_row_id"):
                self.db(db.update_session, entry["_row_id"], label=arg)
            feishu_api.reply_message(token, msg["id"], f"当前对话已命名为「{arg}」")
        else:
            feishu_api.reply_message(token, msg["id"], "当前没有活跃的对话")

    def cmd_new(self, token, msg, arg):
        dir_name = self.last_dir["name"]
        data = self.dir_sessions.setdefault(dir_name, {"list": [], "current": 0, "history": []})
        entry = self.make_session(dir_name)
        data["list"].append(entry)
        data["current"] = len(data["list"]) - 1
        data["history"] = []  # 清空历史，真正从零开始
        self.set_current(dir_name, entry)
        self.pending = None
        feishu_api.reply_message(token, msg["id"], f"[{dir_name}] 已创建新对话（共 {len(data['list'])} 个），发送任务即可开始")
        print(f"[new-session] {dir_name} total={len(data['list'])}")

    def cmd_permit(self, token, msg, arg):
        dir_name = self.last_dir["name"]
        self.permit_modes[dir_name] = not self.permit_modes.get(dir_name, False)
        on = self.permit_modes[dir_name]
        self.save_bot_state()
        status = "已开启（可在当前目录读写/修改文件，不执行命令）" if on else "已关闭"
        feishu_api.reply_message(token, msg["id"], f"[{dir_name}] 权限模式 {status}")
        print(f"[permit] {dir_name}={on}")
        if on and self.last_task:
            feishu_api.reply_message(token, msg["id"], "是否用新权限重跑上一条任务？发送 /retry 确认")

    def cmd_model(self, token, msg, arg):
        """列出可选模型，回复序号选择（作用于当前对话）。"""
        dir_name = self.last_dir["name"]
        # 确保当前目录有一个 session 承载模型选择（无则按首用初始化）
        if self.dir_sessions.get(dir_name) is None:
            entry = self.make_session(dir_name)
            self.dir_sessions[dir_name] = {"list": [entry], "current": 0, "history": []}
            self.set_current(dir_name, entry)
        cur = self.current_entry(dir_name)
        cur_model = cur.get("model") if cur else None
        options = ["默认（不指定）"] + list(self.models)  # 序号 1=默认，2..=各模型
        # 当前选中项在 options 里的下标
        cur_pos = 0 if not cur_model else (self.models.index(cur_model) + 1 if cur_model in self.models else -1)
        lines = ["选择当前对话使用的模型，回复序号："]
        for i, name in enumerate(options, 1):
            lines.append(f"{i}. {name}{' ◀ 当前' if i - 1 == cur_pos else ''}")
        self.pending = "model"
        feishu_api.reply_message(token, msg["id"], "\n".join(lines))

    def cmd_usage(self, token, msg, arg):
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
        feishu_api.reply_message(token, msg["id"], text)
        print(f"[{self.name}][usage] queried")

    def cmd_del(self, token, msg, arg):
        dir_name = self.last_dir["name"]
        data = self.dir_sessions.get(dir_name)
        if not arg.isdigit():
            feishu_api.reply_message(token, msg["id"], "用法：/del <序号>（序号见 /sessions）")
            return
        if not data or not data["list"]:
            feishu_api.reply_message(token, msg["id"], f"[{dir_name}] 当前没有可删除的对话")
            return
        didx = int(arg) - 1
        if not (0 <= didx < len(data["list"])):
            feishu_api.reply_message(token, msg["id"], f"无效序号，请输入 1～{len(data['list'])}")
            return
        removed = data["list"].pop(didx)
        # 删磁盘上的 Claude session 文件
        if removed["id"]:
            try:
                claude_runner.delete_claude_session(removed["id"], self.work_dirs[dir_name])
            except Exception as e:
                print(f"[del] remove claude session file failed: {e}")
        # 删 DB session 行（审计记录经 ON DELETE SET NULL 保留）
        if removed.get("_row_id"):
            self.db(db.delete_session, removed["_row_id"])
        # 修正 current 指针
        if not data["list"]:
            new_entry = self.make_session(dir_name)  # 删空了，补一个新的空对话
            data["list"].append(new_entry)
            data["current"] = 0
            data["history"] = []
            self.set_current(dir_name, new_entry)
        else:
            if didx <= data["current"]:
                data["current"] = max(0, data["current"] - 1)
            cur_entry = data["list"][data["current"]]
            self.set_current(dir_name, cur_entry)
            data["history"] = self.load_history(cur_entry["id"])
        self.pending = None
        feishu_api.reply_message(token, msg["id"], f"[{dir_name}] 已删除对话「{removed['label']}」（剩 {len(data['list'])} 个）")
        print(f"[del] {dir_name} removed idx={didx} row={removed.get('_row_id')}")

    @staticmethod
    def _model_suffix(used_model):
        """成功结果末尾追加「（模型：…）」；去掉 claude- 前缀。"""
        if not used_model:
            return ""
        short = used_model[7:] if used_model.startswith("claude-") else used_model
        return f"\n\n（模型：{short}）"

    def cmd_retry(self, token, msg, arg):
        t = self.last_task
        if not t:
            feishu_api.reply_message(token, msg["id"], "没有可重跑的任务")
            return
        feishu_api.reply_message(token, msg["id"], f"正在 [{t['dir_name']}] 重跑任务，请稍候…")
        result, new_sid, used_model = self.execute_claude(t["text"], t["dir_name"], t["session_id"])
        self.audit(t["dir_name"], t["text"], result, new_sid, used_model)
        suffix = "" if result.startswith("[error]") else self._model_suffix(used_model)
        feishu_api.reply_message(token, t["msg_id"], result + suffix)
        print("[retry] done")

    # --------------------------------------------------------------- 任务执行

    def run_task(self, token, msg, text, dir_name):
        self.pending = None
        is_new = self.dir_sessions.get(dir_name) is None or self.current_session_id(dir_name) is None
        session_id = self.current_session_id(dir_name)

        # 首次使用此目录，自动初始化 session 条目（建 DB 行）
        if self.dir_sessions.get(dir_name) is None:
            entry = self.make_session(dir_name)
            self.dir_sessions[dir_name] = {"list": [entry], "current": 0, "history": []}
            self.set_current(dir_name, entry)

        history = self.dir_sessions.get(dir_name, {}).get("history", [])
        self.last_task = {"text": text, "msg_id": msg["id"], "dir_name": dir_name, "session_id": session_id}
        permit_mode = self.permit_modes.get(dir_name, False)
        print(f"[{self.name}][task] dir={dir_name} permit={permit_mode} "
              f"session={'new' if is_new else session_id[:8] + '…'} | {text[:60]}")
        feishu_api.reply_message(token, msg["id"], f"正在 [{dir_name}] 执行任务，请稍候…")

        result, new_sid, used_model = self.execute_claude(
            text, dir_name, session_id, history=history,
            first_task=text if is_new else None,
        )
        # 更新对话历史（保留最近 20 轮避免 prompt 过长）
        if not result.startswith("[error]"):
            data = self.dir_sessions.setdefault(dir_name, {"list": [], "current": 0, "history": []})
            data.setdefault("history", [])
            data["history"].append({"role": "user", "content": text})
            data["history"].append({"role": "assistant", "content": result})
            if len(data["history"]) > 40:  # 20 轮 × 2
                data["history"] = data["history"][-40:]

        self.audit(dir_name, text, result, new_sid, used_model)
        suffix = "" if result.startswith("[error]") else self._model_suffix(used_model)
        feishu_api.reply_message(token, msg["id"], result + suffix)
        print(f"[{self.name}][done] replied to {msg['id']} model={used_model}")

    # ---------------------------------------------------------- 消息处理 / 主循环

    def handle_select(self, token, msg, idx):
        """pending 状态下用数字消息选择目录 / 对话。"""
        dir_name = self.last_dir["name"]
        if self.pending == "dir":
            if 0 <= idx < len(self.dir_names):
                name = self.dir_names[idx]
                self.last_dir = {"name": name, "path": self.work_dirs[name]}
                self.pending = None
                self.save_bot_state()
                feishu_api.reply_message(token, msg["id"], f"已切换到 [{name}]")
                print(f"[dir] switched to {name}")
            else:
                feishu_api.reply_message(token, msg["id"], f"无效序号，请输入 1～{len(self.dir_names)}")
        elif self.pending == "session":
            sessions = self.dir_sessions.get(dir_name, {}).get("list", [])
            if 0 <= idx < len(sessions):
                self.dir_sessions[dir_name]["current"] = idx
                entry = sessions[idx]
                self.pending = None
                self.set_current(dir_name, entry)
                self.dir_sessions[dir_name]["history"] = self.load_history(entry["id"])
                feishu_api.reply_message(token, msg["id"], f"已切换到对话 {idx + 1}：{entry['label']}")
                print(f"[session] {dir_name} switched to idx={idx}")
            else:
                feishu_api.reply_message(token, msg["id"], f"无效序号，请输入 1～{len(sessions)}")
        elif self.pending == "model":
            options = ["默认（不指定）"] + list(self.models)  # idx 0=默认
            if 0 <= idx < len(options):
                model = None if idx == 0 else self.models[idx - 1]
                entry = self.current_entry(dir_name)
                self.pending = None
                if entry:
                    entry["model"] = model
                    if entry.get("_row_id"):
                        self.db(db.set_session_model, entry["_row_id"], model)
                feishu_api.reply_message(token, msg["id"], f"当前对话模型已设为：{options[idx]}")
                print(f"[model] {dir_name} -> {model}")
            else:
                feishu_api.reply_message(token, msg["id"], f"无效序号，请输入 1～{len(options)}")

    def handle_upload(self, token, msg):
        """下载并安全存储上传的图片/文件/压缩包，写台账，回复保存路径。"""
        try:
            info = uploads.save_upload(token, msg, self.chat_id)
        except uploads.TooLarge:
            mb = uploads.MAX_UPLOAD_BYTES // (1024 * 1024)
            feishu_api.reply_message(token, msg["id"], f"文件超过大小上限（>{mb}MB），未保存")
            return
        except Exception as e:
            feishu_api.reply_message(token, msg["id"], f"[error] 保存上传文件失败: {e}")
            print(f"[{self.name}][upload-error] {e}")
            return
        self.db(db.record_upload, msg["id"], self.chat_id, msg.get("resource_type"),
                info["file_name"], info["path"], info["size"], info["content_type"])
        feishu_api.reply_message(token, msg["id"], "\n".join([
            "已保存上传文件：",
            info["path"],
            f"大小 {info['size_human']}",
            "（如需分析，发任务引用此路径）",
        ]))
        print(f"[{self.name}][upload] {info['path']} ({info['size_human']})")

    def handle_post_images(self, token, msg):
        """下载 post 富文本里内嵌的图片，写台账，返回 (文字, [本地路径,...])。"""
        text = msg.get("text", "")
        paths = []
        for idx, image_key in enumerate(msg.get("images", []), 1):
            synth = {"id": msg["id"], "file_key": image_key,
                     "resource_type": "image", "file_name": f"img{idx}"}
            try:
                info = uploads.save_upload(token, synth, self.chat_id)
            except Exception as e:
                print(f"[{self.name}][post-img-error] {e}")
                continue
            self.db(db.record_upload, msg["id"], self.chat_id, "image",
                    info["file_name"], info["path"], info["size"], info["content_type"])
            paths.append(info["path"])
        return text, paths

    def handle_unsupported(self, token, msg):
        """处理不了的消息（不支持的类型）：留痕到 DB + 回提示。"""
        mt = msg.get("msg_type", "?")
        self.db(db.record_unhandled, msg["id"], self.chat_id, mt, msg.get("raw", ""))
        feishu_api.reply_message(token, msg["id"], "不支持处理当前消息类型")
        print(f"[{self.name}][unhandled] msg_type={mt} id={msg['id']}")

    def handle_message(self, token, msg):
        # 文件类消息（图片/文件/压缩包）先分流（这些消息没有 text 字段）
        if msg.get("kind") == "file":
            self.handle_upload(token, msg)
            return
        # 处理不了的类型：留痕 + 提示
        if msg.get("kind") == "unsupported":
            self.handle_unsupported(token, msg)
            return
        # 富文本 post：下载内嵌图片；有图则作为带附件的任务，纯文本则按文本继续
        if msg.get("kind") == "post":
            ptext, paths = self.handle_post_images(token, msg)
            if paths:
                full = (ptext or "请查看随附的图片").rstrip()
                full += "\n\n[随消息附带的图片，可直接读取]\n" + "\n".join(paths)
                self.run_task(token, msg, full, self.last_dir["name"])
                return
            msg = {**msg, "text": ptext}  # 纯文本 post，降级为普通文本处理
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # 等待序号时，数字消息用于选择
        if self.pending and text.isdigit():
            self.handle_select(token, msg, int(text) - 1)
            return
        # 命令分发（首词为命令，其余为参数）
        head, _, rest = text.partition(" ")
        handler = self.commands.get(head.lower())
        if handler:
            handler(token, msg, rest.strip())
            return
        # 普通任务
        self.run_task(token, msg, text, self.last_dir["name"])

    def run(self):
        print(f"[{self.name}] started, chat_id={self.chat_id}, "
              f"work_dirs={self.dir_names}, default={self.default_name}, "
              f"backoff 5s~{self.poll_interval}s")

        last_ts = str(int(time.time()))
        last_poll_time = time.time()
        interval = 5

        while True:
            try:
                now = time.time()
                # 距上次轮询超过 60s，说明电脑曾休眠，跳过积压消息
                if now - last_poll_time > 60:
                    last_ts = str(int(now))
                    print(f"[{self.name}][info] woke from sleep, resetting message timestamp")
                last_poll_time = now

                token = feishu_api.get_token(self.app_id, self.app_secret)
                messages = feishu_api.fetch_new_messages(token, self.chat_id, last_ts)
                interval = 5 if messages else min(interval * 4, self.poll_interval)

                for msg in messages:
                    last_ts = str(int(msg["create_time"]) // 1000 + 1)
                    self.handle_message(token, msg)
            except Exception as e:
                print(f"[{self.name}][error] {e}")

            time.sleep(5 if self.pending else interval)
