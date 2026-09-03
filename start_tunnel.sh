#!/usr/bin/env bash
# cloudflared quick tunnel 로 API 서버(8090)를 HTTPS 로 노출하고, 주소를 docs/config.js 에 반영해 push.
#   bash start_tunnel.sh          # 터널 기동 + URL 반영 + push
#   bash start_tunnel.sh stop
set -u
cd "$(dirname "$0")"
CF=~/bin/cloudflared
if [ "${1:-}" = "stop" ]; then pkill -f "cloudflared tunnel" && echo stopped; exit 0; fi
pkill -f "cloudflared tunnel" 2>/dev/null
setsid nohup "$CF" tunnel --url http://127.0.0.1:8090 --no-autoupdate > tunnel.log 2>&1 &
URL=""
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
  [ -n "$URL" ] && break; sleep 1
done
[ -z "$URL" ] && { echo "[오류] 터널 URL 없음 (tunnel.log 확인)"; exit 1; }
echo "tunnel: $URL"
cat > docs/config.js <<JS
// 데모 API 서버 주소 (start_tunnel.sh 가 자동 갱신). 같은 서버에서 서빙할 땐 "" 로 두면 상대 경로.
window.DEMO_CONFIG = {
  apiBase: location.hostname.endsWith("github.io") ? "$URL" : "",
};
JS
git add docs/config.js && git commit -q -m "tunnel url: $URL" && git push -q origin main && echo "config.js pushed -> 공개 페이지는 1~2분 뒤 반영"
