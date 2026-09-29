# feishu-claude-bot

## 项目目标

本地运行的服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。

**不需要开放本地端口，也不需要公网 IP**：采用飞书官方 SDK `lark-oapi` 的**长连接（WebSocket）**
接收事件——连接由本机主动向飞书发起（出站），只需能访问公网即可。群里 **@机器人** 触发。

## 文件说明

代码按职责拆为 6 个模块（依赖单向：`feishu_claude → bot → {db→models, feishu_api, claude_runner}`）：

- `feishu_claude.py` — 入口：解析 `.env.<name>`、建 DB engine、**按 app_id 分组建长连接**、管理 PID
- `bot.py` — `Bot` 类：单个机器人的消息分发、命令处理、会话编排（业务主体，改功能主要看这里）
- `models.py` — SQLAlchemy ORM 模型：`BotState` / `Conversation`(表 sessions，含会话级 `cwd`/`external`)
  / `Message` / `Upload`(表 uploads)
- `db.py` — 数据库层：engine/session 管理 + 基于 ORM 的数据访问（不再手写 SQL）
- `feishu_api.py` — 飞书交互层（基于 lark-oapi SDK）：长连接客户端与事件分发、事件归一、回复/下载、文本分段
- `uploads.py` — 上传图片/文件/压缩包的安全下载与存储（隔离目录 700、文件 600、文件名消毒、不自动解压）
- `wiki_sync.py` — 飞书知识库 → 本地 markdown 镜像：列知识空间、递归走节点树、docx **block → markdown**（保留标题层级）、按 `obj_edit_time` 增量同步；也可作自检 CLI 独立运行
- `wiki_index.py` — 知识库索引与检索：按标题小节切块、ollama 取向量、**向量 + MySQL ngram 全文双路 RRF 混合召回**；刻意不依赖 Bot/飞书，将来可整个文件搬走
- `claude_runner.py` — 调用 `claude` CLI、解析 stream-json（含 `tool_use` → 本轮动作回报）、
  磁盘 session 文件的枚举/探查/回显/删除（`list_all_sessions`/`session_exists`/`session_tail`/
  `session_context`/`delete_claude_session`，都以 cwd 定位 `~/.claude/projects/<key>/`）
- `ops/` — 运维面（**刻意不 import 任何项目模块**，见下「自动回滚」）：`snapshot.py` 快照/回滚、
  `guard.py` 看门狗、`notify.py` 独立告警通道、`envfile.py` 独立 env 解析、`systemd/` unit 模板
- `.env` — 配置模板（入 git）；`.env.local` — 实际配置（不入 git）

配置统一为 `.env.local`（默认），含共享项 + `BOTS` JSON 列表。

**接收：长连接（WebSocket）**。长连接是「**每 app 一条、cluster 模式、不广播**」——同一 `app_id`
只能有一条有效连接，多开只有随机一条能收到事件。故入口**按 `app_id` 分组**：每个 app_id 一个 SDK 客户端
（发送/下载，token 由 SDK 内部管理）+ 一条长连接，收到 `im.message.receive_v1` 事件后**按 `chat_id`
路由**到对应 `Bot`。多个群共用一个 `app_id` 时共享这一条连接。

**线程模型**：事件回调跑在 ws 事件循环里、必须快速返回，故 `Bot.on_event` 只做 `message_id` 幂等去重
（防飞书重投）后丢进 inbox 队列；每个 Bot 有 inbox 线程（串行处理命令/上传/入队）+ worker 线程
（跑 claude 长任务）。**同一 chat_id 串行保序、不同 chat_id 并行**；所有机器人共享一个全局并发闸
（`MAX_CONCURRENT`，`threading.BoundedSemaphore`），限制同时运行的 claude 进程数，防止内存被打满。
DB 访问走 SQLAlchemy ORM；每次操作开短生命周期 Session（线程安全），状态按 `chat_id` 隔离。

> 注意：长连接断线期间飞书**不补发**漏掉的事件（SDK 会自动重连）；进程未运行时发的消息不会补处理。

## 命令清单（2026-09-29 精简后）

命令只保留**不可替代的护栏与高频入口**，共 7 个；其余能力一律走自然语言。

| 命令 | 作用 | 保留理由 |
|---|---|---|
| `/new` | 新建对话（清空历史） | 护栏，不可替代 |
| `/sessions` | 列对话，回序号切换 | 高频（14 次/18 天） |
| `/del <序号>` | 删对话 | 高频（13 次/18 天） |
| `/unsafe` | 开关最高权限 | 护栏，**绝不能交给 LLM 自己开** |
| `/usage` | 查订阅用量 | 高频（18 次/18 天） |
| `/debate <辩题>` | 正反方各 5 轮辩论 | 待阶段 4 重构为 skill 后移除 |
| `/help` | 帮助 | — |

