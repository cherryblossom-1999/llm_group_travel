#!/usr/bin/env bash
# 데모 API 서버 기동 (포트 8090). 선행: Gemma 서버(localhost:8000)
#   bash run_server.sh            # nohup 백그라운드, 로그 server.log
#   bash run_server.sh stop
set -u
cd "$(dirname "$0")"
PY=/home/teedlab/miniconda3/envs/torch_env/bin/python
PORT="${PORT:-8090}"
if [ "${1:-}" = "stop" ]; then
  pkill -f "uvicorn server:app" && echo "stopped" || echo "not running"
  exit 0
fi
pkill -f "uvicorn server:app" 2>/dev/null
setsid nohup "$PY" -m uvicorn server:app --host 0.0.0.0 --port "$PORT" --log-level info > server.log 2>&1 &
sleep 2
curl -s "http://127.0.0.1:$PORT/api/health" && echo
