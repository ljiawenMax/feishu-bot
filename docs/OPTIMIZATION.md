# feishu-claude-bot 优化方案

> 制定日期：2026-09-29 ｜ 基线版本：`a8f10a6`
> 全部结论基于实测数据（`audit_logs` 79 天 + journal 18 天 + `sessions` 表），不含推测。

---


## 2026-10-03 续做记录

上次阶段 1 的未完成工作已继续实现，沿用工作区已有的 Codex 订阅迁移。
以下原方案与用量数据保留为历史基线，当前部署与权限说明以 README 为准。

| 阶段 | 实现结果 |
|---|---|
| 1 core 剥离 | 会话增删/接管、context、摘要、附件标记/校验/TTL、git 只读查询已独立；core 不依赖飞书或 DB |
| 2 工具基础设施 | 纯标准库 `fbtool` + 600 Unix socket + 每轮短期令牌；第一批 4 个工具已实现；最终回复送达后调度重启 |
| 3 轻量调用/skill | Codex feature 开关与忽略用户配置替代 Claude 的 `--tools`；摘要不落盘，能力说明按 skill 加载 |
| 4 第二批/辩论 | 上传台账、历史检索、图片发送、git 查询已实现；独立双方历史脚本替代 bot 内约百行编排；移除 `/debate` |
| 5 语音 | 标准库 Azure + SSH 同步播放；独立 flock、200字、429退避、缺sink报99；未修改小兔 |

保留 `/allsessions`、`/name`、`/permit`，命令总数 9。文件标记继续兼容。
权限提升仍只能走用户命令；重启工具要求当前任务已有 unsafe 权限，LLM 不能自行开启。

验证：36 项自动检查通过；真实 Codex unsafe CLI/socket 往返与轻量调用通过；
Azure 返回有效 WAV、树莓派 SSH 与目标 sink 检测通过、缺 sink 实测返回 rc=99。
只读 shell 自测遭主机 bwrap loopback 权限拒绝，保持原沙箱边界，没有自动转为 unsafe。
有声播放和生产重启后的端到端流量验证未包含在这些结果中。

## 一、现状与证据

### 1.1 代码规模

| 模块 | 行数 | 说明 |
|---|---|---|
| bot.py | 1633 | 业务主体，20 个命令 + 会话编排 |
| wiki_sync.py | 465 | 知识库镜像 |
| wiki_index.py | 455 | 混合召回 |
| claude_runner.py | 421 | CLI 调用与 session 管理 |
| feishu_api.py | 351 | 飞书交互 |
| feishu_claude.py | 267 | 入口 |
| db.py | 229 | 数据层 |
| gitsync.py | 219 | 增量 bundle 同步 |
| usage.py | 216 | 用量查询 |
| models.py | 174 | ORM |
| uploads.py | 126 | 上传落盘 |
| **合计** | **4556** | |

### 1.2 命令实测用量

| 命令 | 用量 | 证据来源 |
|---|---|---|
| `/usage` | 18 次 / 18 天 | journal |
| `/new` | 14 次 / 18 天 | journal |
| `/del` | 13 次 / 18 天 | journal |
| `/unsafe` | 1 次 / 18 天 | journal（98% 轮次常开，不需反复切） |
| `/context` | 1 次 / 18 天 | journal |
| `/restart` | **0 次 / 18 天** | journal |
| `/allsessions` | **0 次 / 18 天** | journal |
| `/compact` | **手动 0 次**（24 次压缩全部 `reason=context` 自动） | journal |
| `/retry` | **2 次 / 79 天** | `audit_logs` tag=retry |
| `/debate` | **1 次 / 79 天**（2026-07-16 唯一一晚） | `audit_logs` |
| `/model` | **从未生效**：59 个会话 `sessions.model` 列全为 NULL | `sessions` 表 |
| `/permit` | **0 次** | journal |
| `/sync` `/resync` | **0 次** | journal |
| `/files` | 无日志点；71 次上传全走自动附带 | `uploads` 表 |
| `/name` | 0 次 | journal |

### 1.3 两个决定性发现

**① gitsync 的全部使用者都是已下线的群**

```
tag='patch' 共 52 次，分布在 2 个 chat_id：
  oc_d140cbad…  46 次   2026-07-14 → 2026-08-07   ← 已不在 BOTS 配置中
  oc_aecd3e00…   6 次   2026-07-17 → 2026-07-21   ← 已不在 BOTS 配置中
```