**2026-09-29 撤除的 10 个命令**（依据：18 天 journal + 79 天 `audit_logs` + `sessions` 表实测）：
`/compact`（手动 0 次，24 次压缩全自动）、`/retry`（2 次/79 天）、`/files`（0）、
`/model`（**59 个会话 `sessions.model` 全为 NULL，从未生效过**）、`/context`（1 次，与 inline
`ctx N%` 重复）、`/wiki`（0，CLI 等价）、`/permit`（0，98% 轮次直接用 unsafe）、`/name`（0）、
`/allsessions`（0）、`/sync` `/resync`（0，见 gitsync 段）、`/restart`（0，见「自杀式重启陷阱」）。

撤除的是**命令入口**，不是能力：`_do_compact`、`_wiki_reference`、`list_all_sessions`、
`pending_uploads` 等底层全部保留，阶段 2~4 会以 tool 形式回归。详见 `docs/OPTIMIZATION.md`。

**一 chat_id 一 work_dir**：每个机器人绑定一个群（chat_id）+ 一个工作目录（`work_dir` 单字符串，
省略回退家目录 `~`），chat_id 唯一。视觉隔离靠分群；一个群内用 `/new`、`/sessions` 手动管理多个对话。
会话与 `permit`/`unsafe` 权限均按 `chat_id` 分区，持久化在 `bot_state` 表。
**唯一的例外是「接管本机会话」**（底层能力仍在，命令入口已撤除）：那种会话在 `sessions.cwd` 里记着自己的
项目目录、在那里跑，见下文「接管本机 Claude Code 会话」。取工作目录一律走 `Bot.entry_cwd()`，
别再直接读 `self.work_dir`。

## BOTS 每项字段说明（`.env.local` 里的 `BOTS` JSON 列表）

```json
{
  "name": "机器人名称（日志标识）",
  "app_id": "飞书应用 App ID",
  "app_secret": "飞书应用 App Secret",
  "chat_id": "监听的群聊 ID（格式 oc_xxxxxx，唯一）",
  "work_dir": "Claude Code 执行任务的工作目录（绝对路径，可省略→家目录 ~）",
  "extra_dirs": "可选字符串数组：额外挂进工作区的只读目录（--add-dir），启动时过滤掉不存在的路径",
  "wiki": "可选对象：绑定飞书知识库做 RAG，{space_id, root_node（可选，只同步某子树）, top_k（默认5）}"
}
```

**extra_dirs（额外挂载目录）**：cwd 始终是 `work_dir`，`extra_dirs` 只是让 Claude 能*读*到
别处的代码/资料。典型用法是「群里的对话产物不要污染源码目录」——把 work_dir 设成一个
专用空目录，把项目源码用 `extra_dirs` 挂进来（`resume` 机器人就是这么配的）。
该 chat 的上传目录始终自动挂载，不必写进 `extra_dirs`。

**发送者身份**：`event_to_msg` 会把发言人 open_id 放进 `msg["sender_id"]`，`_execute_job`
再以 `[飞书用户: ou_xxx]` 作为首行拼进给 Claude 的 prompt。群里多人共用一个机器人时，
Claude 可据此按人分账（`resume` 项目就用它做 `--owner`）。拿不到时该行不出现。

**附件回传协议 `[[SEND_FILE:/abs/path]]`**：Claude 在回复正文里写这个标记，`_push_reply`
会剥离标记、正常发送正文，随后把对应文件作为文件消息补发到群（复用 `feishu_api.upload_file`
+ `reply_file`）。**这是通用能力，每一轮对话都生效、不绑定任何命令。**
能力说明写在 `claude_runner.SYNC_SYSTEM_PROMPT` 里，
每次调用都 `--append-system-prompt` 带上，所以 Claude 任何时候都知道自己能发文件。

**目录白名单已于 2026-08-30 按机主决定撤除**（个人自用、单机部署）。撤除的直接原因：旧白名单只认
`work_dir` 与本 chat 上传目录，而 `resume`/`stock` 这类「work_dir 是专用空目录、源码靠 `extra_dirs`
挂进来」的机器人，产物天然落在 `extra_dirs` 里——Claude 读得到却发不出去，于是它照着系统提示里的
规则直接回一句「发送不了」，看起来像是这个功能压根不存在。现在 `Bot._extract_send_files` 只剩防呆：
必须是绝对路径、realpath 是**已存在的普通文件**（目录、`/dev/*` 之类字符设备挡掉）、单文件 ≤20MB、
一条回复最多 3 个、同一路径重复标记只发一次；被拒的会在正文末尾列出原因。
⚠️ **代价要清楚**：这条通道现在能把 bot 进程可读的**任意**本机文件送进飞书群——`.env.local`、
`~/.ssh`、各类凭证都在内；上传给 bot 的文件若诱导 Claude 去发某个路径，不再有第二道闸拦它。
要收回就把 roots 白名单加回 `_extract_send_files`（建议 roots = 当前会话有效 cwd + 上传目录 +
`extra_dirs`，而不是退回只认 `work_dir` 的旧版）。

