"""Bot：单个飞书机器人（一个 chat_id）的消息处理与会话编排。

每个 Bot 实例对应一个 chat_id，状态持久化在 MySQL。消息由长连接事件推来，经
on_event（跑在 ws 事件循环里、必须快速返回）按 message_id 去重后丢进 inbox 队列；
inbox 线程串行处理（命令回复/上传下载/把长任务入队），Codex 长任务再交给 worker 线程。
同一 chat_id 天然保序；不同 chat_id 各有自己的线程可并行，全局 Codex 并发由 run_slot 约束。
"""

import collections
import difflib
import os
import queue
import re
import subprocess
import threading
import time

import codex_runner
import db
import feishu_api
import uploads
import usage
import wiki_index
import wiki_sync
import tool_bridge
from input_conversion import InputConverter
from core.locking import index_lock
from core import debate as core_debate
from core import compact as core_compact
from core import context as core_context
from core import sessions as core_sessions
from core import model_settings
from core import uploads as core_uploads
from ops import snapshot as ops_snapshot

# context 占用阈值、压缩用的模型与提示词都在 core 里（见 core/__init__.py 说明那道墙）：
#   core_context.WARN / HIGH / HARD、core_compact.MODEL / PROMPT
# _context_guard 返回状态
GUARD_PROCEED, GUARD_BLOCKED = "proceed", "blocked"

# 附件回传协议：GPT 在回复里写 [[SEND_FILE:/abs/path]]，bot 剥离标记并把文件发到群。
# 这是**通用能力**、每轮对话都生效，不绑定任何命令。
# 目录白名单已按机主要求撤除（个人自用、单机部署），限制只剩「已存在的普通文件 + 体积 + 条数」，
# 见 Bot._extract_send_files 里的说明与代价。
SEND_FILE_RE = core_uploads.SEND_FILE_RE
SEND_FILE_MAX = core_uploads.MAX_FILES
SEND_FILE_MAX_BYTES = core_uploads.MAX_BYTES

# 上传文件自动进上下文：handle_upload 把落盘路径暂存，下一条任务消息自动附带（见 _drain_uploads）。
# 超过这个时长未被消费就丢弃——几小时前传的文件再塞进 prompt 只会误导。
UPLOAD_CONTEXT_TTL = 30 * 60
COMPACT_REQUEST_RE = re.compile(r"(?:请)?压缩(?:一下)?(?:当前)?(?:对话)?上下文[。！!]?|compact context", re.I)

HELP_TEXT = "\n".join([
    "可用命令：",
    "/sessions  — 列出对话，回复序号切换",
    "/new       — 新建对话（清空历史）",
    "/del <序号> — 删除指定对话（序号见 /sessions；接管的本机会话只解绑、不删盘）",
    "/name <名称> — 给当前对话改名（便于在 /sessions 里认出来）",
    "/allsessions [关键词] — 列出本机全部 Codex 会话（跨项目/跨群），回复序号即可接管续聊",
    "/permit    — 开关工作区写权限（Codex 沙箱内可运行命令）",
    "/unsafe    — 开关最高权限（跳过全部权限校验，可执行 bash 命令，谨慎）",
    "/usage     — 查看 ChatGPT / Codex 订阅用量",
    "/model [模型|default] — 查看或设置当前对话模型",
    "/effort [档位|default] — 查看或设置当前对话推理强度",
    "/help      — 显示此帮助",
    "",
    "其余能力一律用自然语言：发文件、重启服务、压缩上下文（已自动）、",
    "查知识库等，直接说就行。",
    "单次指定：这次用 gpt-6.1-sol，effort high，审查方案（不改变对话设置）。",
])

# 知识库检索注入：每轮取 top_k 个条目、总字数上限（越大越费 context，见 CLAUDE.md）
WIKI_TOP_K = 5
WIKI_MAX_CHARS = 3000