当前在线的 9 个 bot **没有任何一个用过** gitsync，且 `sync_patch` 在 9 个 bot 上**全部为 false**。
最后一次使用距今 53 天。这 219 行 + `/sync` + `/resync` + `_send_bundle` + 基线 ref 机制，
服务的是两个已经不存在的群。

**② wiki 后台同步 40% 的日志量，全部是「什么都没变」的空转播报**

```
18 天 journal：总 4260 行，其中 [wiki] 1738 行 = 40.8%

拆开看：
  866 次  "新增 0 / 更新 0 / 删除 0 / 未变 9, chunks=91"   ← bot.py:876
  866 次  "[wiki-index] 索引完成：91 chunk / 9 文档"       ← wiki_index.py:272
    2 次  "需要编码 N 个 chunk"                          ← 18 天里真正的变更
```

每 30 分钟同步一次（`WIKI_SYNC_INTERVAL=1800`），18 天 866 轮，每轮无条件打两行。
1738 行里 **1730 行是噪音**。

**这不是「群该停用」的证据**——知识库里有 **91 chunk / 9 文档**，内容是齐的，
该群只是还没开始正式提问。修的是日志，不是群。

### 1.4 各群活跃度

| bot | 总任务 | 近 18 天 | 最后使用 | 判定 |
|---|---|---|---|---|
| general | 438 | **142** | 09-29 | 活跃 |
| cnooc-bot | 91 | **84** | 09-23 | 活跃 |
| feishu-bot | 228 | 10 | 09-29 | 活跃（运维群） |
| stock | 18 | 1 | 09-23 | 低频 |
| English | 66 | 0 | 09-01 | 休眠 |
| ipcp | 30 | 0 | 08-25 | 休眠 |
| resume | 21 | 0 | 08-23 | 休眠 |
| wiki | 5 | 0 | 07-29 | 保留：知识库已备 91 chunk，待正式启用 |
| debate | **0** | 0 | 从未使用 | 保留（用户决定） |

---

## 二、优化目标

1. **砍掉服务于不存在场景的代码**——gitsync、9 个零用量命令、debate 编排
   （**9 个 bot 全部保留**，删的是代码不是群）
2. **把能力从「命令」迁到「tool」**——LLM 可调用，自然语言触发，不占命令表
3. **建立三层架构**——core 层不依赖飞书，为将来的能力扩展留出位置
4. **消掉日志噪音**——wiki 同步无变更时静默，日志量降 40%（不动群）

**不做的事**（前几轮已排除）：MCP 方案、LLM 抽象层、飞书多平台抽象、在本项目实现模型后端切换。

---

## 三、目标架构

```
┌─────────────────────────────────────────────────┐
│ 命令层（飞书特有，直接用 lark SDK，73 处调用点不动）  │
│   /new /unsafe /usage /del /sessions /help      │
└───────────────────┬─────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────┐
│ core 层（纯能力）                                 │
│   不 import feishu_api，不认识 message_id        │
│   compact / session 管理 / uploads / wiki 检索   │
└───────────────────┬─────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────┐
│ tool 层（LLM 调用）                               │
│   fbtool CLI（纯标准库，30ms 启动）               │
│      ↓ unix socket（权限 600 天然鉴权）           │
│   bot 进程（client/token 热加载）                 │
└─────────────────────────────────────────────────┘
```

**为何 CLI 而非 MCP**：上下文注入靠 `Popen(env=)` 的环境变量继承天然解决
（`FEISHU_CHAT_ID` / `FEISHU_MSG_ID`）；「📝 本轮动作」已经报 `Bash`，tool 可见性白送。

---

## 四、分阶段实施

### 阶段 0：清理（无依赖，可立即做）

> **事后修订（2026-09-29，阶段 0 执行当日）**：`/allsessions`、`/name`、`/permit`
> 三个命令已按机主要求**恢复**。1.2 节的用量数据本身没错（三者确实零使用），但
> 「零使用」作为撤除依据对它们不成立：`/permit` 是与 `/unsafe` 同源的权限护栏，
> 护栏的价值不由使用频次衡量；`/allsessions` 与 `/name` 则是「还没轮到用」，
> 不是「用过发现没用」。下面表格里的「删 7 个命令」实际落地为 **9 个**
> （`/compact` `/retry` `/files` `/model` `/context` `/wiki` `/sync` `/resync` `/restart`）。

**收益最大、风险最低，先做完这步再动架构。**