**上传文件自动进上下文**：传完文件下一句话就是要用它，让用户再手抄一遍路径纯属多余。
`handle_upload` 落盘后把路径记进 `Bot.pending_uploads` 缓冲，**下一条任务消息**由 `run_task`
统一拼一段「[随消息附带的文件，可直接读取]」附在正文后（`_drain_uploads` + `_attach_block`），
消费即清空、不重复附带。富文本内嵌图（`handle_post_images`）改成也只往这个缓冲里塞，
两条上传路径共用同一套拼装逻辑，不再各拼各的。要点：
- **拼进 `text` 而不是只改 `run_text`**（与知识库 RAG 注入相反）：路径很短撑不爆 context，
  进了 history 之后续聊追问「刚那张图」才有依据；RAG 资料动辄几千字才必须绕开 history。
- 缓冲**只在 inbox 线程读写**（`handle_upload` 与 `run_task` 都在这条线程上），故不加锁。
- `UPLOAD_CONTEXT_TTL`（默认 30 分钟）过期即丢——几小时前传的文件再塞进 prompt 只会误导。
- 只在内存里，进程重启即丢（`uploads` 表仍有台账）。查看/清空的 `/files` 命令已撤除
  （2026-09-29，零使用），需要时直接问「现在有哪些待附带文件」。

**接管本机 Claude Code 会话**：扫 `~/.claude/projects` 列出本机全部会话，接管后在本群建一个
绑定该 `session_id` 的会话条目，之后每轮 `--resume` 都在**那个会话原本的项目目录**下跑，
等于把终端里的对话搬到飞书接着聊。
⚠️ **`/allsessions` 命令已于 2026-09-29 撤除**（18 天零使用，「列表+数序号」交互太笨重）。
底层能力（`claude_runner.list_all_sessions` / `session_exists` / `session_tail`、`sessions.cwd`
/ `external` 列、`Bot.entry_cwd()`）**全部保留**，等阶段 4 的 tool 落地后用自然语言接管。
在那之前这条链路没有入口，属于暂时休眠的代码。
- **cwd 必须下放到会话级**，这是能不能做成的关键：Claude Code 的 session 文件存在
  `~/.claude/projects/<cwd 把 / 换成 - >/<sid>.jsonl`，`--resume` 在别的目录下跑必然
  `No conversation found`。故 `sessions` 表加 `cwd` 列（NULL=用本 bot 的 `work_dir`），
  `Bot.entry_cwd()` 是唯一取值口，`execute_claude` / `_context_pct` / `delete_claude_session`
  全部改用它，不再直接读 `self.work_dir`。接管期间本 bot 的 `work_dir` 会作为 `--add-dir` 挂进去。
- **`external` 列让 `/del` 只解绑、不删盘**：那是用户自己在终端里的对话，飞书里手滑删一下就毁掉
  他的现场，代价完全不对等。压缩（`_do_compact`）新建的会话**继承 cwd 但不继承 external**——
  不继承 cwd 的话，压一次就被悄悄丢回 bot 的 work_dir，后半段对话对着另一个项目说话。
- **`cwd` 取自 session 文件里的首条 `cwd` 事件**，不是从目录名反解：目录名是把 `/` 换成 `-`
  编码来的、不可逆（路径本身含 `-` 时还原不出）。拿不到 cwd 的会话在列表里标「⚠️无法接管」。
- 接管后会回显最近 3 轮对话（`claude_runner.session_tail`）——这段历史只存在于本机 jsonl 里，
  bot 的 `messages` 表没有，不回显的话接不上话头。只取 text 块，`tool_use`/`tool_result`
  与 `<command-name>` 之类 CLI 注入全部滤掉，否则群会被过程噪音刷爆。
- **context 护栏要为它开个后门**：终端里聊久了的会话一接管往往就接近上限，而它在 `messages` 表里
  没有任何历史（那些话发生在终端里），`_do_compact` 照旧「本地历史为空 → return False」的话，
  护栏一触发就是死路——压缩压不动，而这类会话恰恰
  最容易一上来就顶到阈值。故本地历史为空时改从 `session_tail(rounds=12)` 读盘取素材。接管时的
  回执也会直接把 context 占用报出来，免得用户发第一条就莫名撞墙。
