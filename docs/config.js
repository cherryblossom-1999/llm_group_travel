// 데모 API 서버 주소 (start_tunnel.sh 가 자동 갱신). 같은 서버에서 서빙할 땐 "" 로 두면 상대 경로.
window.DEMO_CONFIG = {
  apiBase: location.hostname.endsWith("github.io") ? "https://eur-ice-camp-visiting.trycloudflare.com" : "",
};
