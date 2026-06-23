# feishu-claude-bot

## 项目目标

本地运行的轮询服务，监听飞书群消息，将消息内容作为任务交给 Claude Code 执行，把执行结果回复到飞书。

**不需要开放本地端口**，采用主动轮询方式拉取飞书消息。

## 文件说明

- `feishu_claude.py` — 主服务脚本，轮询飞书消息并驱动 Claude Code 执行
- `config.json` — 运行配置（需填写真实的飞书凭证）

## config.json 字段说明

```json
{
  "app_id": "飞书应用 App ID",
  "app_secret": "飞书应用 App Secret",
  "chat_id": "监听的群聊 ID（格式 oc_xxxxxx）",
  "work_dir": "Claude Code 执行任务时的工作目录（绝对路径）",
  "poll_interval": 5,
  "task_timeout": 300
}
```

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