class Bot:
    """飞书消息驱动 Codex 的轮询服务，状态持久化在 MySQL。"""

    def __init__(self, bot_cfg, session_factory, task_timeout,
                 heartbeat_interval=60, run_slot=None, client=None,
                 auto_compact_threshold=75, wiki_cfg=None):
        self.name = bot_cfg["name"]
        self.app_id = bot_cfg["app_id"]
        self.app_secret = bot_cfg["app_secret"]
        self.chat_id = bot_cfg["chat_id"]
        self.input_converter = InputConverter(bot_cfg.get("input_zh_tw", True), log=self._log)
        # 一 chat 一 work_dir；省略时回退家目录，配置值支持 ~
        self.work_dir = os.path.expanduser(bot_cfg.get("work_dir") or "~")
        # 额外的只读参考资料目录（通过提示提供路径）：让 GPT 能读 work_dir 之外的代码/资料，
        # 但 cwd 仍是 work_dir，产物不会落进那些目录。启动时过滤掉不存在的路径。
        self.extra_dirs = [d for d in
                           (os.path.expanduser(x) for x in (bot_cfg.get("extra_dirs") or []))
                           if os.path.isdir(d)]
        # 发送/下载用的 SDK 客户端（同 app_id 的多个 Bot 共用一个；token 由其内部管理）
        self.client = client
        self.task_timeout = task_timeout
        self.heartbeat_interval = heartbeat_interval  # 长任务心跳间隔秒数，0=禁用
        # 全局并发闸（所有 bot 共享一个）：限制同时运行的 Codex 进程数；未传时不限流
        self.run_slot = run_slot or threading.BoundedSemaphore(1000)

        # 自动压缩阈值（context 占用百分比）：续聊前若达此值，先把本地历史摘要成一段
        # 上下文、开新对话再执行，避免每轮都重复烧满额度。<=0 关闭（改由 core_context.HARD 硬闸兜底）。
        self.auto_compact_threshold = auto_compact_threshold

        # 飞书知识库 RAG：配了 wiki.space_id 才启用。索引按 space_id 分区（不按 chat_id），
        # 多个群配同一知识库时共享一份索引，不重复建也不重复占内存。
        wcfg = bot_cfg.get("wiki") or {}
        self.wiki_space_id = str(wcfg.get("space_id") or "").strip() or None
        self.wiki_root_node = wcfg.get("root_node") or None
        self.wiki_top_k = int(wcfg.get("top_k") or WIKI_TOP_K)
        self.wiki_cfg = wiki_cfg or {}
        self.wiki_dir = (wiki_index.space_dir(self.wiki_cfg.get("cache_dir", "~/.feishu-bot/wiki"),
                                              self.wiki_space_id)
                         if self.wiki_space_id else None)
        self.wiki_index = None          # 加载好的索引；整体替换，读侧无锁
        self.wiki_status = "未同步"      # 最近一次同步结果（供日志/排查）
        self.wiki_lock = threading.Lock()  # 只护「同步+重建」这段，检索不加锁

        self.Session = session_factory

        # 恢复持久化状态（单一会话列表 + 单一权限开关）
        state = self.db(db.load_state, self.chat_id, strict=True) or {}
        # {"list": [{id, label, model, _row_id}, ...], "current": idx, "history": [...]}
        self.sessions = state.get("sessions") or core_sessions.empty()
        # 旧模型选择与 GPT 无关；保留历史审计里的实际模型，清掉会话选择。
        for entry in self.sessions["list"]:
            model = entry.get("model")
            if model in codex_runner.LEGACY_MODELS or (model and model.startswith("claude-")):
                entry["model"] = None
                if entry.get("_row_id"):
                    self.db(db.set_session_model, entry["_row_id"], None)
        self.permit_mode = state.get("permit", False)  # Codex workspace-write：工作区内读写/运行命令
        self.unsafe_mode = state.get("unsafe", False)  # 跳过全部权限校验

        self.pending = None   # "session" | "allsession"，等待用户回复序号的类型
        self.pending_all = []  # /allsessions 本次列出的本机会话（供回序号接管），随 pending 失效

        # 上传文件的「待附带」缓冲：用户传完图/文件后，下一条任务消息自动带上落盘路径。
        # inbox 写入/消费；工具线程可读取未消费的快照，用 upload_lock 保护缓冲。
        self.pending_uploads = []  # [{"path", "name", "size_human", "ts"}, ...]
        self.upload_lock = threading.RLock()  # inbox 写缓冲，工具线程只读快照

        # inbox：长连接事件先入此队列（on_event 快速返回），inbox 线程串行处理命令/上传/入队
        self.inbox = queue.Queue()
        # 幂等去重：长连接事件可能重复投递，记最近处理过的 message_id（有界 LRU）
        self._seen_ids = collections.OrderedDict()
        self._seen_max = 500
        # 异步执行：Codex 长任务丢后台 worker 串行跑，inbox 线程不阻塞；lock 护会话状态短临界区
        self.task_queue = queue.Queue()
        self.lock = threading.RLock()

        # 命令分发表：命令 -> 处理方法(msg, arg)
        self.commands = {
            "/sessions": self.cmd_sessions,
            "/help": self.cmd_help,
            "/new": self.cmd_new,
            "/unsafe": self.cmd_unsafe,
            "/usage": self.cmd_usage,
            "/del": self.cmd_del,
            "/allsessions": self.cmd_allsessions,
            "/name": self.cmd_name,
            "/permit": self.cmd_permit,
            "/model": self.cmd_model,
            "/effort": self.cmd_effort,
        }

    # ------------------------------------------------------------------ DB

    def db(self, fn, *a, strict=False, **kw):
        """开一个短生命周期 Session 执行 DB 操作（每次独立、线程安全），失败不致命。"""
        with self.Session() as s:
            try:
                r = fn(s, *a, **kw)
                s.commit()
                return r
            except Exception as e:
                s.rollback()
                print(f"[{self.name}][db-error] {e}")
                if strict:
                    raise
                return None

    def save_bot_state(self):
        self.db(db.save_bot_state, self.chat_id, self.permit_mode, self.unsafe_mode, strict=True)

    def _log(self, text):
        """给 core 能力层使用的日志回调，带机器人名前缀。"""
        print(f"[{self.name}]{text}")

    # -------------------------------------------------------------- session

    # 下面几个是 core_sessions 的薄包装：那边是纯函数（不知道 self.work_dir、不碰 DB），
    # 这边补上本 bot 的上下文。保留包装而不让调用方直连 core，是因为 bot.py 内部到处在用
    # self.entry_cwd()/self.current_entry()，改签名的收益远小于风险。

    def current_session_id(self):
        return core_sessions.current_sid(self.sessions)

    def current_entry(self):
        return core_sessions.current(self.sessions)

    def entry_cwd(self, entry):
        """某个会话条目实际该在哪个目录下跑 Codex（接管来的会话用它自己的原项目目录）。"""
        return core_sessions.entry_cwd(entry, self.work_dir)

    def current_cwd(self):
        return self.entry_cwd(self.current_entry())

    def make_session(self, label=None, claude_sid=None, cwd=None, external=False, seed=None):
        """创建内存 session 条目并在 DB 中建行，返回条目（含 _row_id）。
        claude_sid/cwd/external 只在「接管本机已有会话」时给。"""
        entry = core_sessions.new_entry(label, claude_sid=claude_sid, cwd=cwd, external=external)
        position = len(self.sessions["list"])
        entry["_row_id"] = self.db(db.insert_session, self.chat_id, entry["label"], position,
                                   claude_sid=claude_sid, cwd=cwd, external=external, seed=seed, strict=True)
        if seed:
            entry["seed"] = seed
            entry["_summary"] = seed
        return entry

    def set_current(self, entry):
        if entry.get("_row_id"):
            self.db(db.set_current, self.chat_id, entry["_row_id"])

    def load_history(self, claude_sid):
        return self.db(db.load_history, self.chat_id, claude_sid) or []

    def _send_recent_history(self, msg_id, rounds=3):
        """切换对话后，把最近 rounds 轮（user+assistant 各算一条）逐条单独发回，
        每条带时间戳和发送者，方便不用翻聊天记录就能看到上下文。"""
        recent = self.sessions["history"][-rounds * 2:]
        for turn in recent:
            who = "你" if turn["role"] == "user" else "GPT"
            created_at = turn.get("created_at")
            ts = created_at.strftime("%m-%d %H:%M") if created_at else ""
            header = f"[{ts}] {who}：" if ts else f"{who}："
            feishu_api.reply_message(self.client, msg_id, f"{header}\n{turn.get('content') or ''}", tag="history")

    def update_session(self, session_id, first_task=None, entry=None):
        """首次执行时写入 session_id 和标签，并同步到 DB。
        在 worker 线程于长调用之后运行，加锁与主循环的会话增删改互斥。"""
        with self.lock:
            data = self.sessions
            if not data["list"]:
                return
            entry = entry if entry is not None else self.current_entry()
            entry["id"] = session_id
            new_label = None
            if first_task and entry["label"].startswith("新对话"):
                ts = time.strftime("%m-%d %H:%M")
                entry["label"] = f"{ts} {first_task[:20]}{'…' if len(first_task) > 20 else ''}"
                new_label = entry["label"]
            if entry.get("_row_id"):
                self.db(db.update_session, entry["_row_id"], claude_sid=session_id, label=new_label)

    def audit(self, user_text, result, new_session_id, used_model=None, entry=None, effort=None):
        """把一轮对话（用户输入 + LLM 输出）写入审计表。"""
        entry = entry if entry is not None else self.current_entry()
        row_id = entry.get("_row_id") if entry else None
        sid = (entry.get("id") if entry else None) or new_session_id
        self.db(db.append_message, row_id, self.chat_id, sid, "user", user_text)
        self.db(db.append_message, row_id, self.chat_id, sid, "assistant", result,
                result.startswith("[error]"), "超时" in result, model=used_model, effort=effort)

    def execute_codex(self, text, session_id, history=None, first_task=None, run_text=None,
                       extra_system=None, tool_env=None, entry=None, permissions=None,
                       settings=None, execution_info=None):
        """调用 Codex，处理超时/异常，成功时同步 session_id 到 DB。
        run_text 指定实际发给 Codex 的 prompt（默认同 text）：压缩后首轮会在 text 前
        拼上摘要，但审计/历史/标签仍用用户原文 text。extra_system 追加一段系统提示
        （注入了知识库资料时带上回答策略）。返回 (result, new_sid, used_model)。"""
        permit, unsafe = permissions or (self.permit_mode, self.unsafe_mode)
        # 始终把该聊天上传目录纳入工作区（存在才加），让 GPT 能读上传文件
        up = uploads.chat_dir(self.chat_id)
        extra_dirs = ([up] if os.path.isdir(up) else []) + self.extra_dirs
        entry = entry if entry is not None else self.current_entry()
        with self.lock:
            choice = dict(entry or {})
        # 接管来的本机会话在它自己的原项目目录下跑；此时把本 bot 的 work_dir 也挂进去，
        # 免得「在飞书里续聊」反而读不到这个群平时的工作区。
        cwd = self.entry_cwd(entry)
        if cwd != self.work_dir and os.path.isdir(self.work_dir):
            extra_dirs = extra_dirs + [self.work_dir]
        try:
            settings = settings or self.task_settings(choice)
            result, new_sid, used_model = codex_runner.run_codex(
                run_text or text, cwd, self.task_timeout,
                session_id, permit=permit, extra_dirs=extra_dirs, history=history,
                model=settings['model'], effort=settings['effort'], execution_info=execution_info,
                unsafe=unsafe, extra_system=extra_system, tool_env=tool_env,
            )
            if new_sid and not result.startswith("[error]"):
                self.update_session(new_sid, first_task=first_task, entry=entry)
        except subprocess.TimeoutExpired:
            result, new_sid, used_model = f"[error] 任务超时（>{self.task_timeout}s）", None, None
        except Exception as e:
            result, new_sid, used_model = f"[error] 执行失败: {e}", None, None
        return result, new_sid, used_model

    # ------------------------------------------------------------- commands

    def task_settings(self, entry, overrides=None):
        cwd = self.entry_cwd(entry)
        data = model_settings.catalog(cwd, for_task=True)
        try:
            return model_settings.resolve(entry, data, overrides)
        except ValueError:
            # 用户显式选择了旧目录里没有的新模型/档位时，查询一次最新目录。
            # 仍按原选择校验，不静默换模型或降低 effort。
            if not overrides:
                raise
            return model_settings.resolve(entry, model_settings.catalog(cwd, refresh=True), overrides)

    def audit_preflight_failure(self, msg_id, stage, error):
        detail = f'{type(error).__name__}: {error}'[:2000]
        self._log(f'[preflight-error] stage={stage} message={msg_id} {detail}')
        try:
            self.db(db.record_audit, self.chat_id, 'preflight', stage,
                    message_id=msg_id, ok=False, detail=detail, strict=True)
        except Exception as e:
            self._log(f'[preflight-audit-error] {type(e).__name__}: {e}')

    def cmd_model(self, msg, arg):
        self._cmd_settings(msg, arg, 'model')

    def cmd_effort(self, msg, arg):
        self._cmd_settings(msg, arg, 'effort')

    def _cmd_settings(self, msg, arg, field):
        self.pending = None
        try:
            if arg and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', arg):
                raise ValueError(f'用法：/{field} <名称或 default>')
            with self.lock:
                entry = self.current_entry()
                choice = dict(entry or {})
                cwd = self.entry_cwd(entry)
            data = model_settings.catalog(cwd, refresh=bool(arg))
            if arg:
                choice[field] = None if arg.lower() == 'default' else arg.lower()
            try:
                settings = model_settings.resolve(choice, data)
            except ValueError as e:
                if arg:
                    raise
                # 已保存的模型可能从账户目录中消失；仍允许查看目录并修正选择。
                lines = [f'当前设置需要调整：{e}',
                         f"已保存模型：{choice.get('model') or 'default'}",
                         f"已保存 effort：{choice.get('effort') or 'default'}", '可选模型：']
                lines.extend(m['model'] for m in data['models'])
                feishu_api.reply_message(self.client, msg['id'], '\n'.join(lines))
                return
            if arg:
                with self.lock:
                    if entry is None:
                        entry = self.make_session()
                        core_sessions.append_and_focus(self.sessions, entry)
                        self.set_current(entry)
                    # DB 提交成功后才更新内存；失败时保留原设置。
                    self.db(db.set_session_settings, entry['_row_id'], choice.get('model'),
                            choice.get('effort'), strict=True)
                    entry['model'], entry['effort'] = choice.get('model'), choice.get('effort')
                lines = ['已保存当前对话设置；仅影响此后提交的任务。']
            else:
                lines = ['当前对话设置：']
            lines += [f"模型：{settings['model']}（{settings['model_source']}）",
                      f"effort：{settings['effort']}（{settings['effort_source']}）"]
            if field == 'model':
                lines.append('可选模型：')
                lines.extend(m['model'] for m in data['models'])
            else:
                selected = next(m for m in data['models'] if m['model'] == settings['model'])
                lines.append('可选 effort：' + ' / '.join(selected['efforts']))
            if data.get('_warning'):
                updated = time.strftime('%m-%d %H:%M', time.localtime(data['_cached_at']))
                lines.append(f"{data['_warning']}（{updated} 更新）。")
            lines.append(f'/{field} default 恢复默认；/new 新建对话沿用默认设置。')
            reply = '\n'.join(lines)
        except Exception as e:
            self.audit_preflight_failure(msg['id'], f'{field}-command', e)
            reply = f'[error] 设置未更改：{e}'
        feishu_api.reply_message(self.client, msg['id'], reply)

    def cmd_sessions(self, msg, arg):
        data = self.sessions
        if len(data["list"]) <= 1:
            feishu_api.reply_message(self.client, msg["id"], "当前只有 1 个对话，发送 /new 创建新对话")
        else:
            self.pending = "session"
            feishu_api.reply_message(self.client, msg["id"], feishu_api.build_sessions_prompt(data["list"], data["current"]))

    def cmd_allsessions(self, msg, arg):
        """列出本机全部 Codex 会话（扫 ~/.codex/sessions，跨项目/跨群/跨 bot），
        回复序号即可**接管续聊**：在本群建一个绑定该 session_id + 该项目 cwd 的会话条目，
        之后每轮 codex exec resume 都在原目录下跑，等于把终端里的那段对话搬到飞书接着聊。

        arg 为可选关键词，对标题与项目路径做子串过滤（列表只显示 30 条，靠它捞更早的会话）。"""
        sessions = codex_runner.list_all_sessions()
        kw = (arg or "").strip().lower()
        sessions = core_sessions.filter_local(sessions, arg)
        if not sessions:
            hint = f"没有匹配「{arg}」的会话" if kw else "本机暂无 Codex 会话记录"
            feishu_api.reply_message(self.client, msg["id"], hint)
            return
        limit = 30
        shown = sessions[:limit]
        now = time.time()
        head = f"本机共 {len(sessions)} 个 Codex 会话"
        if kw:
            head += f"（匹配「{arg}」）"
        head += f"，按最近使用倒序，最多显示 {limit} 条："
        lines = [head]
        for i, s in enumerate(shown, 1):
            age = self._human_elapsed(max(0, int(now - s["mtime"]))) + "前"
            title = s["title"] or "(无标题)"
            mark = "" if s["cwd"] else "  ⚠️无法接管(未记录工作目录)"
            lines.append(f"{i}. [{age}] {title}{mark}")
            lines.append(f"   项目：{s['project']}")
            lines.append(f"   session：{s['session_id']}")
        lines.append("")
        lines.append("回复序号即可接管该会话并在本群继续（不影响本机原会话文件，续聊会写进同一份）")
        self.pending = "allsession"
        self.pending_all = shown
        feishu_api.reply_message(self.client, msg["id"], "\n".join(lines))
        print(f"[{self.name}][allsessions] total={len(sessions)} shown={len(shown)} kw={kw!r}")

    def _attach_session(self, msg, s):
        """把一个本机会话接管进本群：新建绑定 session_id + 原项目 cwd 的会话条目并切过去。
        本机 jsonl 里的历史 bot 的 DB 没有，故顺带回显最近几轮，方便接上话头。"""
        cwd, sid = s.get("cwd"), s["session_id"]
        error = core_sessions.attach_precheck(s)
        if error:
            feishu_api.reply_message(self.client, msg["id"], error)
            return

        with self.lock:  # 与 worker 的 update_session 互斥
            data = self.sessions
            existing = core_sessions.find_by_sid(data, sid)
            reattached = existing is not None
            if reattached:  # 已接管过：直接切过去，别撞 sessions 的 (chat_id, sid) 唯一索引
                data["current"] = existing
                entry = data["list"][existing]
            else:
                label = (s["title"] or os.path.basename(cwd.rstrip("/")) or "本机会话")[:40]
                entry = self.make_session(label=f"[本机] {label}", claude_sid=sid,
                                          cwd=cwd, external=True)
                core_sessions.append_and_focus(data, entry, clear_history=False)
            self.set_current(entry)
            data["history"] = (list(entry["_history"]) if "_history" in entry else self.load_history(sid))
        self.pending = None
        self.pending_all = []

        note = "已切回先前接管的会话" if reattached else "已接管本机会话"
        lines = [f"✅ {note}：{entry['label']}", f"工作目录：{cwd}", f"session：{sid}"]
        # 先把 context 占用摆出来：终端里聊了很久的会话往往一上来就接近上限，
        # 不说清楚的话，用户发第一条消息就撞上护栏会一头雾水。
        pct = self._context_pct(sid)
        if pct is not None:
            lines.append(f"当前 context 占用 {pct:.0f}%"
                         + ("（较高，续聊可能先触发自动压缩）" if pct >= self.auto_compact_threshold > 0
                            else ""))
        lines.append("后续消息将在该目录下续聊；/sessions 可切回本群自己的对话。")
        feishu_api.reply_message(self.client, msg["id"], "\n".join(lines))
        # 回显最近几轮，接上话头（这段历史只在本机 jsonl 里，DB 查不到）
        tail = []
        try:
            tail = codex_runner.session_tail(sid, cwd)
        except Exception as e:
            print(f"[{self.name}][attach-tail-error] {e}")
        for turn in tail:
            who = "你" if turn["role"] == "user" else "GPT"
            feishu_api.reply_message(self.client, msg["id"],
                                     f"{who}：\n{turn['content']}", tag="history")
        print(f"[{self.name}][attach] sid={sid[:8]}… cwd={cwd}")

    def cmd_help(self, msg, arg):
        feishu_api.reply_message(self.client, msg["id"], HELP_TEXT)

    def cmd_name(self, msg, arg):
        """给当前对话改名——/sessions 列表里全是「新对话 09-29 16:40」时根本认不出哪个是哪个。"""
        if not arg:
            feishu_api.reply_message(self.client, msg["id"], "用法：/name <名称>")
            return
        if len(arg) > 255:
            feishu_api.reply_message(self.client, msg["id"], "名称最多 255 个字符")
            return
        data = self.sessions
        if data["list"]:
            entry = data["list"][data["current"]]
            if entry.get("_row_id"):
                self.db(db.update_session, entry["_row_id"], label=arg, strict=True)
            entry["label"] = arg
            feishu_api.reply_message(self.client, msg["id"], f"当前对话已命名为「{arg}」")
        else:
            feishu_api.reply_message(self.client, msg["id"], "当前没有活跃的对话")

    def cmd_new(self, msg, arg):
        with self.lock:  # 与 worker 的 update_session 互斥，防会话列表被并发改乱
            data = self.sessions
            entry = self.make_session()
            core_sessions.append_and_focus(data, entry)
            self.set_current(entry)
            total = len(data["list"])
        self.pending = None
        feishu_api.reply_message(self.client, msg["id"], f"已创建新对话（共 {total} 个），发送任务即可开始")
        print(f"[{self.name}][new-session] total={total}")

    def cmd_permit(self, msg, arg):
        with self.lock:
            previous = self.permit_mode, self.unsafe_mode
            self.permit_mode = not self.permit_mode
            on = self.permit_mode
            # 与 unsafe 互斥：开 permit 则关掉 unsafe
            cleared = on and self.unsafe_mode
            if on:
                self.unsafe_mode = False
            try:
                self.save_bot_state()
            except Exception:
                self.permit_mode, self.unsafe_mode = previous
                raise
        status = "已开启（可在工作区读写文件、在 Codex 沙箱内运行命令）" if on else "已关闭"
        note = "（已自动关闭最高权限 unsafe）" if cleared else ""
        feishu_api.reply_message(self.client, msg["id"], f"权限模式 {status}{note}")
        print(f"[{self.name}][permit]={on}")

    def cmd_unsafe(self, msg, arg):
        with self.lock:
            previous = self.permit_mode, self.unsafe_mode
            self.unsafe_mode = not self.unsafe_mode
            on = self.unsafe_mode
            # 与 permit 互斥：开 unsafe 则关掉 permit
            cleared = on and self.permit_mode
            if on:
                self.permit_mode = False
            try:
                self.save_bot_state()
            except Exception:
                self.permit_mode, self.unsafe_mode = previous
                raise
        status = ("已开启（跳过全部权限校验，可执行任意命令含 bash，请谨慎）"
                  if on else "已关闭")
        note = "（已自动关闭文件读写 permit）" if cleared else ""
        feishu_api.reply_message(self.client, msg["id"], f"最高权限 {status}{note}")
        print(f"[{self.name}][unsafe]={on}")

    def cmd_usage(self, msg, arg):
        """查看 ChatGPT 订阅用量（官方 Codex app-server，按需+缓存）。"""
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
            if data["list"][didx].get("_jobs", 0):
                feishu_api.reply_message(self.client, msg["id"], "该对话仍有任务正在执行或排队，完成后再删除")
                return
            removed = core_sessions.remove_at(data, didx)
            # 删磁盘上的 Codex session 文件。接管来的本机会话（external）只解绑不删盘——
            # 那是用户自己在终端开的对话，飞书里删一下就毁掉他的现场，代价不对等。
            if removed["id"] and not removed.get("external"):
                try:
                    codex_runner.delete_codex_session(removed["id"], self.entry_cwd(removed))
                except Exception as e:
                    print(f"[del] remove Codex session file failed: {e}")
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
                cur_entry = data["list"][data["current"]]
                self.set_current(cur_entry)
                data["history"] = (list(cur_entry["_history"]) if "_history" in cur_entry
                                   else self.load_history(cur_entry["id"]))
            remaining = len(data["list"])
        self.pending = None
        note = "（接管的本机会话，只解绑、磁盘上的会话文件保留）" if removed.get("external") else ""
        feishu_api.reply_message(self.client, msg["id"],
                                 f"已删除对话「{removed['label']}」（剩 {remaining} 个）{note}")
        print(f"[{self.name}][del] removed idx={didx} row={removed.get('_row_id')} "
              f"external={bool(removed.get('external'))}")

    @staticmethod
    def _model_suffix(used_model, ctx_pct=None, effort=None):
        """成功结果末尾追加模型、实际 effort、上下文与缓存用量。
        用量取自 usage 模块的缓存（只读、不阻塞），取不到时静默省略。"""
        parts = []
        if used_model:
            short = used_model
            parts.append(f"模型：{short}")
        if effort:
            parts.append(f'effort：{effort}')
        if ctx_pct is not None:
            parts.append(f"ctx {ctx_pct:.0f}%")
        u = usage.report_inline()
        if u:
            parts.append(u)
        return f"\n\n（{' · '.join(parts)}）" if parts else ""

    def _context_pct(self, session_id, entry=None):
        """读盘取某 session 当前 context 占用百分比（只读、不阻塞）；取不到返回 None。"""
        return core_context.pct(session_id, self.entry_cwd(entry) if entry is not None else self.current_cwd(), log=self._log)

    @staticmethod
    def _context_warning(pct):
        """按 context 占用给分级提醒文案（附在回复末尾）；未越阈值返回空串。"""
        return core_context.warning(pct)

    def _context_guard(self, msg_id, session_id, entry=None, job=None):
        """续聊前的 context 护栏：达自动压缩阈值则压缩重置；未开压缩且触顶则硬闸拦下。
        返回 GUARD_PROCEED / GUARD_BLOCKED，或压缩后的会话条目。"""
        pct = self._context_pct(session_id, entry=entry)
        if pct is None:
            return GUARD_PROCEED
        if self.auto_compact_threshold > 0 and pct >= self.auto_compact_threshold:
            compacted = self._do_compact(msg_id, reason=f"context {pct:.0f}%", entry=entry, job=job)
            if compacted:
                return compacted
            # 压缩失败：跌落到硬闸判断（不因失败而卡死续聊）
        if pct >= core_context.HARD:
            self._push_reply(
                msg_id,
                f"🛑 当前对话 context 已达 {pct:.0f}%，为避免持续消耗额度已暂停执行。\n"
                "请发 /new 开新对话后重试（或直接说「压缩一下上下文」）。")
            return GUARD_BLOCKED
        return GUARD_PROCEED

    def _do_compact(self, msg_id, reason=None, entry=None, job=None):
        """在原对话保存摘要并重置执行上下文，保留列表条目和 DB 行。
        下一条任务自动带上 seed；成功返回原条目，无历史或摘要失败返回 False。"""
        with self.lock:
            entry = entry if entry is not None else self.current_entry()
            history = self._entry_history(entry)
            previous_summary = (entry or {}).get("seed") or (entry or {}).get("_summary")
        if not history and entry and entry.get("id"):
            # 接管来的本机会话在 messages 表里没有任何历史（那些对话发生在终端里），
            # 本地历史为空就直接 return False 的话，护栏一触发就是死路：压缩压不动，
            # 而这类会话恰恰最容易一上来就顶到阈值。
            # 退而求其次，从磁盘 session 文件读最近若干轮当摘要素材。
            history = core_compact.history_from_disk(entry["id"], self.entry_cwd(entry),
                                                     log=self._log)
        if previous_summary:
            history = [{"role": "assistant",
                        "content": f"[上一段对话的压缩摘要]\n{previous_summary}"}] + history
        if not history:
            return False
        note = f"（{reason}）" if reason else ""
        self._push_reply(msg_id, f"🗜 正在压缩当前对话上下文{note}…", tag="compact")
        with self.run_slot:  # 摘要也是一次 Codex 调用，纳入全局并发闸
            summary = core_compact.summarize(history, self.entry_cwd(entry), self.task_timeout,
                                             log=self._log)
        if not summary:
            self._push_reply(msg_id, "上下文自动压缩失败，可手动 /new 开新对话", tag="compact")
            return False
        with self.lock:  # 先持久化，失败时不改变内存里的原会话
            self.db(db.compact_session, entry["_row_id"], summary, strict=True)
            entry.update(id=None, seed=summary, _summary=summary, _history=[], external=False)
            # 当前任务和排队任务仍引用同一条目，无需转移；_jobs 继续保护删除。
            # 压缩期间用户已切换会话时，不改变其选择和历史。
            if self.current_entry() is entry:
                self.sessions["history"] = []
        self._push_reply(msg_id, "✅ 已压缩当前对话，后续在压缩摘要基础上继续。", tag="compact")
        print(f"[{self.name}][compact] done reason={reason} summary_chars={len(summary)}")
        return entry

    def _entry_history(self, entry):
        """锁内读取指定会话历史，不受用户切换会话影响。"""
        if entry is None:
            return []
        if "_history" not in entry:
            entry["_history"] = (list(self.sessions.get("history", []))
                                 if self.current_entry() is entry
                                 else self.load_history(entry.get("id")))
        return list(entry["_history"])

    def _permit_banner(self):
        """若开着 permit/unsafe，返回提醒文案（防止开启后忘关）。互斥后至多一条。"""
        if self.unsafe_mode:
            return "⚠️ 最高权限(unsafe)开启中，可执行任意命令 — 用完发 /unsafe 关闭"
        if self.permit_mode:
            return "🔓 工作区写权限及沙箱内命令(permit)开启中 — 用完发 /permit 关闭"
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

    # --------------------------------------------------------------- 知识库（RAG）

    def _wiki_refresh(self, log=None, verbose=False):
        """增量同步知识库 + 重建索引 + 换上新索引。返回 (ok, 描述文本)。

        整个过程在 wiki_lock 内串行（同一 bot 不并发重建）；检索侧读的是 self.wiki_index
        引用，这里构建完才整体替换，故检索不需要等锁、也不会读到半截索引。"""
        log = log or (lambda s: print(f"[{self.name}][wiki] {s}"))
        quiet = (lambda s: None)
        with self.wiki_lock, index_lock(self.wiki_dir):
            try:
                stats = wiki_sync.sync_space(self.client, self.wiki_space_id,
                                             self.wiki_dir, self.wiki_root_node, log=log)
                # 无变更时不透传 log：定时同步每 30 分钟跑一次，实测 18 天里 866 轮
                # 有 864 轮什么都没变，无条件打印会把 journal 刷成 40% 的空转播报。
                changed = bool(stats["added"] + stats["updated"] + stats["removed"] + stats["failed"])
                wiki_index.build(self.wiki_dir, self.wiki_cfg,
                                 session_factory=self.Session, log=log if changed else quiet)
                self.wiki_index = wiki_index.load(self.wiki_dir)
            except Exception as e:
                self.wiki_status = f"同步失败（{time.strftime('%m-%d %H:%M')}）：{e}"
                print(f"[{self.name}][wiki-error] {e}")
                return False, str(e)
        n = len(self.wiki_index) if self.wiki_index else 0
        desc = (f"新增 {stats['added']} / 更新 {stats['updated']} / 删除 {stats['removed']} / "
                f"未变 {stats['unchanged']}" + (f" / 失败 {stats['failed']}" if stats["failed"] else ""))
        # wiki_status 始终更新（状态随时可查），只有日志静默——静默不丢信息
        self.wiki_status = f"{time.strftime('%m-%d %H:%M')} 同步完成，{desc}，共 {n} 个条目"
        if changed or verbose:
            print(f"[{self.name}][wiki] {desc}, chunks={n}")
        return True, desc

    def _wiki_worker(self):
        """后台定时增量同步。不占 run_slot——那是 Codex 进程的并发闸，
        这里只是网络 IO + ollama 编码，不该挤占任务名额。"""
        interval = max(int(self.wiki_cfg.get("sync_interval", 1800)), 60)
        while True:
            self._wiki_refresh()
            time.sleep(interval)

    def _wiki_reference(self, question, strict=False):
        """检索知识库，返回可直接拼进 prompt 的「参考资料」块（无命中返回空串）。
        任何失败都只记日志、返回空串——知识库挂了不该让整轮任务失败。"""
        idx = self.wiki_index
        if idx is None or not len(idx):
            return ""
        try:
            hits = wiki_index.search(idx, question, self.wiki_cfg,
                                     top_k=self.wiki_top_k, session_factory=self.Session)
        except Exception as e:
            print(f"[{self.name}][wiki-search-error] {e}")
            if strict:
                raise RuntimeError(f"知识库检索失败：{e}") from e
            return ""
        if not hits:
            print(f"[{self.name}][wiki] 无命中: {question[:40]}")
            return ""
        print(f"[{self.name}][wiki] 命中 {len(hits)} 条: "
              + " | ".join(h["breadcrumb"][-40:] for h in hits[:3]))
        return wiki_index.format_reference(hits, max_chars=WIKI_MAX_CHARS)

    def run_task(self, msg, text):
        """主线程只做入队 + 立即回执，真正执行在后台 worker，故长任务不冻结 bot。"""
        self.pending = None
        # 先解析本次设置，验证通过后由 _enqueue 消费附件并附到任务与审计正文。
        try:
            task_text, overrides = model_settings.parse_once(text)
        except ValueError as e:
            self.audit_preflight_failure(msg['id'], 'settings-parse', e)
            feishu_api.reply_message(self.client, msg['id'], f'[error] {e}')
            return
        self._enqueue({"kind": "task", "text": text, "task_text": task_text,
                       "overrides": overrides, "attach_uploads": True, "msg_id": msg["id"],
                       "sender_id": msg.get("sender_id")},
                      msg["id"], verb="执行")

    def _enqueue(self, job, msg_id, verb):
        """把任务丢后台队列，立即回执（不阻塞主循环）。"""
        with self.lock:
            entry = self.current_entry()
            if entry is None:
                entry = self.make_session()
                core_sessions.append_and_focus(self.sessions, entry)
                self.set_current(entry)
            self._entry_history(entry)
            job["entry"] = entry
            choice = dict(entry)
        try:
            task_text = job.get('task_text', job['text'])
            if not COMPACT_REQUEST_RE.fullmatch(task_text):
                job['execution_settings'] = self.task_settings(choice, job.get('overrides'))
        except Exception as e:
            self.audit_preflight_failure(msg_id, 'model-settings', e)
            feishu_api.reply_message(self.client, msg_id, f'[error] 任务未提交，设置无效或查询失败：{e}')
            return
        if job.get('attach_uploads') and not COMPACT_REQUEST_RE.fullmatch(task_text):
            # 正文与自动附带的路径/文件元信息分开，后者不经过语言转换。
            job['user_task_text'] = task_text
            attach = self._drain_uploads()
            if attach:
                block = '\n' + self._attach_block(attach)
                job['upload_context'] = block
                job['text'] = job['text'].rstrip() + block
                job['task_text'] = task_text.rstrip() + block
        with self.lock:
            entry["_jobs"] = entry.get("_jobs", 0) + 1
            ahead = self.task_queue.qsize()
            self.task_queue.put(job)
        lines = [f"✅ 已接收，任务进入后台{verb}，完成后把结果发给你"]
        if ahead > 0:
            lines.append(f"（前面还有 {ahead} 个任务在排队）")
        settings = job.get('execution_settings')
        if settings:
            lines.append(f"本次：{settings['model']} · effort {settings['effort']}")
        banner = self._permit_banner()
        if banner:
            lines.append(banner)
        feishu_api.reply_message(self.client, msg_id, "\n".join(lines))
        print(f"[{self.name}][enqueue] {verb} qsize={ahead + 1} | {job['text'][:60]}")

    def _worker(self):
        """后台执行 Codex 任务：同一 chat_id 由本 worker 单线程串行处理、保序；
        不同 chat_id 各有自己的 worker 可并行——真正的同时并发数由全局 run_slot 上限约束。"""
        while True:
            job = self.task_queue.get()
            try:
                self._execute_job(job)
            except Exception as e:
                self._log(f"[worker-error] {type(e).__name__}: {e}")
                # 编排层也可能在调用模型之前失败，不能只记日志让用户一直等。
                try:
                    self.db(db.record_audit, self.chat_id, "llm", "worker-error",
                            message_id=job["msg_id"], ok=False,
                            detail=f"{type(e).__name__}: {e}"[:2000])
                except Exception as audit_error:
                    self._log(f"[worker-audit-error] {audit_error}")
                try:
                    self._push_reply(job["msg_id"],
                                     "[error] 任务执行中断，请稍后重试。"
                                     f"\n原因：{type(e).__name__}: {e}", tag="worker-error")
                except Exception as report_error:
                    self._log(f"[worker-report-error] {report_error}")
            finally:
                with self.lock:
                    entry = job.get("entry")
                    if entry is not None:
                        entry["_jobs"] = max(0, entry.get("_jobs", 0) - 1)
                self.task_queue.task_done()

    def _execute_job(self, job):
        text, msg_id = job["text"], job["msg_id"]
        task_text = job.get('task_text', text)

        # 锁内：准备会话状态、快照 history（短临界区，不含长调用）
        with self.lock:
            entry = job.get("entry")
            if entry is None:
                entry = self.current_entry()
                if entry is None:
                    entry = self.make_session()
                    core_sessions.append_and_focus(self.sessions, entry)
                    self.set_current(entry)
                job["entry"] = entry
            session_id = entry.get("id")
            is_new = session_id is None
            history = self._entry_history(entry)
            first_task = text if is_new else None
            seed = entry.get("seed")

        # 硬闸触发后仍允许直接压缩，避免用户无法通过自然语言脱困。
        if COMPACT_REQUEST_RE.fullmatch(task_text):
            if not self._do_compact(msg_id, reason="用户请求", entry=entry, job=job):
                self._push_reply(msg_id, "暂无可压缩的历史，或摘要失败；可发 /new 开新对话。", tag="compact")
            return

        # 档1/档2：续聊前按 context 占用护栏——达阈值自动压缩，未开压缩且触顶则硬闸拦下
        if session_id and not is_new:
            status = self._context_guard(msg_id, session_id, entry=entry, job=job)
            if status == GUARD_BLOCKED:
                return
            if isinstance(status, dict):  # 压缩后的目标，用户可能已切到其他会话
                with self.lock:
                    session_id, history, first_task, is_new = None, [], text, True
                    entry = job["entry"] = status
                    seed = entry.get("seed")

        # 压缩后首轮：把摘要拼进实际 prompt（审计/历史仍存用户原文 text）
        debate_required = core_debate.required(task_text)
        run_text = (self.input_converter.convert(job.get('user_task_text', task_text))
                    + job.get('upload_context', ''))
        if seed:
            run_text = (f"[以下是上一段对话的压缩摘要，请据此延续上下文，无需复述]\n{seed}\n\n"
                        f"[用户新消息]\n{run_text}")
        if debate_required:
            run_text = ("[本轮要求真实的独立辩论：调用 feishu_bot 的 MCP debate 工具，各方五轮逐轮推送。"
                        "工具失败则说明失败；不可自己扮演双方。]\n" + run_text)
        # 知识库 RAG：检索出的条目拼在用户消息之前。只改 run_text，text 原样进 history
        # 与审计——否则历史会被资料撑爆，压缩摘要也会跟着跑偏。
        reference = self._wiki_reference(task_text) if self.wiki_space_id and not tool_bridge._SERVER else ""
        if reference:
            run_text = f"{reference}\n\n[用户新消息]\n{run_text}"
        # 群里可能多人共用一个 bot，把发言人 open_id 交给 GPT，便于按人分账/存档
        if job.get("sender_id"):
            run_text = f"[飞书用户: {job['sender_id']}]\n{run_text}"

        permit_mode, unsafe_mode = self.permit_mode, self.unsafe_mode
        print(f"[{self.name}][task] permit={permit_mode} "
              f"unsafe={unsafe_mode} session={'new' if is_new else str(session_id)[:8] + '…'} | {text[:60]}")

        # 锁外：长耗时执行 + 心跳（此期间主循环命令不被阻塞）
        # 全局并发闸：不同 chat_id 可并行，但同时运行的 Codex 进程数受 run_slot 上限约束；
        # 心跳在闸外启动，等待空位期间也会回「仍在处理中」。elapsed 只计实际执行、不含排队等待。
        # 回滚基线（方案 b）：在 Codex 动手**之前**拍一份主程序目录的快照，本轮 LLM 成功后
        # 才把它转正为 known-good。必须是「动手前」——写坏配置的那一轮它自己往往是成功的
        # （正是它写坏的），拿完成时刻的状态当基线会把污染态存成 known-good。详见 ops/snapshot.py。
        snap_id = ops_snapshot.take_quiet()

        stop = self._start_heartbeat(msg_id)
        execution_info = {}
        try:
            with self.run_slot:
                with self.lock:
                    permissions = self.permit_mode, self.unsafe_mode
                with tool_bridge.bind(self, msg_id, entry=entry, unsafe=permissions[1]) as tools:
                    t0 = time.time()
                    result, new_sid, used_model = self.execute_codex(
                        text, session_id, history=history, first_task=first_task,
                        run_text=run_text,
                        extra_system=codex_runner.WIKI_SYSTEM_PROMPT if reference else None,
                        tool_env=tools.env if tool_bridge._SERVER else None,
                        entry=entry, permissions=permissions,
                        settings=job.get('execution_settings'), execution_info=execution_info,
                    )
                    elapsed = time.time() - t0
        finally:
            stop.set()

        # LLM 响应审计：耗时 / 模型 / 是否出错 / 输出规模 / 新 session（同时落库）
        debate = getattr(tools, 'debate', None)
        if ((isinstance(debate, dict) and not debate.get('completed'))
                or (debate_required and not result.startswith('[error]')
                    and not (isinstance(debate, dict) and debate.get('completed')))):
            sent = debate.get('sent', 0) if isinstance(debate, dict) else 0
            error = (debate.get('error', '调用未完成') if isinstance(debate, dict)
                     else '模型没有调用独立辩论工具')
            result = f"[error] 独立辩论未完成（已推送 {sent}/10 条发言）：{error}"
        is_err = result.startswith("[error]")
        used_effort = execution_info.get('effort')
        print(f"[{self.name}][llm] elapsed={self._human_elapsed(int(elapsed))} "
              f"model={used_model} error={is_err} timed_out={'超时' in result} chars={len(result)} "
              f"sid={str(new_sid)[:8] + '…' if new_sid else '-'} | {result[:120].replace(chr(10), ' ')}")
        self.db(db.record_audit, self.chat_id, "llm", "task",
                message_id=msg_id, ok=not is_err, model=used_model, effort=used_effort,
                elapsed_ms=int(elapsed * 1000), chars=len(result), detail=result[:2000])

        # 本轮 LLM 成功 = 这轮开跑时的状态被真实流量证明可用 → 转正为回滚基线。
        # 用真实流量当健康信号，零成本、零延迟、不烧额度；判据必须是「有成功」而不是
        # 「没失败」——没有调用时「没失败」恒真，会把污染态直接存成基线。
        if not is_err:
            ops_snapshot.promote_quiet(snap_id)

        # 锁内：更新对话历史（保留最近 20 轮）+ 审计；成功后清除已消费的压缩 seed
        with self.lock:
            if not result.startswith("[error]"):
                updated = (history + [{"role": "user", "content": text},
                                      {"role": "assistant", "content": result}])[-40:]
                entry["_history"] = updated
                if self.current_entry() is entry:
                    self.sessions["history"] = updated
                if seed:
                    entry.pop("seed", None)
            self.audit(text, result, new_sid, used_model, entry=entry, effort=used_effort)

        # 主动推送结果（带失败重试，避免网络抖动丢结果）+ context 占用提示/分级提醒
        banner = self._permit_banner()
        if is_err:
            suffix, warn = "", ""
        else:
            ctx_pct = self._context_pct(new_sid or session_id, entry=entry)
            suffix = self._model_suffix(used_model, ctx_pct, effort=used_effort)
            warn = self._context_warning(ctx_pct)
        tail = "".join("\n\n" + x for x in (warn, banner) if x)
        if not is_err and tools.compact:
            self._do_compact(msg_id, reason="用户请求", entry=entry, job=job)
        delivered = self._push_reply(msg_id, result + suffix + tail,
                                     file_limit=max(0, SEND_FILE_MAX - tools.files))
        if not is_err and delivered and tools.restart and self.unsafe_mode:
            try:
                unit = tool_bridge.schedule_restart()
                self._log(f"[restart] scheduled unit={unit}")
            except Exception as e:
                self._push_reply(msg_id, f"[error] 重启调度失败：{e}", tag="restart")
        print(f"[{self.name}][done] pushed to {msg_id} model={used_model}")


    def _extract_send_files(self, text):
        """解析 GPT 回复里的 [[SEND_FILE:/abs/path]] 标记，返回 (剥离标记的正文, 合法路径, 拒绝说明)。

        **通用能力**：每一轮对话都生效，跟命令无关。以前这里有一道目录白名单（只准发
        work_dir 与本群上传目录之下的文件），实际效果是「GPT 读得到却发不出去」——
        work_dir 与源码分离的机器人（源码靠 extra_dirs 挂载）永远发不了自己的产物，
        于是它按系统提示里的规则直接答复「发送不了」。经机主明确决定（个人自用、单机
        部署）撤除该白名单：现在本机任意可读的普通文件都可外发。

        ⚠️ 代价要清楚：这条通道现在能把本机任何 bot 进程可读的文件送进飞书群，包括
        .env.local、~/.ssh、各类凭证。上传给 bot 的文件内容如果诱导 GPT 去发某个
        路径，不再有第二道闸拦它。留下的只是防呆：必须是已存在的普通文件（目录、
        /dev/* 之类字符设备都挡掉）、单文件 ≤20MB、一条回复最多 3 个。
        """
        return core_uploads.extract_files(text)

    def _send_files(self, msg_id, paths):
        """把文件逐个上传并以文件消息回复。失败回提示并返回 False，正文已送达的状态不受影响。"""
        delivered = True
        for path in paths:
            try:
                file_key = feishu_api.upload_file(self.client, path, os.path.basename(path))
                res = feishu_api.reply_file(self.client, msg_id, file_key, tag="attach")
                self._audit_sends(msg_id, "attach", res)
                if not res:
                    raise RuntimeError("飞书附件接口返回失败")
                print(f"[{self.name}][attach] sent {path}")
            except Exception as e:
                delivered = False
                print(f"[{self.name}][attach-error] {path}: {e}")
                self._push_reply(msg_id, f"⚠️ 附件发送失败：{os.path.basename(path)}（{e}）",
                                 attempts=1, tag="attach")
        return delivered

    def _push_reply(self, msg_id, text, attempts=3, tag="push", file_limit=SEND_FILE_MAX):
        """主动推送结果，失败重试几次（后台任务跑很久，不能因一次网络抖动就丢结果）。

        正文里的 [[SEND_FILE:...]] 标记会被剥离，对应文件在正文送达后作为文件消息补发。
        """
        text, files, rejected = self._extract_send_files(text)
        if len(files) > file_limit:
            rejected.extend(f"{os.path.basename(p)}：本轮最多发 {SEND_FILE_MAX} 个附件"
                            for p in files[file_limit:])
            files = files[:file_limit]
        if rejected:
            text = (text + "\n\n⚠️ 以下附件未发送：\n" +
                    "\n".join("· " + r for r in rejected)).strip()
        if not text:
            text = "（见附件）"
        # 每段单独重试，已送达的段落不会因后续段失败而重复发送。
        for chunk in feishu_api.split_text(text):
            for i in range(attempts):
                try:
                    res = feishu_api.reply_message(self.client, msg_id, chunk, tag=tag)
                    self._audit_sends(msg_id, tag, res)
                    if res:
                        break
                except Exception as e:
                    print(f"[{self.name}][push-error] attempt {i + 1}/{attempts}: {e}")
                if i < attempts - 1:
                    time.sleep(min(5 * (i + 1), 30))
            else:
                print(f"[{self.name}][push-failed] 结果推送失败 msg={msg_id}（结果已存审计表，可查库）")
                return False
        return self._send_files(msg_id, files) if files else True

    def _audit_sends(self, msg_id, tag, res):
        """把一次飞书外发的每段响应写入审计表。"""
        for s in res.sends:
            self.db(db.record_audit, self.chat_id, "feishu", tag, message_id=msg_id,
                    ok=s["ok"], code=s["code"], chars=s.get("chars"),
                    detail=(f"sent_id={s.get('sent_id')}" if s["ok"] else s.get("msg")))

    def handle_select(self, msg, idx):
        """pending 状态下用数字消息选择：本群对话（/sessions）/ 本机会话接管（/allsessions）。"""
        if self.pending == "session":
            sessions = self.sessions["list"]
            if 0 <= idx < len(sessions):
                with self.lock:  # 与 worker 的 update_session 互斥
                    self.sessions["current"] = idx
                    entry = sessions[idx]
                    self.set_current(entry)
                    self.sessions["history"] = (list(entry["_history"]) if "_history" in entry
                                                else self.load_history(entry["id"]))
                self.pending = None
                feishu_api.reply_message(self.client, msg["id"], f"已切换到对话 {idx + 1}：{entry['label']}")
                self._send_recent_history(msg["id"])
                print(f"[{self.name}][session] switched to idx={idx}")
            else:
                feishu_api.reply_message(self.client, msg["id"], f"无效序号，请输入 1～{len(sessions)}")
        elif self.pending == "allsession":
            shown = self.pending_all
            if 0 <= idx < len(shown):
                self._attach_session(msg, shown[idx])
            else:
                feishu_api.reply_message(self.client, msg["id"], f"无效序号，请输入 1～{len(shown)}")

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
        # 传完文件下一句就是要用它——把路径存进待附带缓冲，下一条任务消息自动带上，
        # 不必让用户再把回复里的路径手抄一遍。富文本内嵌图（handle_post_images）走同一个
        # 缓冲，附带说明由 run_task 统一拼，两条上传路径不再各拼各的。
        pending = self._remember_upload(info)
        feishu_api.reply_message(self.client, msg["id"], "\n".join([
            "已保存上传文件：",
            info["path"],
            f"大小 {info['size_human']}",
            f"（已加入下一条消息的上下文{f'，共 {pending} 个待附带' if pending > 1 else ''}，"
            "直接说要做什么即可）",
        ]))
        print(f"[{self.name}][upload] {info['path']} ({info['size_human']}) pending={pending}")

    # -------------------------------------------------- 上传文件自动进上下文

    def _remember_upload(self, info):
        """把刚落盘的上传文件记入待附带缓冲，返回当前待附带数量。"""
        with self.upload_lock:
            self._expire_uploads()
            self.pending_uploads.append({"path": info["path"], "name": info["file_name"],
                                         "size_human": info["size_human"], "ts": time.time()})
            return len(self.pending_uploads)

    def _expire_uploads(self):
        """丢弃超过 TTL 的待附带文件：几小时前传的东西再塞进 prompt 只会误导。"""
        with self.upload_lock:
            self.pending_uploads = core_uploads.expire(
                self.pending_uploads, time.time(), UPLOAD_CONTEXT_TTL)

    def pending_uploads_snapshot(self):
        """工具侧读取仍待附带的文件，不消费缓冲。"""
        with self.upload_lock:
            return [dict(u) for u in core_uploads.expire(
                self.pending_uploads, time.time(), UPLOAD_CONTEXT_TTL)]

    def _drain_uploads(self):
        """取走并清空待附带的上传文件（一次任务消费一次，不重复附带）。"""
        with self.upload_lock:
            self._expire_uploads()
            items, self.pending_uploads = self.pending_uploads, []
            return items

    @staticmethod
    def _attach_block(items):
        """把待附带文件拼成一段附在用户消息后面的说明。"""
        return core_uploads.attach_block(items)

    def handle_post_images(self, msg):
        """下载 post 富文本里内嵌的图片，写台账，把落盘路径记入待附带缓冲，返回 (文字, 张数)。
        内嵌图和单发图走同一个缓冲，附带说明由 run_task 统一拼——两条路径别各拼各的。"""
        text = msg.get("text", "")
        n = 0
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
            self._remember_upload(info)
            n += 1
        return text, n

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
            ptext, n_img = self.handle_post_images(msg)
            if n_img:
                self.run_task(msg, (ptext or "请查看随附的图片").rstrip())
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
        parts = text.split(None, 1)
        head, rest = parts[0], parts[1] if len(parts) > 1 else ""
        handler = self.commands.get(head.lower())
        if handler:
            handler(msg, rest.strip())
            return
        # 形如「单个斜杠词」（/foo 或 /foo args）却不是已知命令：直接提示，不走 LLM。
        # 只匹配「斜杠 + 纯 ASCII 词」，故文件路径参数（/home/x.py、/etc/nginx.conf，含更多 / 或 .）
        # 不会被误判为命令，仍照常作为任务交给 GPT。
        if re.fullmatch(r"/[A-Za-z][A-Za-z0-9_-]*", head):
            sugg = difflib.get_close_matches(head.lower(), self.commands.keys(), n=1, cutoff=0.5)
            hint = f"，你是指 {sugg[0]} 吗？" if sugg else ""
            feishu_api.reply_message(self.client, msg["id"],
                                     f"未识别的命令 {head}{hint}\n发送 /help 查看全部命令")
            return
        # 普通任务
        self.run_task(msg, text)

    # ------------------------------------------------------------ 长连接入口 / 启动

    def start(self):
        """启动后台线程：inbox 分发（命令回复/上传下载/入队）+ worker（跑 Codex 长任务）。
        事件由长连接推来，绝不在 ws 事件循环里做网络 IO。"""
        threading.Thread(target=self._inbox_worker, name=f"{self.name}-inbox", daemon=True).start()
        threading.Thread(target=self._worker, name=f"{self.name}-worker", daemon=True).start()
        if self.wiki_space_id:
            # 知识库同步线程：启动即先同步一次，之后按 sync_interval 循环
            threading.Thread(target=self._wiki_worker, name=f"{self.name}-wiki",
                             daemon=True).start()
        print(f"[{self.name}] ready, chat_id={self.chat_id}, work_dir={self.work_dir}"
              + (f", wiki={self.wiki_space_id}" if self.wiki_space_id else ""))

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
                try:
                    self._push_reply(msg["id"], f"[error] 消息处理失败：{type(e).__name__}: {e}", tag="inbox-error")
                except Exception as report_error:
                    self._log(f"[inbox-report-error] {report_error}")
            finally:
                self.inbox.task_done()
