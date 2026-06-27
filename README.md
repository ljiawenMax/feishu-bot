# feishu-claude-bot

本地运行的轮询服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。无需开放本地端口，采用主动轮询方式拉取消息。

## 架构

```
飞书群聊 → 轮询拉取消息 → 解析指令/任务 → 调用 Claude Code CLI → 回复结果到飞书
```

核心脚本 `feishu_claude.py` 在一个循环中：

1. 调用飞书 API 拉取最新群消息（带自适应退避，闲置时最长 `POLL_INTERVAL` 秒）
2. 识别是命令（`/` 开头）还是普通任务文本
3. 以 `claude -p <task> --output-format stream-json` 执行任务
4. 将结果分段（每段 ≤ 3900 字符）回复到飞书

## 配置文件

使用 `.env.<env>` 格式的文件，默认读 `.env.prod`：

```dotenv
APP_ID=cli_xxxxxxxx
APP_SECRET=xxxxxxxxxxxxxxxx
CHAT_ID=oc_xxxxxxxxxxxxxxxx
WORK_DIRS={"daily-assistant":"/home/user/projects/assistant","web-project":"/home/user/projects/web"}
POLL_INTERVAL=60
TASK_TIMEOUT=300
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=financial_report
DB_USER=admin
DB_PASSWORD=xxxxxxxx
```

| 字段 | 说明 |
|------|------|
| `APP_ID` / `APP_SECRET` | 飞书自建应用凭证 |
| `CHAT_ID` | 监听的群聊 ID（格式 `oc_xxxxxx`），同时作为数据库中区分群/环境的键 |
| `WORK_DIRS` | JSON 对象，`目录别名 → 绝对路径`，支持多目录切换 |
| `POLL_INTERVAL` | 空闲时最大轮询间隔（秒），有消息时退回 5s |
| `TASK_TIMEOUT` | 单次 Claude Code 执行超时（秒） |
| `DB_*` | PostgreSQL 连接信息，用于 session 持久化与对话审计 |

### 飞书应用配置

1. 前往 [飞书开放平台](https://open.feishu.cn) 创建企业自建应用
2. 权限管理中开启：`im:message`、`im:message.group_at_msg`
3. 发布应用并将 Bot 添加到目标群聊
4. `CHAT_ID`：飞书 PC 端打开群聊 → 右上角「…」→「复制链接」→ 链接中 `open_chat_id=oc_xxx` 的值

## 启动与重启

```bash
# 首次安装依赖（使用项目虚拟环境）
python -m venv .venv && .venv/bin/pip install requests psycopg2-binary

# 后台启动（prod 环境）
./restart.sh

# 指定 test 环境
./restart.sh test

# 查看日志
tail -f ~/run/log/feishu-bot.log
```

`restart.sh` 会自动停止旧进程（通过 `~/run/feishu_bot/run.pid`），再以 nohup 启动新进程。

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

## Session 持久化与对话历史

- 每个工作目录独立维护 session 列表，支持创建多个对话并按序号切换
- **session 状态持久化在 PostgreSQL**（`sessions` 表，一行一个对话），重启后 `/sessions` 仍能列出旧对话、`/name` 标签保留、并能 `--resume` 回原 Claude session
- Session 失效时（Claude Code 报 `No conversation found`），自动将最近对话历史拼入 prompt 重建 context，无感恢复
- 切换 / 重启时从数据库读取该对话最近 40 条（20 轮）历史用于 context 恢复
- `/del` 删除对话时会同时删除：数据库中的 session 行、磁盘上的 Claude session 文件（`~/.claude/projects/{编码目录}/{session_id}.jsonl` 及 subagents 目录）

## 数据库表

启动时自动建表（`CREATE TABLE IF NOT EXISTS`），无需手动迁移：

| 表 | 用途 |
|----|------|
| `bot_state` | 每个 `chat_id` 的 UI 状态：当前目录、各目录权限模式 |
| `sessions` | 一行一个对话，代理主键 `id`；`claude_session_id` 在首次执行前为 NULL |
| `messages` | 对话审计日志，每轮用户输入与 LLM 输出各一行，append-only |

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
feishu_claude.py   # 主服务脚本
restart.sh         # 停止旧进程并重新启动
.env.prod          # 生产环境配置（不入 git）
.env.test          # 测试环境配置（不入 git）
~/run/feishu_bot/run.pid   # 运行时 PID 文件
~/run/log/feishu-bot.log   # 运行日志
```
