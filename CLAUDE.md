# feishu-claude-bot

## 项目目标

本地运行的轮询服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。

**不需要开放本地端口**，采用主动轮询方式拉取飞书消息。

## 文件说明

代码按职责拆为 6 个模块（依赖单向：`feishu_claude → bot → {db→models, feishu_api, claude_runner}`）：

- `feishu_claude.py` — 入口：解析 `.env.<name>`、建 DB engine、为 `BOTS` 列表每项起一个线程、管理 PID
- `bot.py` — `Bot` 类：单个机器人的消息分发、命令处理、会话编排（业务主体，改功能主要看这里）
- `models.py` — SQLAlchemy ORM 模型：`BotState` / `Conversation`(表 sessions) / `Message` / `Upload`(表 uploads)
- `db.py` — 数据库层：engine/session 管理 + 基于 ORM 的数据访问（不再手写 SQL）
- `feishu_api.py` — 飞书 API：token、拉取/回复消息、下载资源、文本分段与构造
- `uploads.py` — 上传图片/文件/压缩包的安全下载与存储（隔离目录 700、文件 600、文件名消毒、不自动解压）
- `claude_runner.py` — 调用 `claude` CLI、解析 stream-json、删除磁盘 session 文件
- `.env` — 配置模板（入 git）；`.env.local` — 实际配置（不入 git）

配置统一为 `.env.local`（默认），含共享项 + `BOTS` JSON 列表。多机器人：**单进程多线程**，
每个机器人（`BOTS` 一项，一个 chat_id）一个轮询线程 + 一个后台 worker。**同一 chat_id 串行保序、
不同 chat_id 并行**；所有机器人共享一个全局并发闸（`MAX_CONCURRENT`，`threading.BoundedSemaphore`），
限制同时运行的 claude 进程数，防止内存被打满。DB 访问走 SQLAlchemy ORM；每次操作开短生命周期
Session（线程安全），状态按 `chat_id` 隔离。

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
  "work_dir": "Claude Code 执行任务的工作目录（绝对路径，可省略→家目录 ~）"
}
```

`poll_interval` / `task_timeout` / `heartbeat_interval` / `models` 为共享项，写在 `.env.local` 顶层。

## 飞书开放平台前置配置

1. 前往 https://open.feishu.cn 创建企业自建应用
2. 权限管理中开启：`im:message`、`im:message.group_at_msg`
3. 发布应用，将 Bot 添加到目标群聊
4. 从「凭证与基础信息」获取 `app_id` 和 `app_secret`
5. `chat_id`：飞书 PC 端打开群聊 → 右上角「…」→「复制链接」→ 链接中 `open_chat_id=oc_xxx` 的值

## 启动方式

```bash
# 安装依赖（仅首次）
pip3 install requests

# 前台运行
python3 feishu_claude.py

# 后台运行（推荐）
nohup python3 feishu_claude.py > /tmp/feishu-bot.log 2>&1 &
```

## 当前状态 / 待完成事项

- [ ] 填写 `config.json` 中的真实飞书凭证
- [ ] 验证飞书应用权限是否配置正确
- [ ] 测试：在飞书群发消息，确认 Bot 能正确回复

## 飞书 API 调用链（备查）

| 步骤 | 方法 | 端点 |
|------|------|------|
| 获取 token | POST | `/open-apis/auth/v3/tenant_access_token/internal` |
| 拉取消息 | GET | `/open-apis/im/v1/messages` |
| 回复消息 | POST | `/open-apis/im/v1/messages/{message_id}/reply` |
