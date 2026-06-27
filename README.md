# feishu-claude-bot

本地运行的轮询服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。无需开放本地端口，采用主动轮询方式拉取消息。

## 架构

```
飞书群聊 → 轮询拉取消息 → 解析指令/任务 → 调用 Claude Code CLI → 回复结果到飞书
```

`Bot` 在一个循环中：

1. 调用飞书 API 拉取最新群消息（带自适应退避，闲置时最长 `POLL_INTERVAL` 秒）
2. 识别是命令（`/` 开头）还是普通任务文本
3. 以 `claude -p <task> --output-format stream-json` 执行任务
4. 将结果分段（每段 ≤ 3900 字符）回复到飞书

### 代码结构（按职责分层）

| 文件 | 职责 |
|------|------|
| `feishu_claude.py` | 入口：解析配置、建 DB engine、按 `BOTS` 列表为每个机器人起线程、管理 PID |
| `bot.py` | `Bot` 类：单个机器人的消息分发、命令处理、会话编排（业务主体） |
| `models.py` | SQLAlchemy ORM 模型：`BotState` / `Conversation`(表 sessions) / `Message` / `Upload` |
| `db.py` | 数据库层：engine/session 管理 + 基于 ORM 的数据访问 |
| `feishu_api.py` | 飞书 API：获取 token、拉取/回复消息、下载资源、文本分段与构造 |
| `uploads.py` | 上传文件的安全下载与存储（隔离目录、最小权限、文件名消毒） |
| `claude_runner.py` | 调用 `claude` CLI、解析 stream-json、删除磁盘 session 文件 |

依赖方向单向：`feishu_claude → bot → {db→models, feishu_api, uploads→feishu_api, claude_runner}`。
数据访问用 **SQLAlchemy ORM**（不再手写 SQL），表结构由模型定义、`create_all()` 自动建表。

### 多机器人（单进程多线程）

一个进程内可运行多个机器人——`.env.local` 的 `BOTS` 列表里每一项就是一个机器人（自己的
app 凭证、chat_id、工作目录）。每个机器人在**自己的线程**里跑独立的轮询循环：

- 同一 `chat_id` 的消息在该机器人线程里**串行处理**，无并发
- 不同机器人并行、互不阻塞（一个机器人跑长任务不影响其它机器人）
- 共享一个数据库 engine；每次 DB 操作开短生命周期 Session，天然线程安全
- token 缓存按 `app_id` 分键；数据库三张表按 `chat_id` 隔离

## 配置文件

配置统一放 `.env.local`（不入 git；模板见仓库里的 `.env`），启动默认读它。共享项扁平，
机器人放 `BOTS` JSON 数组：

```dotenv
DB_HOST=your-mysql-host
DB_PORT=3306
DB_NAME=feishu_bot
DB_USER=feishu_bot
DB_PASSWORD=xxxxxxxx
POLL_INTERVAL=5
TASK_TIMEOUT=600
BOTS=[{"name":"prod","app_id":"cli_xxx","app_secret":"xxx","chat_id":"oc_xxx","work_dirs":{"别名":"/abs/path"}}]
```

| 字段 | 说明 |
|------|------|
| `DB_*` | MySQL 连接信息，用于 session 持久化与对话审计 |
| `POLL_INTERVAL` | 空闲时最大轮询间隔（秒），有消息时退回 5s |
| `TASK_TIMEOUT` | 单次 Claude Code 执行超时（秒） |
| `BOTS` | JSON 数组，每项一个机器人；多机器人就追加数组项 |
| `BOTS[].name` | 机器人名（用于日志前缀，区分多机器人） |
| `BOTS[].app_id` / `app_secret` | 飞书自建应用凭证 |
| `BOTS[].chat_id` | 监听的群聊 ID（`oc_xxxxxx`），同时是数据库里区分机器人的键 |
| `BOTS[].work_dirs` | JSON 对象，`目录别名 → 绝对路径`，支持多目录切换 |

### 飞书应用配置

1. 前往 [飞书开放平台](https://open.feishu.cn) 创建企业自建应用
2. 权限管理中开启：`im:message`、`im:message.group_at_msg`
3. 发布应用并将 Bot 添加到目标群聊
4. `CHAT_ID`：飞书 PC 端打开群聊 → 右上角「…」→「复制链接」→ 链接中 `open_chat_id=oc_xxx` 的值

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
| `/permit` | 开关当前目录的写文件/执行命令权限（对应 `--dangerously-skip-permissions`） |
| `/retry` | 用当前权限重跑上一条任务 |

非命令消息直接作为任务交给 Claude Code 执行。

## 文件上传

在群里发**图片 / 文件 / 压缩包 / 音视频**，bot 会下载并存到隔离目录，然后回复保存路径。

- **存储位置**：`~/run/feishu_bot/uploads/<chat_id>/`，在所有 work_dir 与代码库之外
- **最小权限**：目录 `700`、文件 `600`，**绝不加执行位**；文件名消毒（只取 basename + 白名单字符 + 唯一前缀）防路径穿越；大小上限 50MB（`uploads.py` 的 `MAX_UPLOAD_BYTES` 可调）
- **不自动解压**压缩包（避免 zip 炸弹 / zip-slip），bot 只存不跑
- 每次上传记一行到 `uploads` 表（台账，独立于会话）
- **如何分析**：想让 Claude 处理上传的文件，发一条任务引用回复里的路径（如「解压并看看 /path/xxx.zip」），需先 `/permit` 开权限——执行闸门在你手里
- 暂不支持的消息类型（表情/位置/合并转发等）会被忽略

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
| `messages`（`Message`） | 对话审计日志，每轮用户输入与 LLM 输出各一行，append-only |
| `uploads`（`Upload`） | 上传文件台账：消息 id、路径、文件名、大小、Content-Type |

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

## 权限模式

默认情况下 Claude Code 以只读方式运行（不传 `--dangerously-skip-permissions`）。发送 `/permit` 开启写文件/执行命令权限，再次发送 `/permit` 关闭。权限模式按目录独立记录。

## 自适应轮询与休眠恢复

- 有新消息时轮询间隔重置为 5s；无消息时每次乘以 4 直到 `POLL_INTERVAL` 上限
- 检测到距上次轮询超过 60s（电脑休眠），自动跳过积压消息，避免重复执行历史任务

## 文件结构

```
feishu_claude.py   # 入口：解析配置 + 建 engine + 起线程 + PID 管理
bot.py             # Bot 类：单机器人消息分发、命令处理、会话编排
models.py          # SQLAlchemy ORM 模型（BotState/Conversation/Message/Upload）
db.py              # 数据库层：engine/session + ORM 数据访问
feishu_api.py      # 飞书 API：token / 拉取 / 回复 / 下载资源 / 文本构造
uploads.py         # 上传文件安全下载与存储（隔离目录、最小权限）
claude_runner.py   # 调用 claude CLI + 删除磁盘 session 文件
restart.sh         # 按配置名停止旧进程并重启
.env               # 配置模板（入 git，值留空）
.env.local         # 实际配置（不入 git）：DB + BOTS 列表
~/run/feishu_bot/uploads/<chat_id>/   # 上传文件隔离目录（700）
~/run/feishu_bot/<name>.pid    # PID 文件（默认 local.pid）
~/run/log/feishu-bot-<name>.log   # 运行日志（默认 feishu-bot-local.log）
```
