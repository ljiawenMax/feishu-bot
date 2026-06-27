#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV="${1:-local}"
# 一份配置一个进程（进程内可多机器人）；PID 与日志按配置名区分
PID_FILE="$HOME/run/feishu_bot/$ENV.pid"
LOG_FILE="$HOME/run/log/feishu-bot-$ENV.log"
mkdir -p "$(dirname "$LOG_FILE")"

# 停止旧进程
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "stopping old process (pid=$OLD_PID)..."
        kill "$OLD_PID"
        # 等待进程退出（最多 5s）
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "force killing..."
            kill -9 "$OLD_PID"
        fi
        echo "stopped"
    else
        echo "pid=$OLD_PID not running, skipping"
    fi
fi

# 启动新进程
cd "$SCRIPT_DIR"
nohup .venv/bin/python -u feishu_claude.py --env "$ENV" >> "$LOG_FILE" 2>&1 &
echo "started, pid=$!, env=$ENV, log=$LOG_FILE"