- 续聊会写进本机那份 jsonl（这正是「延续」的含义）。若同一 session 在终端里也开着，两边并发写
  同一文件会互相覆盖——接管前先把终端那边退出。

**增量 bundle 同步（gitsync）已于 2026-09-29 整套移除**。撤除依据：`tag='patch'` 共 52 次
调用全部来自**两个已下线的群**（`oc_d140cbad…` 46 次、`oc_aecd3e00…` 6 次），当前在线的 9 个 bot
无一使用过，`sync_patch` 在所有 bot 上均为 false，最后一次使用距撤除日 53 天。
删除范围：`gitsync.py`（219 行）、`/sync`、`/resync`、`Bot._send_bundle`、`_commit_message`、
`_git_ready`、`sync_patch` 配置项。隐藏基线 ref `refs/feishu-sync/<chat_id>` 留在仓库里无害。
将来若又需要远程同步：让 Claude 用自然语言直接跑 git（它本来就能），或按需重写——
`git bundle` + `fetch` + `merge --ff-only` 的做法本身是对的，只是没人用。

**飞书知识库 RAG（wiki_sync + wiki_index）**：给某个 chat 绑一个飞书知识空间，提问时先检索
相关条目、把片段注入 prompt，让 Claude 基于知识库回答并注明来源。只有配了 `wiki.space_id`
的机器人才启用。

- **抓取用 block API，不用 `raw_content`**：`raw_content` 返回纯文本、标题与正文无法区分，
  而知识库的典型写法是「一篇文档记录大量问题、一个问题一个小节」，丢掉 heading 层级后只能
  按字数硬切、切点横穿条目，把问题和它的解决方案劈开。所以走 `docx.document_block.list`
  自转 markdown 保留层级，让切块能做到「一个问题 = 一个 chunk」。块类型判断看「哪个属性
  非空」而不是 `block_type` 数字枚举，飞书加新类型也不会错位。
- **混合召回，两路都不能省**：向量路（ollama + bge-small-zh-v1.5，512 维）负责同义/口语化
  问法，MySQL ngram 全文路负责专有名词精确匹配（错误码、组件名）。实测问「网关挂了返回502」
  时纯向量会把 Nginx 条目挤掉、分差仅 0.009，全文路靠 `502` 精确命中救回；反过来「怎么报销
  出差的钱」这类完全换词的问法只有向量能召回。两路各取 Top-20 后按 RRF 融合（只看排名不看
  绝对分，规避两路分数量纲不同）。
- **索引按 `space_id` 分区**（不按 chat_id）存在 `WIKI_CACHE_DIR` 下，多个群配同一知识库时
  共享一份索引。**缓存目录放在 `work_dir` 之外**——原因是 gitsync 每轮 `git add -A`
  会把镜像整个提交进 git 历史；gitsync 已移除，但保持在外面仍然更干净（不污染工作区）。
- **注入只改 `run_text`，不进 history**：审计与压缩摘要仍用用户原文，否则历史会被资料撑爆、
  摘要也跟着跑偏。代价是 `--resume` 续聊会让历轮资料在 session 里累积，**context 比不开知识库
  涨得快**，靠 `AUTO_COMPACT_THRESHOLD` 自动压缩兜底；每轮注入上限为 Top 5 × 3000 字。
- 同步：启动时一次 + 后台每 `WIKI_SYNC_INTERVAL` 秒增量一次（按 `obj_edit_time` 只重拉变更
  文档，只对变更文档重新编码）。**无变更时不打日志**——实测 18 天 866 轮同步有 864 轮
  什么都没变，无条件打印会把 journal 刷成 40% 的空转播报（`_wiki_refresh` 的 `changed` 判断）。
  `wiki_status` 字段仍每轮更新，静默不丢信息。飞书云文档有 `drive.file.edit_v1`
  事件可做秒级刷新，但要逐文件订阅、且新增节点无事件、断线不补发，故只能当加速器，定时扫描
  必须保留（本版未做事件订阅）。
- 前置：开放平台开 `wiki:wiki:readonly` + `docx:document:readonly`，并且**必须把机器人应用
  加为该知识库的成员**（知识库 → 设置 → 成员管理 → 添加应用）。不加成员时飞书**不报权限错，
  只返回空列表**——所以判据是：`--spaces` 返回空 = 还没加成员，不是代码问题。
- **`/wiki` 命令已于 2026-09-29 撤除**（零使用）。四个子命令 CLI 完全等价，见下「启动方式」。
  RAG 注入本体（`_wiki_reference`，每轮检索+注入）与后台同步（`_wiki_worker`）全部保留。
- 依赖 ollama（systemd 常驻）：`ollama pull quentinz/bge-small-zh-v1.5`。模型驻留时长走请求体
  的 `keep_alive` 参数控制，不动系统配置。换模型会改变向量维度，需 `--rebuild` 全量重建。