| 动作 | 删除量 | 风险 |
|---|---|---|
| 删 7 个命令：`/compact` `/retry` `/files` `/model` `/context` `/wiki` `/permit` `/name` `/allsessions` | ~288 行 | 低 |
| 删 gitsync 整套（`gitsync.py` + `/sync` + `/resync` + `_send_bundle`） | ~260 行 | 低（53 天无人用） |
| wiki 同步无变更时静默（日志降 40%） | +3 行 | 无 |
| 删 `/restart` 命令（阶段 2 以 tool 回归） | 27 行 | 低 |

**合计删除 ≈ 575 行，bot.py 1633 → ~1320。**

⚠️ **必须同步处理的联动**：

1. 删 `/compact` 后，92% 硬闸的提示文案写着「手动 `/compact` 或 `/new`」→ 改成只提 `/new`
2. 删 `/model` 后，`pending = "model"` 的回复分支、`models` 配置项读取一并清理；
   `sessions.model` **列保留**（DB 迁移不值当，且 `_format_context` 之外无人读）
3. 删 `/files` 后，`pending_uploads` 缓冲**必须保留**——自动附带还在用，删的只是查看入口
4. 删 `/wiki` 后，`_wiki_refresh`（24 行）**保留**——后台定时同步还在调；
   删的是 `cmd_wiki`（80 行）。`spaces/sync/search/rebuild` 四个子命令 CLI 已完全等价：
   `.venv/bin/python -m wiki_sync --env local --bot <name> --spaces`
5. 删 gitsync 后，`refs/feishu-sync/<chat_id>` 隐藏 ref 留在仓库里无害，可不清理

**wiki 同步静默化**（`_wiki_refresh`，bot.py:853）：

```python
changed = stats['added'] + stats['updated'] + stats['removed'] + stats['failed']
# 无变更时不透传 log，build 的「索引完成」一并静默
wiki_index.build(..., log=log if changed else (lambda s: None))
...
if changed:                       # 原来是无条件 print
    print(f"[{self.name}][wiki] {desc}, chunks={n}")
```

`self.wiki_status` 字段仍然每轮更新，状态随时可查——**静默日志不丢信息**。
手动触发时（将来的 `wiki_search` tool 或 CLI）强制打印，加个 `verbose=True` 参数即可。

**wiki 群保留**：知识库已有 91 chunk / 9 文档，是待启用状态而非废弃。
`_wiki_refresh`、`_wiki_worker`、`_wiki_reference` 全部保留，删的只有 `cmd_wiki`（80 行）。

### 阶段 1：core 剥离（所有后续工作的前提）

把 `cmd_*` / `_send_*` 里的纯能力抽成无飞书依赖的函数。

**约束**：`core/` 不 import `feishu_api`、不认识 `message_id`。
**验证方式**：行为零变化，可独立单测。

```
core/
  compact.py     ← 从 _do_compact 抽（摘要生成，不管推送）
  sessions.py    ← 会话增删改查、接管本机会话
  context.py     ← context 占用计算（_context_pct）
  uploads.py     ← 已经基本独立，微调即可
```

这一步**不改行为、不删功能**，纯搬家。做完后 `bot.py` 只剩「飞书 I/O + 编排」。

### 阶段 2：tool 基础设施

**2.1 `Popen(env=)` 注入**（改 `claude_runner.run_claude`）

```python
env = {**os.environ,
       "FEISHU_CHAT_ID": chat_id,
       "FEISHU_MSG_ID": msg_id}
```
顺带为 per-bot 后端切换预留 `ANTHROPIC_BASE_URL`（本项目不实现切换逻辑，
在 CLI 配置层做，对项目透明）。

**2.2 `fbtool` CLI + unix socket**

- 纯标准库（启动 30ms，每轮可能调多次，不能背 SDK 的加载开销）
- 请求交回 bot 进程处理（client/token 由 bot 热加载，CLI 不碰凭证）
- socket 文件权限 600 = 天然鉴权
- 路径：`/run/user/<uid>/feishu-bot.sock` 或 `~/run/feishu-bot.sock`

**2.3 第一批 tool**

| tool | 替代 | 备注 |
|---|---|---|
| `send_file(path)` | `[[SEND_FILE:]]` 文本标记 | 42 次/18 天，最高频能力 |
| `send_text(text)` | 无（新增） | 长任务报进展，不只是心跳 |
| `restart_service()` | `/restart` 命令 | **见下方设计要点** |
| `wiki_search(query)` | 每轮强制预注入 | 按需检索，省 context |

**`restart_service` 设计要点**（不能简单包装旧 `cmd_restart`）：

实测 LLM 自然语言重启时自己选了 **25 秒延迟 + 轮询验证 active**，
而旧命令硬编码 `sleep 2` 且无验证——直接包装等于降级。正确做法：

