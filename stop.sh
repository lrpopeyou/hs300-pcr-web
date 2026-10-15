#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
if [ -f logs/daemon.pid ]; then
  PID="$(cat logs/daemon.pid)"
  if kill -0 "$PID" 2>/dev/null; then
    CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
    if printf '%s' "$CMD" | grep -q 'hs300_pcr_strategy.py daemon'; then
      kill "$PID" && echo "已停止每日自动更新进程（PID $PID）"
    else
      echo "PID 文件指向的进程不是 PCR 自动更新进程，未执行 kill。"
    fi
  else
    echo "每日自动更新进程已停止。"
  fi
  rm -f logs/daemon.pid
else
  echo "没有找到 logs/daemon.pid。若只需关闭图形界面，在运行 Streamlit 的终端按 Ctrl+C。"
fi
