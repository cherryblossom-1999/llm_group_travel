#!/usr/bin/env bash
# ============================================================
# run_server.sh — 데모 전체 기동 (한 번에)
#   1) Gemma 배칭 서버 (localhost:8000)  : 없으면 기동, grouped_mm 고정, 배치 16
#   2) 데모 API 서버   (0.0.0.0:8090)   : 기동 시 v3 상주 워커(임베딩 모델·권역 인덱스)를 워밍업
#   (외부 노출 터널은 별도: bash start_tunnel.sh)
#
#   bash run_server.sh            # 전체 기동 (Gemma 가 이미 떠 있으면 그대로 사용)
#   bash run_server.sh stop       # API 서버 + v3 워커만 중지 (Gemma 유지)
#   bash run_server.sh stop-all   # Gemma 까지 중지
#   bash run_server.sh restart-gemma   # Gemma 서버만 재기동 (서버 코드 수정 후)
#   bash run_server.sh status
#
# 환경변수(선택): PORT(8090) GEMMA_MAX_BATCH(16) DEMO_GEN_CONCURRENCY(16) DEMO_TOPK(30) DEMO_EARLY_EXIT(10)
# ============================================================
set -u
cd "$(dirname "$0")"
PY=/home/teedlab/miniconda3/envs/torch_env/bin/python
V3=../talk_pipeline_v3
PORT="${PORT:-8090}"
export GEMMA_MAX_BATCH="${GEMMA_MAX_BATCH:-16}"
export GEMMA_BATCH_WAIT_MS="${GEMMA_BATCH_WAIT_MS:-80}"
export GEMMA_FORCE_GROUPED_MM="${GEMMA_FORCE_GROUPED_MM:-1}"
export DEMO_GEN_CONCURRENCY="${DEMO_GEN_CONCURRENCY:-16}"
export DEMO_TOPK="${DEMO_TOPK:-30}"
export DEMO_EARLY_EXIT="${DEMO_EARLY_EXIT:-10}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

gemma_ready() { curl -s -m 3 http://localhost:8000/health | grep -q '"model_loaded": *true'; }
gemma_pid()   { pgrep -f "^${PY} .*serve_gemma_(batched|local)\.py" | head -1; }
api_pid()     { pgrep -f "^${PY} -m uvicorn server:app" | head -1; }

start_gemma() {
  if gemma_ready; then
    if curl -s -m 3 http://localhost:8000/health | grep -q '"batching"'; then
      echo "[gemma] 이미 실행 중 (배칭 서버) -> 그대로 사용"
      return 0
    fi
    echo "[gemma] 구버전(직렬) 서버가 떠 있음 -> 배칭 서버로 교체"
    stop_gemma
  fi
  echo "[gemma] 배칭 서버 기동 (batch=$GEMMA_MAX_BATCH, grouped_mm=$GEMMA_FORCE_GROUPED_MM) … 모델 로딩 약 4분"
  ( cd "$V3" && setsid nohup "$PY" serve_gemma_batched.py --model-id google/gemma-4-26B-A4B-it --port 8000 \
      > gemma_batched.log 2>&1 < /dev/null & )
  for i in $(seq 1 90); do
    gemma_ready && { echo "[gemma] 준비 완료"; return 0; }
    sleep 5
  done
  echo "[gemma] 준비 실패: $V3/gemma_batched.log 확인"; return 1
}

# pid 를 정중히 종료하고(SIGTERM) 최대 N초 기다린 뒤 남아 있으면 강제 종료
kill_wait() {
  local p="$1" n="${2:-20}"
  [ -z "$p" ] && return 0
  kill "$p" 2>/dev/null
  for i in $(seq 1 "$n"); do kill -0 "$p" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$p" 2>/dev/null; sleep 1
}

stop_gemma() {
  local p
  for p in $(pgrep -f "^${PY} .*serve_gemma_(batched|local)\.py"); do kill_wait "$p" 30; done
  echo "[gemma] 중지"
}

stop_api() {
  local p
  for p in $(pgrep -f "^${PY} -m uvicorn server:app"); do kill_wait "$p" 15; done
  for p in $(pgrep -f "^${PY} -u .*v3_worker.py"); do kill_wait "$p" 5; done
  echo "[api] 중지"
}

start_api() {
  stop_api >/dev/null
  setsid nohup "$PY" -m uvicorn server:app --host 0.0.0.0 --port "$PORT" --log-level info \
      > server.log 2>&1 < /dev/null &
  sleep 3
  echo "[api] http://127.0.0.1:$PORT  (v3 워커 워밍업은 백그라운드로 약 1~2분, 그 사이에도 사전 계산 결과는 조회 가능)"
  curl -s -m 5 "http://127.0.0.1:$PORT/api/health" && echo
}

case "${1:-start}" in
  start)
    start_gemma || exit 1
    start_api
    ;;
  stop)
    stop_api ;;
  stop-all)
    stop_api
    stop_gemma ;;
  restart-gemma)
    stop_gemma
    start_gemma ;;
  status)
    echo "gemma: $(gemma_ready && echo ready || echo down)  pid=$(gemma_pid)"
    echo "api:   pid=$(api_pid)"
    curl -s -m 5 "http://127.0.0.1:$PORT/api/health"; echo ;;
  *)
    echo "사용: bash run_server.sh [start|stop|stop-all|restart-gemma|status]" ;;
esac