```
tool 调用 → 只登记 pending_restart 意图（不立即调度）
  → bot 推完最终回复（_push_reply 成功）
  → 才起 transient unit：sudo -n systemd-run --collect --no-block
  → 服务重启 → STARTUP_NOTIFY 推「✅ 已启动」（含代码版本 hash）
```

时序挂在「回复推送完成」这个点上，延迟能缩回 2 秒且零丢回执风险——比猜 25 秒更准。
重启后的健康验证不由 tool 做（那时它已被杀），靠启动通知覆盖：收不到就是没起来。

**附带收益**：CLAUDE.md 里「自杀式重启陷阱」那段 ~200 字可删，
tool description 一行足够（"重启本服务，已处理 cgroup 自杀问题"）。

### 阶段 3：轻量调用与 skill

**3.1 `--tools ""` 轻量调用**
压缩摘要、debate 这类内部调用不需要工具权限，显式关掉可省启动开销与误触发。

**3.2 skill 替代硬塞 prompt**
当前 `SYNC_SYSTEM_PROMPT` 427 字（≈284 token）每轮 `--append-system-prompt` 硬塞。
1103 次任务 ≈ 31 万 token 的固定成本——**金额上不大，真正的收益是可维护性**：
加新能力不必改这段硬编码，skill 发现层自动暴露。

### 阶段 4：第二批 tool 与 debate 重构

| tool | 说明 |
|---|---|
| `list_uploads(n)` | 补 `pending_uploads` 内存缓冲的可见性漏洞 |
| `history_search(query)` | 查超过最近 20 轮的历史 |
| `send_image(path)` | 发图表 |
| `git_local_changes()` | 只读查改动（gitsync 删掉后的替代，不带基线机制） |

**`/debate` 重构为 skill**：

直接让一个 LLM 扮演双方会丢掉最本质的东西——**两个独立 session 互不见底牌**。
一个 LLM 同时知道正反方所有想法，辩论会自动收敛（它会不自觉地让双方都合理）。

拿回来的路径：
- 独立上下文 → **subagent**（两个 Task 各自维护上下文）
- 每轮实时推送 → **`send_text` tool**（阶段 2 已落地）
- 落库 + 清理 → subagent 天然不落 `sessions` 表，比现在的临时 Conversation 行更干净

完成后删 `_execute_debate_job` + `_build_debate_prompt` + `cmd_debate` ≈ **100 行**。

### 阶段 5：语音能力（作为全局 tool）

已用 feishu-bot 自己的 venv 实测跑通，**不碰 xiaotu 代码**：

```
TTS 合成(1.5s) → ssh 传文件 → 树莓派 paplay → 蓝牙音箱
新增依赖：零（Azure TTS 只要 httpx，venv 已有）
核心代码：~45 行
```

**定位：通用 tool，9 个群全部可用，不绑定任何群、不加 `/say` 命令。**
交互方式纯自然语言，代价是 5~17s 延迟（LLM 一轮 3~15s + TTS 1.5s）。

#### 关键设计：speak 不走 socket

tool 分两类，`speak` 属于后者：

| 类型 | tool | 为什么 |
|---|---|---|
| **必须回 bot 进程** | `send_file` `send_text` `restart_service` `wiki_search` | 要飞书 client / 要在 cgroup 外调度 / 要 DB 凭证与已加载的索引 |
| **纯 CLI 即可** | `speak` | 只需 TTS + ssh + paplay，不碰飞书、不碰 DB |

`speak` 在 `fbtool` 里直接做完，少一次 socket 往返、bot 进程不被 TTS 的 1.5s 阻塞。

#### 并发控制：只管 feishu-bot 内部，暂不牵连 xiaotu

「所有群可用」意味着 9 个群都能让同一个音箱出声，两个群同时触发会**声音叠加**
（不是排队，是同时播）。但 xiaotu（睡前故事，晚上）与 feishu-bot（白天）
**使用时段天然错开**，按 YAGNI 处理：**先只做内部串行，等真冲突再统一**。

**进程模型**（已实测确认）：9 个 bot 跑在**同一个进程**里（多线程，一个 bot 一组
inbox/worker 线程），不是 9 个进程。但 `speak` 由 claude 子进程调用，
子进程之间仍需跨进程同步。

**方案：`fcntl.flock` + feishu-bot 专用锁文件**

```python
LOCK = "/run/user/<uid>/feishu-speaker.lock"   # 专用，不与 xiaotu 共享
with open(LOCK, "w") as f:
    fcntl.flock(f, fcntl.LOCK_EX)      # 阻塞直到拿到
    play(wav)                           # paplay 同步返回才释放
```

