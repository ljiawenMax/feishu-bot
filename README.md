# feishu-bot

本地运行的服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。无需开放本地端口、也无需公网 IP：用飞书官方 SDK `lark-oapi` 的**长连接（WebSocket）**接收事件（出站连接由本机发起），群里 **@机器人** 触发。

## 架构

```
飞书群聊 @机器人 → 长连接推送事件 → 按 chat_id 路由 → 解析指令/任务 → 调用 Claude Code CLI → 回复结果到飞书
```

收到 `im.message.receive_v1` 事件后：

1. `on_event` 按 `message_id` 幂等去重（防飞书重投）后丢进 inbox（回调在 ws 事件循环里，必须快速返回）
2. inbox 线程识别是命令（`/` 开头）还是普通任务文本
3. 以 `claude -p <task> --output-format stream-json` 执行任务（长任务在 worker 线程，不阻塞接收）
4. 将结果分段（每段 ≤ 3900 字符）回复到飞书

### 代码结构（按职责分层）

| 文件 | 职责 |
|------|------|
| `feishu_claude.py` | 入口：解析配置、建 DB engine、按 `app_id` 分组建长连接并路由、管理 PID |
| `bot.py` | `Bot` 类：单个机器人的消息分发、命令处理、会话编排（业务主体） |
| `models.py` | SQLAlchemy ORM 模型：`BotState` / `Conversation`(表 sessions) / `Message` / `Upload` |
| `db.py` | 数据库层：engine/session 管理 + 基于 ORM 的数据访问 |
| `feishu_api.py` | 飞书交互层（lark-oapi SDK）：长连接与事件分发、事件归一、回复/下载、文本分段 |
| `uploads.py` | 上传文件的安全下载与存储（隔离目录、最小权限、文件名消毒） |
| `claude_runner.py` | 调用 `claude` CLI、解析 stream-json、删除磁盘 session 文件 |

依赖方向单向：`feishu_claude → bot → {db→models, feishu_api, uploads→feishu_api, claude_runner}`。
数据访问用 **SQLAlchemy ORM**（不再手写 SQL），表结构由模型定义、`create_all()` 自动建表。

### 多机器人（单进程多线程）

一个进程内可运行多个机器人——`.env.local` 的 `BOTS` 列表里每一项就是一个机器人（自己的
app 凭证、chat_id、工作目录）。长连接是「**每 app 一条、cluster 模式、不广播**」，故按
`app_id` 分组：每个 app_id 一条长连接，收到事件按 `chat_id` 路由到对应 Bot；多个群共用一个
app_id 时共享这条连接。每个 Bot 有 inbox 线程（串行处理消息）+ worker 线程（跑长任务）：

- 同一 `chat_id` 的消息**串行处理**、保序，无并发
- 不同 `chat_id` 并行、互不阻塞（一个跑长任务不影响其它）
- 共享一个数据库 engine；每次 DB 操作开短生命周期 Session，天然线程安全
- token 由 SDK 客户端内部管理（按 `app_id` 一个客户端）；数据库表按 `chat_id` 隔离
- 长连接断线期间飞书**不补发**漏掉的事件（SDK 自动重连）；进程未运行时发的消息不会补处理

## 配置文件

配置统一放 `.env.local`（不入 git；模板见仓库里的 `.env`），启动默认读它。共享项扁平，
机器人放 `BOTS` JSON 数组：

```dotenv
DB_HOST=your-mysql-host
DB_PORT=3306
DB_NAME=feishu_bot
DB_USER=feishu_bot
DB_PASSWORD=xxxxxxxx
TASK_TIMEOUT=600
BOTS=[{"name":"prod","app_id":"cli_xxx","app_secret":"xxx","chat_id":"oc_xxx","work_dirs":{"别名":"/abs/path"}}]
```

| 字段 | 说明 |
|------|------|
| `DB_*` | MySQL 连接信息，用于 session 持久化与对话审计 |
| `TASK_TIMEOUT` | 单次 Claude Code 执行超时（秒） |
| `BOTS` | JSON 数组，每项一个机器人；多机器人就追加数组项 |
| `BOTS[].name` | 机器人名（用于日志前缀，区分多机器人） |
| `BOTS[].app_id` / `app_secret` | 飞书自建应用凭证 |
| `BOTS[].chat_id` | 监听的群聊 ID（`oc_xxxxxx`），同时是数据库里区分机器人的键 |
| `BOTS[].work_dirs` | JSON 对象，`目录别名 → 绝对路径`，支持多目录切换 |

### 飞书应用配置

