# feishu-claude-bot

## 项目目标

本地运行的服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。

**不需要开放本地端口，也不需要公网 IP**：采用飞书官方 SDK `lark-oapi` 的**长连接（WebSocket）**
接收事件——连接由本机主动向飞书发起（出站），只需能访问公网即可。群里 **@机器人** 触发。

## 文件说明

代码按职责拆为 6 个模块（依赖单向：`feishu_claude → bot → {db→models, feishu_api, claude_runner}`）：

- `feishu_claude.py` — 入口：解析 `.env.<name>`、建 DB engine、**按 app_id 分组建长连接**、管理 PID
- `bot.py` — `Bot` 类：单个机器人的消息分发、命令处理、会话编排（业务主体，改功能主要看这里）
- `models.py` — SQLAlchemy ORM 模型：`BotState` / `Conversation`(表 sessions) / `Message` / `Upload`(表 uploads)
- `db.py` — 数据库层：engine/session 管理 + 基于 ORM 的数据访问（不再手写 SQL）
- `feishu_api.py` — 飞书交互层（基于 lark-oapi SDK）：长连接客户端与事件分发、事件归一、回复/下载、文本分段
- `uploads.py` — 上传图片/文件/压缩包的安全下载与存储（隔离目录 700、文件 600、文件名消毒、不自动解压）
- `gitsync.py` — 增量同步：把每个 chat 工作目录的改动**在本地真实提交**（commit message = 本轮飞书任务内容），再把「上次同步以来」的提交打包成增量 `git bundle`（供远程 `git fetch` + `merge --ff-only`；`/resync` 用全量 bundle + `reset --hard` 兜底恢复）
- `wiki_sync.py` — 飞书知识库 → 本地 markdown 镜像：列知识空间、递归走节点树、docx **block → markdown**（保留标题层级）、按 `obj_edit_time` 增量同步；也可作自检 CLI 独立运行
- `wiki_index.py` — 知识库索引与检索：按标题小节切块、ollama 取向量、**向量 + MySQL ngram 全文双路 RRF 混合召回**；刻意不依赖 Bot/飞书，将来可整个文件搬走
- `claude_runner.py` — 调用 `claude` CLI、解析 stream-json、删除磁盘 session 文件
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

**一 chat_id 一 work_dir**：每个机器人绑定一个群（chat_id）+ 一个工作目录（`work_dir` 单字符串，
省略回退家目录 `~`），chat_id 唯一。视觉隔离靠分群；一个群内用 `/new`、`/sessions` 手动管理多个对话
（不在群内切换目录）。会话与 `permit`/`unsafe` 权限均按 `chat_id` 分区，持久化在 `bot_state` 表。

## BOTS 每项字段说明（`.env.local` 里的 `BOTS` JSON 列表）