### 知识库编写规范（写作侧约定，直接决定召回质量）

切块参数（`wiki_index.MAX_CHARS` 等）是按这套规范调的，改规范要同步改参数。

- **组织方式**：按主题/组件分文档（`Nginx 排障`、`MySQL 排障`…），**文档内一个问题一个 H2**。
  目录树本身要有语义层级，别把文档平铺在根节点——面包屑靠它，是白送的检索信号。
- **标题写成问题本身**，不写编号或概括词：`Nginx 502 Bad Gateway：upstream 响应超时` 而非
  `问题3`。面包屑会拼进 chunk 文本，标题是命中率最高的部分，越接近「用户会怎么问」越准。
- **条目固定结构**：`现象`（贴报错原文/日志，全文路精确匹配的黄金材料）/ `环境` / `原因` /
  `解决` / `关键词`（人工补同义说法）。实测加一行「关键词：网关挂了、502」就能让原本失败的
  向量召回变成 Top1 命中——一行字的成本，收益比换大模型还高。
- **必须用真 heading 块**，不能用加粗伪标题：`**问题一**` 只是带 style 的 text block，切块器
  按 heading 找边界，识别不到就退化成按字数硬切。飞书 docx 支持粘贴 markdown 自动转结构化块。
- **代码用真代码块；关键报错不要只截图**（本版不抓图片，截图里的错误信息等于不存在）。
- **单条目 100~800 字**：太短语义不足，太长触发二次硬切、切点横穿。
- **不要用表格承载问题清单**（整表常是一个 block 切不开，每行又太短）。
- **过期条目及时删除或标注「已废弃」**——检索不区分新旧，陈旧答案会被照样召回当依据。

**启动 / 恢复通知（`STARTUP_NOTIFY`，默认开）**：长连接就绪后主动往群里推一条
「✅ 已启动」（含时间、启动耗时、`env`/`pid`、代码版本 = 本仓库 HEAD 短 hash + 提交标题），
改完重启不用盯 `journalctl` 也知道服务起没起来、起的是哪一版。之后每次**断线重连成功**再推
「🔄 已恢复」。实现在 `feishu_claude.watch_connection`：SDK 只在*重连*成功时回调
`on_reconnected`、首连不回调，故首连靠后台线程轮询 `ws._conn`（私有属性，读不到时按已就绪处理），
且**不设超时**——连上多晚都发；首连若走了重连路径，`ready` 未置位时 `on_reconnected` 直接返回，
不会重复发。`on_reconnected` 跑在 ws 事件循环里，发消息（阻塞 HTTP）必须丢线程，否则掀翻连接。
通知走 `feishu_api.send_text`（`im.v1.message.create` + `receive_id_type=chat_id`，主动发、
不针对某条消息回复）。两种通知都带一句「未运行/断线期间的消息不会补处理」——飞书不补发事件。
**通知范围** `STARTUP_NOTIFY_BOTS`（按 name 或 chat_id，逗号分隔；留空=所有群）：多个群共用一条
长连接时，重启一次会把每个群都刷一遍，故本机只发给 `feishu-bot` 那个群（work_dir 即本项目，当运维群）。

**本轮动作回报（`claude_runner.format_actions`）**：stream-json 的 assistant 消息里 `content` 是
一组块，`text` 是它**说**的话、`tool_use` 是它**做**的事。旧版只收 `text`，于是飞书里永远只看得到
前者——2026-08-04 的事故正是这样：那一轮实际发生了一个 `Write` 写进项目级
`.claude/settings.local.json`（把 `ANTHROPIC_BASE_URL` 指向一个连不上的内网网关，该 bot 此后
每次调 LLM 都 ECONNRESET、持续 11 小时无人察觉），而回复读起来只像一段配置建议。现在回复末尾会附
「📝 本轮动作」，列出**写**（Write/Edit/MultiEdit/NotebookEdit → 文件名）与**执行**（Bash → 命令）。
取舍：不报 `Read`/`Grep`/`Glob`（只读、量大，全报会淹没正文）；`Bash` 必须报（`/unsafe` 常开，
能跑任意命令，这恰恰最该被看见）；**绝不回显 `Write` 的 content 或 `Edit` 的 new_string**（可能很大、
也可能含密钥），只报文件名；路径显示成相对 work_dir 的形式，但**落在 work_dir 之外时保留绝对路径**
——越界正是最需要看见的；去重 + 超 8 条折叠。压缩摘要那类调用传 `report_actions=False`，否则动作
清单会混进摘要被当成上下文。

