#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV="${1:-local}"
if [[ ! "$ENV" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "invalid environment name" >&2
    exit 1
fi
# 一份配置一个进程（进程内可多机器人）；PID 与日志按配置名区分
PID_FILE="$HOME/run/feishu_bot/$ENV.pid"
LOG_FILE="$HOME/run/log/feishu-bot-$ENV.log"
mkdir -p "$(dirname "$LOG_FILE")"

# PID 文件可能残留或 PID 已被复用，发信号前核对进程身份。
is_bot_process() {
    local candidate="$1" index script_index=-1
    local -a process_args
    [[ "$candidate" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ "$(readlink -f "/proc/$candidate/cwd" 2>/dev/null)" == "$SCRIPT_DIR" ]] || return 1
    mapfile -d '' -t process_args < "/proc/$candidate/cmdline" 2>/dev/null || return 1
    for ((index=1; index<${#process_args[@]}; index++)); do
        if [[ "${process_args[index]}" == "feishu_claude.py" ||
              "${process_args[index]}" == "$SCRIPT_DIR/feishu_claude.py" ]]; then
            script_index=$index
            break
        fi
    done
    ((script_index >= 1)) || return 1
    for ((index=script_index+1; index<${#process_args[@]}-1; index++)); do
        if [[ "${process_args[index]}" == "--env" && "${process_args[index+1]}" == "$ENV" ]]; then
            return 0
        fi
    done
    return 1
}

# 停止旧进程
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if [[ ! "$OLD_PID" =~ ^[1-9][0-9]*$ ]]; then
        echo "invalid PID file, refusing to signal a process" >&2
        exit 1
    fi
    if kill -0 "$OLD_PID" 2>/dev/null; then
        if ! is_bot_process "$OLD_PID"; then
            echo "pid=$OLD_PID does not match this bot and environment; refusing to stop it" >&2
            exit 1
        fi
        echo "stopping old process (pid=$OLD_PID)..."
        kill "$OLD_PID"
        # 等待进程退出（最多 5s）
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            if ! is_bot_process "$OLD_PID"; then
                echo "process identity changed; refusing to force kill" >&2
                exit 1
            fi
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