```json
{
  "name": "机器人名称（日志标识）",
  "app_id": "飞书应用 App ID",
  "app_secret": "飞书应用 App Secret",
  "chat_id": "监听的群聊 ID（格式 oc_xxxxxx，唯一）",
  "work_dir": "Claude Code 执行任务的工作目录（绝对路径，可省略→家目录 ~）",
  "sync_patch": "可选布尔（默认 false）：每轮任务成功后自动把增量 .bundle 发到群，供远程 git fetch",
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
+ `reply_file`）。**白名单是唯一的外传闸门**（`Bot._extract_send_files`）：必须是绝对路径、
realpath 落在 `work_dir` 或本 chat 上传目录之下（软链与 `..` 都逃不出去）、是普通文件、
单文件 ≤20MB、一条回复最多 3 个；被拒的会在正文末尾列出原因。放宽它等于允许被上传文件
里的内容诱导 Claude 外传任意路径，别动。

**增量 bundle 同步（gitsync）**：`work_dir` 是 git 仓库时可用。**同步 = 先在本地当前分支真提交，
再打包真实历史**：每轮先 `git add -A` + `git commit`（message 首行 = 本轮飞书任务正文，正文标注
`via: feishu-sync` 与发言人 open_id；`/sync <说明>` 可手动指定），因此本机和远程的 `git log`
完全一致、都能看出每轮改了什么、是谁触发的，群消息里也会列出本轮打包的提交摘要。提交走
`--no-verify` + 关 gpg 签名（同步不该被 pre-commit hook 或签名配置阻断），用户配了
`user.name/email` 就用用户身份、没配才回退 `feishu-sync` 身份。**代价：会写入你的提交历史，
且 `add -A` 会把暂存区一并提交**（旧版造游离快照、不碰 HEAD，代价是本机 `git log` 查不到任何
同步记录、远程只收到一条 message 全一样的匿名快照链，本机自己的 commit 反而被压平丢失）。
每个 chat 维护隐藏基线 ref `refs/feishu-sync/<chat_id>` 指向「上次已发送的提交」，每轮打包
`基线..HEAD`（尊重 .gitignore），带序号发到群，发完基线前移，故下轮只含新增提交、远程按序
`git fetch` 后 `merge --ff-only` 逐轮叠加；你自己手动做的 commit 也会一并同步过去。
`sync_patch=true` 开启自动发送；`/sync` 命令随时手动发送（不受开关限制）。基线若不是当前 HEAD
的祖先（本机 rebase/reset/切分支改写过历史），增量已无法快进，会直接报错要求改发 `/resync`，
而不是发出一个远程必然 ff 失败的 bundle（此时本轮改动已提交到本地、不会丢）。前提：远程与本机同版本、
只被动接收同步文件——一旦这个前提被打破（远程曾脱离 gitsync 被整体覆盖过等），`merge --ff-only` 会
明确报 "not possible to fast-forward" 而不是像旧版 `git apply` 那样含糊报错；此时发 `/resync`，会打包
一份不依赖增量基线、以本机当前工作树为准的全量 bundle，远程 `fetch` 后 `reset --hard` 即可强制拉平、
重新开始计增量。之所以从 `git diff`/`git apply` 换成 bundle：patch 是纯文本，靠上下文行匹配应用，一旦
基线偏了会在多个文件上同时出现「文件已存在」「上下文对不上」等含糊报错，很难定位是哪一轮出的问题；
bundle 传的是真实 git 对象，`fetch`/`merge --ff-only` 要么整体成功要么明确失败，配合 `/resync` 能可靠
自愈。

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
  共享一份索引。**缓存目录必须在 `work_dir` 之外**——gitsync 每轮 `git add -A`，镜像落进
  work_dir 会被整个提交进你的 git 历史。
- **注入只改 `run_text`，不进 history**：审计与压缩摘要仍用用户原文，否则历史会被资料撑爆、
  摘要也跟着跑偏。代价是 `--resume` 续聊会让历轮资料在 session 里累积，**context 比不开知识库
  涨得快**，靠 `AUTO_COMPACT_THRESHOLD` 自动压缩兜底；每轮注入上限为 Top 5 × 3000 字。
- 同步：启动时一次 + 后台每 `WIKI_SYNC_INTERVAL` 秒增量一次（按 `obj_edit_time` 只重拉变更
  文档，只对变更文档重新编码）。`/wiki sync` 随时手动触发。飞书云文档有 `drive.file.edit_v1`
  事件可做秒级刷新，但要逐文件订阅、且新增节点无事件、断线不补发，故只能当加速器，定时扫描
  必须保留（本版未做事件订阅）。
- 前置：开放平台开 `wiki:wiki:readonly` + `docx:document:readonly`，并且**必须把机器人应用
  加为该知识库的成员**（知识库 → 设置 → 成员管理 → 添加应用）。不加成员时飞书**不报权限错，
  只返回空列表**——所以判据是：`/wiki spaces` 或 `--spaces` 返回空 = 还没加成员，不是代码问题。
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

`task_timeout` / `heartbeat_interval` / `models` / `max_concurrent` / `auto_compact_threshold`
/ `wiki_cache_dir` / `wiki_sync_interval` / `wiki_embed_url` / `wiki_embed_model`
为共享项，写在 `.env.local` 顶层。

**context 护栏（省额度）**：每轮 `--resume` 都会把整段对话重喂给模型，忘了 `/new` 时 context
越滚越大、每轮重复烧订阅额度。为此：①每条回复 inline 附带 `ctx N%`，越阈值（60/80%）追加分级提醒；
②续聊前若 context 占用达 `AUTO_COMPACT_THRESHOLD`（默认 75%，0 关闭）自动压缩——把本地历史用
便宜模型（haiku）摘要成一段上下文、开新对话（`/new`）并把摘要作为新会话首轮 prompt 的前缀带上，
从而清空 context 重新计费又不丢脉络；③关掉自动压缩时，92% 硬闸兜底：触顶暂停执行、要求手动
`/compact` 或 `/new`。`/compact` 命令随时手动触发同样的「摘要→开新对话→下轮带摘要」流程。
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

启动成功会打印 `connected to wss://...`（每个 app_id 一条）。

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