**自动回滚（`ops/`，独立 systemd unit）**：把「重启后有异常就回滚」从 prompt 约定提取成基础设施。
prompt 约定从原理上不成立——Claude 在飞书 bot 群里被调用时它自己就是 `feishu-bot.service` 的子进程，
执行 `systemctl restart` 会把整个 cgroup（含它自己）一起杀掉（exit 144、无输出），**承诺回滚的执行者
在能验证之前就死了**。所以回滚交给 `feishu-bot-guard.service` + `.timer`（每 10 分钟，oneshot），
它在那个 cgroup 之外。

- **保护范围只有本项目目录**（含 `.env.local` / `.claude/`，排除 `.venv`、`__pycache__`、`.git`）。
  其余 9 个 work_dir 不进快照——它们跑的是同一份代码，主程序活着就能通过飞书把它们修回来；而
  work_dir 里全是用户数据（resume 档案、stock 数据、wiki 索引），回滚等于删用户的东西。
  `.git` 同理排除：回滚它会把真实提交历史倒回去，且能规避「rsync 撞上并发 git commit
  拍到半写状态」。代价是回滚后工作树与 HEAD 不一致，需要自己 `git status` 看一眼。
  （原先还有一条理由是「gitsync 的基线 ref 会被一起倒回」，gitsync 已于 2026-09-29 移除。）
- **基线 = 「某一轮任务动手之前」的状态**，不是「上次重启前」也不是「当前状态」。`bot.py` 在调
  `run_claude` **之前** `take()`，该轮 LLM 成功后才 `promote()` 转正。这是整套设计的支点：写坏配置
  的那一轮它自己往往是成功的（正是它写坏的），拿完成时刻的状态当基线会把污染态存成 known-good，
  回滚过去等于没回滚。快照走 rsync `--link-dest` 硬链去重，实测 86ms / 380K，每轮任务拍一次不心疼。
- **健康信号用真实流量的成功记录**（`audit_logs` 里 `kind='llm'` 的 `ok`），零成本、零延迟、不烧额度。
  判据必须是「**有成功**」而不是「没失败」——没有调用时「没失败」恒真，基线会被当场毒化。
- **触发判定按 chat 分组**：某个 chat 在 6 小时窗口内失败 ≥2 次**且零成功**。不能用全局统计——事故当天
  10 个 bot 里只有 1 个全挂、其余 9 个全程正常，全局会被稀释成「窗口内有成功」，看门狗根本不触发。
- **另一条判据：服务本身没在运行**（`service_verdict`）。上面那条盖不住「服务整个起不来」——那时不会
  再产生任何 LLM 记录，窗口里空空如也、`sick` 为空，看门狗会一声不吭地放过去（2026-08-15 就是这样：
  `.env.local` 的 `BOTS` 少一个引号，服务崩溃循环 4 分钟，看门狗全程零动作）。
  **不能只看 `systemctl is-active`**：它只给一个词，而「正在启动」和「崩了正在重试」**都叫
  `activating`**。改成一次 `systemctl show` 取 `ActiveState/SubState/Result/NRestarts` 四分：
  `SubState=auto-restart`（或 `failed`）= **崩溃循环**，直接跳过 DB 判定进分诊；
  `start-pre/start/start-post` = 正常启动中，**仍然跳过**（`ExecStartPre` 探代理最多花 90 秒，
  把它当成死了会在每次正常重启时误触发回滚——`Result` 在每次 start 时会被清回 `success`，
  所以「崩过一次、现在正常启动」不会被误判）；`inactive/dead` = 未运行，照旧进分诊。
  崩溃循环有个 2 秒的「运行窗口」瞬时字段全是健康的，靠**跨轮 `NRestarts` 增长**兜住
  （增长 ≥2 且没连续 active 满 120 秒才算，避免把「重启过但现在好了」误判成循环）。
  服务死掉时网络判据仍然要跑——`ExecStartPre` 探代理失败正是最常见的起不来原因，那是外部故障，
  回滚治不了。
- **分诊**（回滚只治文件，治不了外部故障）：错误文案含额度字样 → 只告警；`curl api.anthropic.com`
  返回非 401/405（403=被当中国大陆，000=代理挂）→ 只告警；两关都过才回滚。服务没起来时**再加一道
  廉价预检**：`json.loads` 试解析 `.env.<name>` 的 `BOTS`，解析失败几乎可以断定是配置问题、回滚命中
  率极高，解析正常则崩因可能在别处（依赖/代码/DB），照样回滚但在告警里标明把握较小。另外**当前状态
  与基线一字不差时不回滚**——那是空操作，只会白白吃掉「同一基线只回滚一次」的名额。
- **回滚只做一次**：防抖标记记住基线 id，同一基线回滚过还失败就停手等人工，避免重启死循环。回滚前把
  现场整份另存 `rejected-<ts>/` 不删——事后要 diff 才知道到底哪一轮写了什么。
