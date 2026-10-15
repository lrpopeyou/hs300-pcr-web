#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
START_DATE="${PCR_START_DATE:-20240501}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "找不到 $PYTHON_BIN。请先安装 Python 3.10+，或设置 PYTHON_BIN。"
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "[1/4] 创建 Python 虚拟环境..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
REQ_HASH="$(python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')"
OLD_HASH="$(cat "$VENV_DIR/.requirements_hash" 2>/dev/null || true)"
if [ "$REQ_HASH" != "$OLD_HASH" ]; then
  echo "安装/更新 Python 依赖（首次运行或 requirements.txt 有变化时执行）..."
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  printf '%s' "$REQ_HASH" > "$VENV_DIR/.requirements_hash"
else
  echo "依赖已就绪，跳过重复安装。"
fi
mkdir -p logs data output

echo "[2/4] 更新数据、计算 PCR/标准差/信号并尝试回测..."
# First start backfills missing ETF dates from START_DATE; subsequent starts only fetch missing dates.
if ! python hs300_pcr_strategy.py run --full-backfill --etf-start "$START_DATE"; then
  echo "数据同步未完全成功；将继续启动图形界面，查看本地缓存并重试即可。"
fi

echo "[3/4] 启动每日收盘后自动更新进程（北京时间 15:20）..."
if [ -f logs/daemon.pid ] && kill -0 "$(cat logs/daemon.pid)" 2>/dev/null; then
  echo "每日自动更新进程已运行，PID=$(cat logs/daemon.pid)"
else
  nohup python hs300_pcr_strategy.py daemon --etf-start "$START_DATE" --time 15:20 --poll-seconds 60 >> logs/daemon.log 2>&1 &
  echo $! > logs/daemon.pid
fi

echo "[4/4] 打开图形化交易系统：http://localhost:8501"
exec streamlit run app.py --server.address 127.0.0.1 --server.port 8501
