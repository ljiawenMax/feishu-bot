# feishu-bot

本机运行的飞书机器人：通过官方 SDK `lark-oapi` 长连接接收群里的 @消息，交给 **Codex CLI** 执行，再将结果回复到飞书。模型通过 **ChatGPT 订阅登录**使用；本项目不配置 OpenAI API key。

```
飞书群 @机器人 → WebSocket → Bot inbox → worker → codex exec → 飞书回复
                                      ↕ MySQL 会话与审计
```

同一群的任务串行执行，不同群可并行，`MAX_CONCURRENT` 控制全局并发。每个 `app_id` 只建一条长连接，按 `chat_id` 分发；重启或断线期间的飞书事件不会补发。

## 安装与登录

```bash
npm install -g @openai/codex
codex login
codex login status  # 应显示 Logged in using ChatGPT

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

在**运行服务的系统用户**下登录，沿用该用户的 Codex 配置与订阅。无浏览器的服务器可运行 `codex login --device-auth`。CLI 负责凭证存储与刷新，项目不读取或转发 OAuth token。

GPT 模型沿用 `~/.codex/config.toml` 的 `model` 设置；不设置时用 Codex 默认模型。模型是否可用取决于账户权限。本机迁移验证使用 `gpt-6.1-sol`。旧的 `MODELS` 配置不再使用，旧会话的 `opus` / `sonnet` / `haiku` / `fable` 选择会清除。

参考：[官方认证说明](https://learn.chatgpt.com/docs/auth)、[非交互执行](https://learn.chatgpt.com/docs/non-interactive-mode)、[App Server](https://learn.chatgpt.com/docs/app-server)。

## 配置与启动

复制 `.env` 模板到 `.env.local`，填写 MySQL 与飞书应用信息。配置不入 git；`BOTS` 支持多行 JSON。

```dotenv
DB_HOST=your-mysql-host
DB_PORT=3306
DB_NAME=feishu_bot
DB_USER=feishu_bot
DB_PASSWORD=xxxxxxxx
TASK_TIMEOUT=600
MAX_CONCURRENT=3
HEARTBEAT_INTERVAL=60
AUTO_COMPACT_THRESHOLD=75
BOTS=[{"name":"prod","app_id":"cli_xxx","app_secret":"xxx","chat_id":"oc_xxx","work_dir":"/abs/path","extra_dirs":["/abs/reference"]}]
```

每个机器人绑定一个群和一个工作目录。`work_dir` 支持 `~`，省略时用家目录；`extra_dirs` 提供参考资料路径，不额外授予写权限。可选 `wiki` 字段配置飞书知识库检索，参数见 `.env`。

飞书应用需开通消息及资源权限，选择长连接订阅 `im.message.receive_v1`，发布版本并把机器人加入目标群。

```bash
./restart.sh                 # nohup 部署；默认读取 .env.local
./restart.sh <name>          # 读取 .env.<name>
tail -f ~/run/log/feishu-bot-local.log
```

已通过 systemd 部署的实例应使用 `sudo systemctl restart feishu-bot.service`，不要同时再起一个 nohup 进程。入口文件 `feishu_claude.py` 保留原名称，兼容已有启动脚本和 systemd unit。维护时可临时设置 `STARTUP_SILENT=true` 跳过启动群通知，之后的断线恢复通知仍按原配置发送。

## 群聊命令

| 命令 | 说明 |
|---|---|
| `/help` | 显示命令帮助 |
| `/sessions` | 列出本群对话，回复序号切换 |
| `/new` | 新建对话，清空上下文 |
| `/name <名称>` | 重命名当前对话 |
| `/del <序号>` | 删除指定对话；接管的本机会话只解绑 |
| `/allsessions [关键词]` | 列出本机 Codex 会话，回复序号接管续聊 |
| `/permit` | 切换工作区写权限，允许在 Codex 沙箱内运行命令 |
| `/unsafe` | 切换完整文件系统与命令执行权限 |
| `/usage` | 查询 ChatGPT / Codex 订阅额度窗口与重置时间 |

其他需求直接用自然语言，例如分析附件、导出文件、检索知识库、压缩上下文或让正反方辩论。
`/allsessions`、`/name`、`/permit` 按机主要求保留；`/debate` 编排已迁到 skill，命令总数为 9。

## 自然语言能力

`fbtool` 是纯标准库 CLI；执行子进程继承本轮的群、消息、socket 路径和短期令牌，
请求通过 Unix socket 交回 bot 进程，复用已加载的 SDK client。socket 文件权限 600，
令牌在任务结束后失效；跨群/跨消息请求被拒绝。CLI 不读取飞书凭证。

可用入口：`send_file`、`send_image`、`send_text`、`wiki_search`、`history_search`、
`list_uploads`、`git_local_changes`、`compact_context`、`restart_service`、`speak`。
文件标记仍兼容，工具发送与最终标记共用每轮 3 个附件的总额。

知识库在工具服务启动后改为按需检索，后台增量同步仍保留；查询结果应注明实际引用来源。
历史查询只查当前群，最多返回 20 条；上传台账最多返回 50 条，另返回未消费且未过期的缓冲快照。
压缩工具在本轮完成后执行，下一轮使用摘要；直接发送「压缩一下上下文」会绕过 context 硬闸执行压缩。摘要和新会话一起持久化，首次续聊前重启也能恢复。重启要求 `/unsafe`，仅登记意图，
最终回复成功送达后才由独立 transient unit 重启；任务失败或回执失败会取消。
因此无需让模型重复拼装 `systemd-run`。在本服务子进程中直接运行 `systemctl restart`
会杀掉当前执行及其回复路径，应使用 `restart_service`。

技能源文件位于 `.agents/skills/feishu-tools` 和 `.agents/skills/feishu-debate`。
本机已在 `~/.agents/skills/` 注册同名符号链接，所有工作目录均可发现；换机器部署时需注册：

```bash
mkdir -p ~/.agents/skills
ln -s "$PWD/.agents/skills/feishu-tools" ~/.agents/skills/feishu-tools
ln -s "$PWD/.agents/skills/feishu-debate" ~/.agents/skills/feishu-debate
```

辩论通过 skill 脚本为两方分别维护历史，只交换最终发言，各 5 轮同步推送；
不写 bot 会话表，内部调用为关闭工具的临时上下文。摘要调用同样不加载用户插件配置。
轻量调用沿用当前默认模型，不硬编码 Claude 的 `--tools` 参数。

`speak` 直接在 CLI 中完成 TTS → SSH → 指定 sink 的同步播放，不经过 bot socket。
私有配置为 `~/.config/feishu-bot/speaker.json`（权限 600，可用 `FEISHU_SPEAKER_CONFIG` 指定）：
包含 `key`、`region`、`voice`、`remote`、`sink`。本机复用了已有 Azure 配置，未修改小兔项目。
仅在用户要求音箱朗读时调用，超过 200 字会截断并返回说明；429 有退避，缺 sink 明确报 rc=99。
跨进程锁 `feishu-speaker.lock` 覆盖合成与播放，只协调 feishu-bot 各群，与小兔不共享锁。


## 权限

| 模式 | Codex 配置 | 行为 |
|---|---|---|
| 默认 | `read-only` | 只读，可运行受沙箱限制的读取命令 |
| `/permit` 开 | `workspace-write` | 可修改当前工作区，并运行沙箱内命令；命令网络访问关闭 |
| `/unsafe` 开 | `danger-full-access` | 可执行任意命令、修改工作区之外的文件 |

`/permit` 与 `/unsafe` 互斥，状态持久化；每次续聊重新应用当前权限。非交互执行不弹出审批，超出沙箱权限的操作会失败。

**迁移差异：Codex 的 `/permit` 允许沙箱内命令，与 Claude 的 `acceptEdits` 不同。** `extra_dirs` 和上传目录只作为参考路径提供，未使用会授予写权限的 `--add-dir`。当前工作目录内的文件遵循该工作目录的权限；若工作目录是家目录，开启 `/permit` 的可写范围也相应较大。

## 会话与历史迁移

MySQL 保存对话列表、名称、当前选中会话与审计，Codex 将完整对话写入本机 `~/.codex/sessions/`。任务用 JSONL 事件解析执行状态和动作，用 `--output-last-message` 获取最终正文，避免把过程说明当成最终结果。

为兼容现有数据库，历史列名 `claude_session_id` 保留：

- 旧值：Claude UUID。首次续聊时，将最近历史带入新的 GPT 对话，不把旧 UUID 传给 Codex。
- 新值：`codex:<UUID>`。之后通过 `codex exec resume` 续聊。
- 旧审计、标签及 Claude 磁盘文件保留；旧本机会话没有 DB 历史时，从原 Claude 文件只读恢复最近对话。
- Codex 文件缺失时同样用历史恢复。执行失败不自动重跑，避免重复写文件或执行命令。

上下文占用取自 Codex 报告的输入 token 与模型窗口，缓存 token 不重复累计；窗口信息缺失时省略百分比。达到 `AUTO_COMPACT_THRESHOLD` 时摘要后开新对话；设为 0 关闭自动压缩，92% 硬闸仍生效。

`/del` 保留审计记录，通过外键 `ON DELETE SET NULL` 解绑。仅清理机器人自己的 Codex rollout 文件；接管的本机会话不删盘，旧 Claude 文件不删盘。

## 附件与用量

群里上传的图片、文件等保存到 `~/run/uploads/feishu_bot/<chat_id>/`，目录权限 700、文件 600，最大 50MB，不自动解压。下一条任务自动附带最近上传路径；富文本中的图片也会下载。回复里的 `[[SEND_FILE:/绝对路径]]` 标记由 bot 处理成文件消息，最多 3 个，每个 20MB。

`/usage` 通过官方 Codex App Server 的 `account/read` 和 `account/rateLimits/read` 获取订阅身份及额度，显示服务器实际返回的窗口，重置时间为北京时间。按需查询，60 秒缓存；限流或查询失败可回退到标明的旧缓存。任务回复末尾只读新鲜缓存，不额外请求用量接口。

## 代码结构与验证

| 文件 | 职责 |
|---|---|
| `feishu_claude.py` | 兼容入口：配置、DB、长连接与多机器人启动 |
| `bot.py` | 消息分发、任务与会话编排、附件和知识库接入 |
| `codex_runner.py` | Codex 执行、JSONL 解析、会话与上下文读取 |
| `claude_runner.py` | 旧会话只读兼容与共用提示/动作格式；业务不调用 Claude 执行 |
| `core/` | 会话、上下文护栏、摘要、附件校验与 git 只读查询 |
| `fbtool` / `tool_bridge.py` | 标准库 CLI、短期任务令牌、Unix socket 路由与回复后重启 |
| `speaker.py` / `.agents/skills/` | 独立语音能力与按需加载的技能 |
| `usage.py` | Codex 订阅用量 RPC 与缓存 |
| `db.py` / `models.py` | MySQL ORM 与迁移、审计 |
| `feishu_api.py` / `uploads.py` | 飞书 SDK、文件下载与发送 |
| `wiki_sync.py` / `wiki_index.py` | 飞书知识库同步与检索 |
| `ops/` | 独立看门狗、快照、回滚；canary 已使用 Codex |

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q bot.py codex_runner.py usage.py core ops
```

本机验证记录：CLI/socket 在当前 unsafe 模式下可用，轻量调用成功；
只读沙箱的真实 shell 自测受到主机 bwrap loopback 权限限制（Operation not permitted）。
程序不因此自动绕过沙箱，需要由部署环境解决 user namespace / sandbox 支持问题。
语音已验证合成、SSH、sink 检测与缺 sink 错误；未进行实际有声播放。

本次完整检查与回归验证记录见 [2026-10-03 检查报告](docs/REVIEW-2026-10-03.md)。