- 回滚后 `restart` → 等 45s → **验证服务真的稳住了**（连续 30s `active` 且 `NRestarts` 不再增长）
  → **canary**（在项目目录里真跑一次 `claude -p`）→ 推飞书。两条缺一不可、各证一半：服务 `active`
  不算 LLM 通道通（事故当天进程也一直 active），而 canary 在项目目录里跑 claude、**服务崩着它照样
  通过**，所以服务起不来那一类只能以「稳住」为准、canary 只作附加确认。
- **告警必须写清「你的改动被撤销了」**：回滚会把用户刚做的**合法**改动一起撤销（2026-08-15 那次就是
  新加的 `ipcp` 配置），所以正文里带上基线时间、被撤销改动的时间与文件名，否则用户会二次困惑
  （「我明明加了 ipcp，怎么没了」）。
- 告警走 `ops/notify.py`：自带凭证、只用标准库 urllib，**不依赖 bot 进程活着**——服务挂了正是最需要
  告警的时刻。同理整个 `ops/` 不 import 任何项目模块（import 会拖进 db/feishu_api/bot 整条链，其中
  任意一个坏掉救援脚本自己先起不来）。告警目标由 `GUARD_NOTIFY_BOT` 指定（BOTS 里的 name，留空取第一项）。

```bash
./ops/install-guard.sh                          # 安装/更新 unit + timer
.venv/bin/python ops/guard.py --status          # 看窗口内各 chat 的 LLM 成败与当前基线
.venv/bin/python ops/guard.py --dry-run         # 演练：只判定与打印，不回滚不重启
.venv/bin/python ops/snapshot.py list|diff      # 快照列表 / 当前状态与基线的差异文件
.venv/bin/python ops/snapshot.py rollback       # 手动回滚（回滚后需自己 restart）
```

另有一道内核级防线（与上面互补）：`feishu-bot.service` 的
`ReadOnlyPaths=/home/ljiawen/project/feishu-bot/.claude`。`--dangerously-skip-permissions` 拆掉的
是 Claude Code 自己的许可闸，而 `ReadOnlyPaths` 是 mount namespace 只读挂载，claude 绕不过，写入直接
`EROFS`——这是 `unsafe` 常开状态下唯一还生效的防线。⚠️ **绝不要把它指到 `~/.claude`**：memory、
session jsonl、OAuth `.credentials.json`（每次刷新都在写）全在那里，锁了服务直接废掉。

`task_timeout` / `heartbeat_interval` / `max_concurrent` / `auto_compact_threshold`
/ `startup_notify` / `guard_notify_bot` / `wiki_cache_dir` / `wiki_sync_interval` / `wiki_embed_url`
/ `wiki_embed_model` 为共享项，写在 `.env.local` 顶层。

**context 护栏（省额度）**：每轮 `--resume` 都会把整段对话重喂给模型，忘了 `/new` 时 context
越滚越大、每轮重复烧订阅额度。为此：①每条回复 inline 附带 `ctx N%`，越阈值（60/80%）追加分级提醒；
②续聊前若 context 占用达 `AUTO_COMPACT_THRESHOLD`（默认 75%，0 关闭）自动压缩——把本地历史用
便宜模型（haiku）摘要成一段上下文、开新对话（`/new`）并把摘要作为新会话首轮 prompt 的前缀带上，
从而清空 context 重新计费又不丢脉络；③关掉自动压缩时，92% 硬闸兜底：触顶暂停执行、要求手动
`/new`（或直接说「压缩一下上下文」）。
**`/compact` 命令已于 2026-09-29 撤除**：18 天内 24 次压缩**全部**是 `reason=context` 自动触发，
手动零次。`_do_compact` 本体保留，只是没有手动入口了。
（`POLL_INTERVAL` 已废弃：改用长连接推送，不再轮询；保留仅为兼容旧配置。）

## 飞书开放平台前置配置

1. 前往 https://open.feishu.cn 创建企业自建应用
2. 权限管理中开启：`im:message`（发消息）、`im:message.group_at_msg`（收群内@机器人的消息）、
   `im:resource`（下载消息里的图片/文件）；如需私聊再加 `im:message.p2p_msg`；
   用知识库 RAG 再加 `wiki:wiki:readonly` + `docx:document:readonly`，
   并**把应用加为目标知识库的成员**（否则只返回空列表、不报权限错）
3. **事件与回调 → 事件配置：订阅方式选「长连接」**，并订阅事件 `im.message.receive_v1`（接收消息）
4. 发布应用版本，将 Bot 添加到目标群聊
5. 从「凭证与基础信息」获取 `app_id` 和 `app_secret`
6. `chat_id`：飞书 PC 端打开群聊 → 右上角「…」→「复制链接」→ 链接中 `open_chat_id=oc_xxx` 的值

