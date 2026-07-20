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
- `gitsync.py` — 增量同步：把每个 chat 工作目录「上次同步以来」的改动打包成增量 `git bundle`（供远程 `git fetch` + `merge --ff-only`；`/resync` 用全量 bundle + `reset --hard` 兜底恢复）
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
  "sync_patch": "可选布尔（默认 false）：每轮任务成功后自动把增量 .bundle 发到群，供远程 git fetch"
}
```

**增量 bundle 同步（gitsync）**：`work_dir` 是 git 仓库时可用。每个 chat 维护隐藏基线 ref
`refs/feishu-sync/<chat_id>`（游离快照，不碰 HEAD/分支/历史/暂存区）；每轮改动把「基线→当前工作树」
打包成 `git bundle`（尊重 .gitignore），带序号发到群，发完基线前移，故下轮只含新增改动、远程按序
`git fetch` 后 `merge --ff-only` 逐轮叠加。`sync_patch=true` 开启自动发送；`/sync` 命令随时手动发送
（不受开关限制）。本机 commit 与否都不影响（快照抓工作树内容而非 HEAD）。前提：远程与本机同版本、
只被动接收同步文件——一旦这个前提被打破（远程曾脱离 gitsync 被整体覆盖过等），`merge --ff-only` 会
明确报 "not possible to fast-forward" 而不是像旧版 `git apply` 那样含糊报错；此时发 `/resync`，会打包
一份不依赖增量基线、以本机当前工作树为准的全量 bundle，远程 `fetch` 后 `reset --hard` 即可强制拉平、
重新开始计增量。之所以从 `git diff`/`git apply` 换成 bundle：patch 是纯文本，靠上下文行匹配应用，一旦
基线偏了会在多个文件上同时出现「文件已存在」「上下文对不上」等含糊报错，很难定位是哪一轮出的问题；
bundle 传的是真实 git 对象，`fetch`/`merge --ff-only` 要么整体成功要么明确失败，配合 `/resync` 能可靠
自愈。

`task_timeout` / `heartbeat_interval` / `models` / `max_concurrent` 为共享项，写在 `.env.local` 顶层。
（`POLL_INTERVAL` 已废弃：改用长连接推送，不再轮询；保留仅为兼容旧配置。）

## 飞书开放平台前置配置

1. 前往 https://open.feishu.cn 创建企业自建应用
2. 权限管理中开启：`im:message`（发消息）、`im:message.group_at_msg`（收群内@机器人的消息）、
   `im:resource`（下载消息里的图片/文件）；如需私聊再加 `im:message.p2p_msg`
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
