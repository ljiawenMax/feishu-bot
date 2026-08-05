#!/usr/bin/env bash
# 安装/更新看门狗的 systemd unit。改完 ops/systemd/ 下的文件后重跑一次即可。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo -n install -m 0644 "$HERE/systemd/feishu-bot-guard.service" /etc/systemd/system/
sudo -n install -m 0644 "$HERE/systemd/feishu-bot-guard.timer"   /etc/systemd/system/
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now feishu-bot-guard.timer

echo "--- timer ---"
systemctl list-timers feishu-bot-guard.timer --no-pager || true
echo
echo "手动跑一次： sudo systemctl start feishu-bot-guard.service && journalctl -u feishu-bot-guard -n 30 --no-pager"
echo "只看判定：   .venv/bin/python ops/guard.py --status"
echo "演练不动手： .venv/bin/python ops/guard.py --dry-run"