1. 前往 [飞书开放平台](https://open.feishu.cn) 创建企业自建应用
2. 权限管理中开启：`im:message`、`im:message.group_at_msg`、`im:resource`（下载图片/文件）；私聊再加 `im:message.p2p_msg`
3. **事件与回调 → 事件配置：订阅方式选「长连接」**，订阅事件 `im.message.receive_v1`
4. 发布应用版本并将 Bot 添加到目标群聊
5. `CHAT_ID`：飞书 PC 端打开群聊 → 右上角「…」→「复制链接」→ 链接中 `open_chat_id=oc_xxx` 的值

> 群里需 **@机器人** 才会收到事件（正文里的 @ 占位会被自动剥掉）。想「每条消息都执行」需改申请敏感权限 `im:message.group_msg`（需审批）。

## 启动与重启

```bash
# 首次安装依赖（使用项目虚拟环境）
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 后台启动（默认读 .env.local，按其中 BOTS 列表起线程）
./restart.sh

# 查看日志（单进程，所有机器人日志按 name 前缀区分）
tail -f ~/run/log/feishu-bot-local.log
```

需要多机器人时，往 `.env.local` 的 `BOTS` 数组追加配置项即可，无需起多个进程。
（仍支持 `./restart.sh <name>` 读取 `.env.<name>`，用于隔离的多套部署。）

`restart.sh <name>` 会按配置名停止旧进程（`~/run/feishu_bot/<name>.pid`）再 nohup 重启，
日志写到 `~/run/log/feishu-bot-<name>.log`。默认 `<name>` 为 `local`。

## 群聊命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示所有命令帮助 |
| `/ls` | 列出所有工作目录，回复序号切换 |
| `/sessions` | 列出当前目录的对话，回复序号切换 |
| `/new` | 在当前目录新建一个对话（清空上下文） |
| `/name <名称>` | 重命名当前对话 |
| `/del <序号>` | 删除指定对话（序号见 `/sessions`），同时删除磁盘上的 Claude session 文件 |
| `/permit` | 开关当前目录的文件读写权限（`acceptEdits`，限定当前目录，不执行命令） |
| `/model` | 选择**当前对话**使用的模型（回复序号；`/new` 新对话重置为默认） |
| `/usage` | 查看 Claude 订阅用量（5 小时窗 / 7 天利用率与重置时间） |
| `/retry` | 用当前权限重跑上一条任务 |

非命令消息直接作为任务交给 Claude Code 执行。

## 文件上传

在群里发**图片 / 文件 / 压缩包 / 音视频**，bot 会下载并存到隔离目录，然后回复保存路径。

- **存储位置**：`~/run/uploads/feishu_bot/<chat_id>/`，在所有 work_dir 与代码库之外
- **最小权限**：目录 `700`、文件 `600`，**绝不加执行位**；文件名消毒（只取 basename + 白名单字符 + 唯一前缀）防路径穿越；大小上限 50MB（`uploads.py` 的 `MAX_UPLOAD_BYTES` 可调）
- **不自动解压**压缩包（避免 zip 炸弹 / zip-slip），bot 只存不跑
- 每次上传记一行到 `uploads` 表（台账，独立于会话）
- **如何分析**：发一条任务引用回复里的路径（如「看看 /path/xxx.jpg」）。该聊天的上传目录始终经 `--add-dir` 纳入 Claude 工作区，所以**只读分析无需 `/permit`**；若要让 Claude 据此**改写文件**，再开 `/permit`（仍不会执行命令）
- **富文本（post）消息**：飞书富文本（含截图+文字的那种）会被解析——文字作为任务指令，内嵌图片自动下载并把路径拼进 prompt 交给 Claude，因此「发一张文字截图 + 帮我翻译」这类能直接出结果（读图属只读，无需 `/permit`）
- 暂不支持的消息类型（表情/位置/合并转发等）：不处理，但会**记一行到 `unhandled_messages` 表**（含类型与原始内容）并回复「暂不支持…，已记录」，不再无声丢弃

## Session 持久化与对话历史

- 每个工作目录独立维护 session 列表，支持创建多个对话并按序号切换
- **session 状态持久化在 MySQL**（`sessions` 表，一行一个对话），重启后 `/sessions` 仍能列出旧对话、`/name` 标签保留、并能 `--resume` 回原 Claude session
- Session 失效时（Claude Code 报 `No conversation found`），自动将最近对话历史拼入 prompt 重建 context，无感恢复
- 切换 / 重启时从数据库读取该对话最近 40 条（20 轮）历史用于 context 恢复
- `/del` 删除对话时会同时删除：数据库中的 session 行、磁盘上的 Claude session 文件（`~/.claude/projects/{编码目录}/{session_id}.jsonl` 及 subagents 目录）

## 数据库表

表由 `models.py` 的 ORM 模型定义，启动时 `Base.metadata.create_all()` 自动建表（对已存在的表幂等跳过），无需手动迁移：

| 表（ORM 类） | 用途 |
|----|------|
| `bot_state`（`BotState`） | 每个 `chat_id` 的 UI 状态：当前目录、各目录权限模式 |
| `sessions`（`Conversation`） | 一行一个对话，代理主键 `id`；`claude_session_id` 在首次执行前为 NULL |
| `messages`（`Message`） | 对话审计日志，每轮用户输入与 LLM 输出各一行（含实际所用 `model`），append-only |
| `uploads`（`Upload`） | 上传文件台账：消息 id、路径、文件名、大小、Content-Type |
| `unhandled_messages`（`Unhandled`） | 处理不了的消息留痕：消息 id、类型、原始内容 |

## 对话审计

每轮对话（用户输入 + Claude 完整输出）都会写入 `messages` 表，含 `is_error` / `timed_out` 标记。删除 session 时审计记录**保留**（外键 `ON DELETE SET NULL`，靠冗余的 `chat_id` / `dir_name` / `claude_session_id` 仍可追溯）。直接用 SQL 查询：

```sql
-- 某个 Claude session 的完整对话
SELECT role, content, created_at FROM messages
WHERE claude_session_id = '...' ORDER BY id;

-- 某个群某目录最近的对话
SELECT role, left(content, 80), created_at FROM messages
WHERE chat_id = 'oc_xxx' AND dir_name = 'feishu-bot'
ORDER BY id DESC LIMIT 20;
```

## 模型选择

- `/model` 列出可选模型，回复序号选择——**作用于当前对话**（按 session），`/new` 新对话重置为「默认（不指定）」
- 选定后任务以 `claude --model <名称>` 运行；未选则用 Claude Code 默认模型
- 可选清单由 `.env.local` 的 `MODELS`（JSON 数组）配置，默认 `["opus","sonnet","haiku"]`
- 每次任务**实际所用模型**（从 stream-json 的 assistant 事件捕获）会：① 在回复末尾显示「（模型：…）」；② 写入 `messages.model` 审计列

## 订阅用量

`/usage` 查看 Claude 订阅（Pro/Max）的实时用量：5 小时窗、7 天的利用率百分比与重置时间。

- 数据来源：官方端点 `GET https://api.anthropic.com/api/oauth/usage`（Claude Code 交互式 `/usage` 背后的同一接口）
- 需要本机**已登录过 claude**（读取 `~/.claude/.credentials.json` 的 OAuth token；token 仅本机直连 Anthropic 官方端点）
- 该端点未公开且限流很严：本命令**按需调用 + 60s 内复用缓存**，绝不轮询；被限流（429）时回退显示上次结果
- token 一般是新鲜的（bot 频繁跑 `claude`，Claude Code 会自动刷新凭证）；若过期会提示在终端跑一次 claude 刷新

## 权限模式

权限按目录独立记录（`/permit` 切换当前目录），分两档：

- **关（默认）**：Claude Code `default` 模式，只读——能读当前 work_dir 与该聊天上传目录里的文件，但不能写、不能执行命令
- **开**：`--permission-mode acceptEdits`——可在**当前 work_dir 与该聊天上传目录内**新建/修改文件（Write+Edit 都放行），但**仍不能执行任意命令**

设计要点：Claude Code 能把文件操作限定在工作区（cwd + `--add-dir`），但无法把 shell 命令限定在目录（命令以系统用户身份运行）。因此本项目**不再使用** `--dangerously-skip-permissions`（会放开整个文件系统与命令执行），`/permit` 最高只到 `acceptEdits`——文件操作锁在当前目录、永不执行任意命令。该聊天上传目录始终经 `--add-dir` 纳入工作区，便于读取上传文件分析。

## 长连接（WebSocket）接收

- 用飞书官方 SDK `lark-oapi` 的 `lark.ws.Client` 建长连接、订阅 `im.message.receive_v1` 事件；连接由本机出站发起，无需公网 IP，无需开端口
- 长连接「每 app 一条、cluster 模式、不广播」——同一 `app_id` 只能有一条有效连接，多开只有随机一条能收到事件；本项目按 `app_id` 分组保证一条
- 事件可能重复投递，`on_event` 按 `message_id` 幂等去重
- 断线由 SDK 自动重连；断线期间飞书**不补发**漏掉的事件，进程未运行时发的消息不会补处理
- 启动成功会打印 `connected to wss://...`

## 文件结构

```
feishu_claude.py   # 入口：解析配置 + 建 engine + 起线程 + PID 管理
bot.py             # Bot 类：单机器人消息分发、命令处理、会话编排
models.py          # SQLAlchemy ORM 模型（BotState/Conversation/Message/Upload）
db.py              # 数据库层：engine/session + ORM 数据访问
feishu_api.py      # 飞书交互层（lark-oapi SDK）：长连接/事件归一/回复/下载/文本分段
uploads.py         # 上传文件安全下载与存储（隔离目录、最小权限）
claude_runner.py   # 调用 claude CLI + 删除磁盘 session 文件
restart.sh         # 按配置名停止旧进程并重启
.env               # 配置模板（入 git，值留空）
.env.local         # 实际配置（不入 git）：DB + BOTS 列表
~/run/uploads/feishu_bot/<chat_id>/   # 上传文件隔离目录（700）
~/run/feishu_bot/<name>.pid    # PID 文件（默认 local.pid）
~/run/log/feishu-bot-<name>.log   # 运行日志（默认 feishu-bot-local.log）
```