> 触发方式：本项目用 `im:message.group_at_msg`，群里需 **@机器人** 才会收到事件（正文里的 @ 占位会被自动剥掉）。
> 若想「群里每条消息都执行」，需改申请敏感权限 `im:message.group_msg`（获取群组中所有消息，需管理员审批）。

## 启动方式

```bash
# 安装依赖（仅首次）；本项目用 .venv
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 用知识库 RAG 才需要（仅首次）：本地 embedding 服务
curl -fsSL https://ollama.com/install.sh | sh
ollama pull quentinz/bge-small-zh-v1.5

# 知识库自检（不启动服务即可验权限/抓取/切块/召回）
.venv/bin/python -m wiki_sync --env local --bot <name> --spaces          # 列知识空间拿 space_id
.venv/bin/python -m wiki_sync --env local --bot <name> --sync            # 增量同步 + 建索引
.venv/bin/python -m wiki_sync --env local --bot <name> --search "问题"   # 看向量/全文/融合三路结果
.venv/bin/python -m wiki_sync --env local --bot <name> --rebuild         # 换模型后全量重建

# 前台运行
.venv/bin/python feishu_claude.py

# 启动 / 重启（推荐）：服务由 systemd 托管，unit 名 feishu-bot.service
systemctl restart feishu-bot.service
# 重启后务必确认启动成功且无异常，失败则回滚：
systemctl status feishu-bot.service            # Active: active (running)
journalctl -u feishu-bot.service -n 50 --no-pager
```

> 若尚未把服务加入 systemd（无 `feishu-bot.service`），可退回用脚本重启：
> `./restart.sh local`（按 `--env` 区分 PID/日志）。

> ⚠️ **自杀式重启陷阱**：在飞书 bot 群里被调用的 Claude，自己就是 `feishu-bot.service` 的子进程，
> 直接 `systemctl restart` 会把整个 cgroup（含它自己和正在跑的 bash）一起杀掉——表现为 exit 144、
> 无任何输出，连回执都发不出去。**必须把重启交给 cgroup 之外的一次性 unit**：
>
> ```bash
> sudo -n systemd-run --collect --no-block --unit=feishu-bot-restart-$(date +%s) \
>   /bin/sh -c "sleep 25; systemctl restart feishu-bot.service"
> ```
>
> **延迟要留够**：本轮回复还在生成管道里，被杀就发不出去了。实测 LLM 自己判断用 25 秒是对的，
> 别照搬旧 `/restart` 命令的 `sleep 2`——那是同步回执（`reply_message` 返回后才 sleep）才够用。
> 起来后各群会收到「✅ 已启动」通知，一分钟内没收到就是没起来。
>
> **`/restart` 命令已于 2026-09-29 撤除**（18 天零使用；实测用自然语言让 LLM 重启每次都成功，
> 且它自己选的延迟与验证步骤比命令的硬编码更准）。阶段 2 会以 `restart_service` tool 回归，
> 届时时序挂在「回复推送完成」这个点上，延迟能缩回 2 秒且零丢回执风险。

启动成功会打印 `connected to wss://...`（每个 app_id 一条），并往 `STARTUP_NOTIFY_BOTS`
指定的群（本机=`feishu-bot` 群）推一条「✅ 已启动」通知——重启后不用盯日志，群里没收到就是没起来。

## 提交规范

commit/push 前务必自查暂存改动，避免敏感信息泄露：`git diff --cached` 扫一遍，确认没有
app_secret、密码、token、数据库凭证、真实 chat_id/app_id 等；`.env.local` 保持 gitignored
（本地实际配置不入 git），`.env` 模板只放占位符。只 `git add` 明确要提交的文件，别用
`git add -A` 顺手带上未审阅或含凭证的文件。

## 当前状态 / 待完成事项

- [ ] 填写 `.env.local` 中的真实飞书凭证
- [ ] 确认已在开放平台把订阅方式设为「长连接」并订阅 `im.message.receive_v1`
- [ ] 测试：在飞书群 @机器人 发消息，确认 Bot 能正确回复

## lark-oapi SDK 调用链（备查）

| 步骤 | SDK 调用 |
|------|------|
| 长连接接收 | `lark.ws.Client(app_id, app_secret, event_handler).start()`，事件 `im.message.receive_v1` |
| 发送/下载客户端 | `lark.Client.builder().app_id().app_secret().build()`（内部管 token） |
| 回复消息 | `client.im.v1.message.reply(ReplyMessageRequest...)` |
| 下载资源 | `client.im.v1.message_resource.get(GetMessageResourceRequest...)` |