**锁文件路径本身就是作用域开关**——这是选它而非进程内队列的理由：

```
现在  /run/user/<uid>/feishu-speaker.lock   仅 feishu-bot 内部 9 个 bot
将来  /run/user/<uid>/speaker.lock          与 xiaotu 共享（xiaotu 接同一路径）
```

一行之差，现在不动 xiaotu 半个字，未来统一也无需重构。
同时保住了「`speak` 纯 CLI 不走 socket」的简化，bot 进程不被 TTS 的 1.5s 阻塞。

⚠️ **已知限制**：`flock` **不保证 FIFO**，多个等待者的唤醒顺序由内核决定。
本场景只要求「不叠加」，不要求严格先来后到。若将来确需有序播放，
改用 bot 进程内 `queue.Queue`（9 个 bot 同进程，一个队列即可覆盖），
代价是 `speak` 必须改走 socket。

#### 另外两件必须补的

1. **Azure 429 退避**——免费额度下并发触发很容易撞限流，指数退避 + 失败返回明确错误
2. **sink 不存在返回 `rc=99`**——音箱没连、蓝牙掉了，要让 LLM 拿到错误去告诉用户，
   而不是静默失败（用户以为说了，其实没响）

#### 防呆

全局可用意味着任何群的 LLM 都可能让家里音箱出声（比如 `cnooc-bot` 处理工作任务时误调）。
tool description 要写明使用场景，并加两道防呆：
- **文本长度上限**（建议 200 字）——防止把整段回复念出来
- 超长时截断并在返回值里说明，让 LLM 知道只播了一部分

## 五、实施顺序

```
阶段 0 清理（独立，先做）
    │
    ▼
阶段 1 core 剥离（后续一切的前提）
    │
    ▼
阶段 2 Popen(env=) → fbtool + socket → 第一批 tool
    │
    ├──▶ 阶段 3 轻量调用 + skill
    │
    └──▶ 阶段 4 第二批 tool + debate 重构

阶段 5 语音 tool（依赖阶段 2 的 fbtool 骨架，不依赖 socket）
```

---

## 六、预期结果

| 指标 | 现状 | 优化后 |
|---|---|---|
| 命令数 | 20 | **9**（另保留 `/allsessions` `/name` `/permit`，辩论已迁 skill） |
| bot.py 行数 | 1633 | ~1200 |
| 项目总行数 | 4556 | ~4300（结构大幅改善） |
| 在线 bot | 9 | **9（全部保留）** |
| journal 日志量 | 4260 行/18 天 | ~2530 行（降 40%，靠静默不靠停机） |
| LLM 可调用能力 | 0 | 9 个 tool（含全局 `speak`） |
| 架构 | 单层，bot.py 承载一切 | 三层，core 不依赖飞书 |

---

## 七、风险与回滚

- **阶段 0 的删除是不可逆的功能移除**。建议先 `git tag pre-optimization` 打点，
  真需要恢复时 `git show` 取回单个函数即可
- **gitsync 删除后**若将来又需要远程同步，`git bundle` 的做法已记录在 CLAUDE.md，
  重写成本低于维护成本
- **看门狗（`ops/`）优化期间不动**——它刻意不 import 任何项目模块，
  重构期间它仍是唯一可靠的自愈通道
- **每个阶段结束后重启验证**：`systemctl restart feishu-bot.service`
  → 群里收到「✅ 已启动」→ 发一条测试任务确认端到端通
  在 bot 子进程内必须走回复送达后的 `restart_service`，不可直接执行上述重启命令。

---

## 八、已确认的决策

| 议题 | 决定 | 日期 |
|---|---|---|
| gitsync 整套 | 删除（使用者全是已下线的群，53 天无人用） | 2026-09-29 |
| `/restart` | 删命令，改 `restart_service` tool | 2026-09-29 |
| wiki 群 | **保留**，删的只有 `cmd_wiki`；日志改静默 | 2026-09-29 |
| debate 群 | **保留**（代码仍在阶段 4 重构为 skill） | 2026-09-29 |
| 语音能力 | **全局 tool**，9 个群都可用，不绑群、不加命令 | 2026-09-29 |
| 9 个 bot | **全部保留**，本方案删代码不删群 | 2026-09-29 |
| 音箱并发 | flock + **专用锁文件**，只管内部串行；xiaotu 暂不接锁（时段错开，等真冲突再统一） | 2026-09-29 |
