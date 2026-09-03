/* 그룹 여행지 추천 데모 — 프론트
 * 대화록 + 권역 선택 → (실제 실행: API/SSE) 또는 (사전 계산 결과) → 기존 대시보드 HTML을 iframe 에 표시 */
(() => {
'use strict';

const CFG = window.DEMO_CONFIG || {};
const API = (CFG.apiBase || '').replace(/\/$/, '');
const REGIONS = ['강원도', '경상남도', '경상북도', '수도권', '전라남도', '전라북도', '제주도', '충청남도', '충청북도'];
const CONSENSUS = { hi: '합의 높음', lo: '합의 낮음' };
// 단계별 이름 / 평균 소요(초, 실측) — 진행 바·예상 남은 시간 계산용
const STAGES = [
  { name: '대화 요약 (LLM)', detail: 'Gemma가 참가자별 선호와 그룹 쿼리를 생성하고 self-refine 합니다', sec: 120 },
  { name: '임베딩 검색', detail: 'bge-m3로 권역 내 후보 장소를 검색합니다', sec: 20 },
  { name: 'Nash 합의 재랭킹', detail: '참가자별 Wilson 점수를 Nash 곱으로 합의 순위화합니다', sec: 22 },
  { name: '카테고리 필터 + LLM 검수', detail: '그룹 핵심 조건으로 필터한 뒤 후보마다 Gemma가 적합성을 검수합니다', sec: 35 },
  { name: '부분충족 백필 (LLM)', detail: '통과 장소가 10개 미만이면 조건별 재검수로 채웁니다', sec: 45 },
  { name: '대시보드 생성', detail: '결과를 대시보드 HTML로 만듭니다', sec: 2 },
];

const $ = id => document.getElementById(id);
// test-only (?sync=1): 헤드리스 캡처가 데이터 로딩을 기다리도록 동기 XHR 사용
const SYNC = new URLSearchParams(location.search).get('sync');
async function getJson(url) {
  if (SYNC) { const x = new XMLHttpRequest(); x.open('GET', url, false); x.send(); if (x.status !== 200) throw new Error(x.status); return JSON.parse(x.responseText); }
  const r = await fetch(url, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); return r.json();
}
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const fmt = s => s >= 60 ? `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초` : `${Math.round(s)}초`;

let dialogues = [];
let cur = null;
let online = false;
let serverCurrent = null;   // health.current (서버에서 실행 중인 작업)
let es = null;              // EventSource
let watching = null;        // {jobId, did, region, t0}
let timer = null;
let curStage = 0;           // 1..6
let stageStart = {};        // stage -> ms
let stageDur = {};          // stage -> sec (완료된 단계)

// ---------- 서버 상태 ----------
async function checkHealth() {
  try {
    const j = await getJson(`${API}/api/health`);
    online = !!j.gemma;
    serverCurrent = j.current || null;
    const busy = serverCurrent ? ` · 실행 중 (${serverCurrent.did} × ${serverCurrent.region})` : '';
    setStatus(online ? 'on' : 'warn', online ? `서버 온라인${j.queue ? ` · 대기 ${j.queue}` : ''}${busy}` : '서버는 켜졌지만 LLM 미준비');
  } catch (e) {
    online = false; serverCurrent = null;
    setStatus('off', '실행 서버 오프라인 — 사전 계산 결과만 볼 수 있습니다');
  }
  updateButtons();
  updateAttachBanner();
}
function setStatus(cls, text) {
  $('status').className = `status ${cls}`;
  $('status-text').textContent = text;
}
// 이 탭이 보고 있지 않은 실행이 서버에서 돌고 있으면 붙어서 볼 수 있게 안내
function updateAttachBanner() {
  const show = serverCurrent && (!watching || watching.jobId !== serverCurrent.job_id);
  $('attach').hidden = !show;
  if (show) $('attach-text').textContent = `서버에서 ${serverCurrent.did} × ${serverCurrent.region} 실행이 진행 중입니다 (${fmt(serverCurrent.elapsed || 0)} 경과).`;
}

// ---------- 대화 목록 ----------
async function loadDialogues() {
  try { dialogues = await getJson(`${API}/api/dialogues`); }
  catch (e) { try { dialogues = await getJson('dialogues.json'); } catch (e2) { dialogues = []; } }
  renderList();
}
function renderList() {
  $('dlist').innerHTML = dialogues.map(d =>
    `<li class="ditem${cur && d.id === cur.id ? ' on' : ''}" data-id="${esc(d.id)}">
       <div class="id"><span>${esc(d.id)}</span>${d.consensus ? `<span class="chip ${d.consensus}">${CONSENSUS[d.consensus] || d.consensus}</span>` : ''}</div>
       <div class="pv">${esc(d.preview)}</div>
       <div class="meta">${esc(d.label)} · ${d.n_turns}턴 · ${esc(d.participants)}</div>
     </li>`).join('') || '<li class="muted" style="padding:12px">대화록 목록을 불러오지 못했습니다.</li>';
  $('dlist').querySelectorAll('.ditem').forEach(li => li.onclick = () => selectDialogue(li.dataset.id));
}
function selectDialogue(id, keepProgress) {
  cur = dialogues.find(d => d.id === id) || null;
  if (!cur) return;
  renderList();
  $('empty').hidden = true;
  $('r-did').textContent = cur.id;
  $('r-label').textContent = `${cur.label} · ${CONSENSUS[cur.consensus] || ''}`.replace(/ · $/, '');
  renderTranscript(cur.text || '');
  $('transcript-card').hidden = false;
  if (!keepProgress) { $('result-card').hidden = true; if (!es) $('progress').hidden = true; }
  updateButtons();
}
function renderTranscript(text) {
  const rows = [];
  for (const l of text.replace(/^﻿/, '').split('\n')) {
    const m = l.match(/^\s*([^:(（]+?)\s*[\(（](P\d+)[\)）]\s*[:：]\s*(.+)$/);
    if (m) rows.push(`<div class="turn"><div class="who">${esc(m[1])}<small>${esc(m[2])}</small></div><div class="utt">${esc(m[3])}</div></div>`);
  }
  $('transcript').innerHTML = rows.join('');
}
function updateButtons() {
  const has = !!cur;
  $('btn-live').disabled = !(has && online) || (es !== null);
  $('btn-pre').disabled = !has;
  $('btn-live').title = online ? '' : '실행 서버가 오프라인입니다';
}

// ---------- 진행 표시 ----------
function resetProgress() {
  document.querySelectorAll('.step').forEach(el => { el.classList.remove('active', 'done', 'error'); el.querySelector('.ms').textContent = ''; });
  $('log').textContent = '';
  $('log').hidden = false; $('btn-log').textContent = '로그 닫기';
  $('prog-msg').textContent = '';
  $('prog-time').textContent = ''; $('prog-eta').textContent = '';
  $('bar-fill').style.width = '0%';
  $('now-spin').className = 'spinner';
  $('now-stage').textContent = '준비 중…'; $('now-detail').textContent = '';
  curStage = 0; stageStart = {}; stageDur = {};
}
function setStage(n) {
  if (n < curStage) return;
  const now = Date.now();
  if (curStage && curStage !== n && stageStart[curStage]) stageDur[curStage] = (now - stageStart[curStage]) / 1000;
  curStage = n;
  if (!stageStart[n]) stageStart[n] = now;
  document.querySelectorAll('.step').forEach(el => {
    const k = +el.dataset.step;
    el.classList.toggle('active', k === n);
    el.classList.toggle('done', k < n);
    if (k < n && stageDur[k] != null) el.querySelector('.ms').textContent = fmt(stageDur[k]);
    if (k > n) el.querySelector('.ms').textContent = '';
  });
  const st = STAGES[n - 1];
  $('now-stage').textContent = `${n}/6 · ${st.name}`;
  $('now-detail').textContent = st.detail;
  updateBar();
}
function updateBar() {
  if (!watching) return;
  const total = STAGES.reduce((a, s) => a + s.sec, 0);
  let done = 0;
  for (let k = 1; k < curStage; k++) done += STAGES[k - 1].sec;
  const inStage = curStage ? Math.min((Date.now() - stageStart[curStage]) / 1000, STAGES[curStage - 1].sec * 0.97) : 0;
  const pct = Math.min(99, Math.round((done + inStage) / total * 100));
  $('bar-fill').style.width = pct + '%';
  const elapsed = (Date.now() - watching.t0) / 1000;
  $('prog-time').textContent = `경과 ${fmt(elapsed)}`;
  const remain = Math.max(0, total - done - inStage);
  $('prog-eta').textContent = curStage ? `예상 남은 시간 약 ${fmt(remain)} (${pct}%)` : '';
}
function finishProgress(ok, elapsed, msg) {
  stopTimer();
  const now = Date.now();
  if (curStage && stageStart[curStage]) stageDur[curStage] = (now - stageStart[curStage]) / 1000;
  document.querySelectorAll('.step').forEach(el => {
    const k = +el.dataset.step;
    el.classList.remove('active');
    if (ok) { el.classList.add('done'); if (stageDur[k] != null) el.querySelector('.ms').textContent = fmt(stageDur[k]); }
    else if (k === curStage) el.classList.add('error');
  });
  $('now-spin').className = 'spinner ' + (ok ? 'done' : 'error');
  $('now-stage').textContent = ok ? `완료 — 총 ${fmt(elapsed)}` : '오류';
  $('now-detail').textContent = msg || '';
  $('bar-fill').style.width = ok ? '100%' : $('bar-fill').style.width;
  $('prog-eta').textContent = '';
  if (ok) { $('log').hidden = true; $('btn-log').textContent = '로그 보기'; }
}
function appendLog(line) {
  const el = $('log');
  el.textContent += line + '\n';
  if (el.textContent.length > 60000) el.textContent = el.textContent.slice(-50000);
  el.scrollTop = el.scrollHeight;
  // 로그의 마지막 줄을 상태줄에도 짧게 보여준다
  const short = line.replace(/^\[\d+(\.\d+)?s\]\s*/, '').replace(/^\$\s.*/, '');
  if (short && !/Loading weights|^\s*$/.test(short)) $('now-detail').textContent = short.slice(0, 140);
}
function startTimer() { stopTimer(); timer = setInterval(updateBar, 1000); }
function stopTimer() { if (timer) clearInterval(timer); timer = null; }

// ---------- 실제 실행 ----------
async function runLive() {
  if (!cur || !online) return;
  const region = $('region').value;
  const did = cur.id;
  $('result-card').hidden = true;
  let job;
  try {
    const r = await fetch(`${API}/api/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ did, region, skip_summarize: $('skip-sum').checked }),
    });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || `HTTP ${r.status}`); }
    job = await r.json();
  } catch (e) {
    $('progress').hidden = false; resetProgress();
    $('now-spin').className = 'spinner error'; $('now-stage').textContent = '실행 요청 실패'; $('now-detail').textContent = e.message;
    return;
  }
  watch(job.job_id, did, region, job.position);
}

// 작업에 붙어서 진행 상황을 본다 (내가 시작한 것이든, 서버에서 이미 돌고 있는 것이든)
function watch(jobId, did, region, position) {
  closeES();
  resetProgress();
  $('progress').hidden = false;
  watching = { jobId, did, region, t0: Date.now() };
  $('prog-msg').textContent = `${did} × ${region}`;
  if (position > 1) { $('now-stage').textContent = `대기열 ${position}번째`; $('now-detail').textContent = '앞 작업이 끝나면 자동으로 시작합니다'; }
  else { $('now-stage').textContent = '시작 대기 중…'; }
  startTimer();
  es = new EventSource(`${API}/api/jobs/${jobId}/events`);
  updateButtons(); updateAttachBanner();
  $('progress').scrollIntoView({ behavior: 'smooth', block: 'start' });
  es.onmessage = ev => {
    let e; try { e = JSON.parse(ev.data); } catch (_) { return; }
    switch (e.type) {
      case 'queued': $('now-stage').textContent = `대기열 ${e.position}번째`; break;
      case 'start': watching.t0 = Date.now() - (e.t || 0) * 1000; setStage(1); break;
      case 'stage': setStage(e.stage); break;
      case 'log': appendLog(`[${e.t}s] ${e.text}`); break;
      case 'cmd': appendLog(`$ ${e.text}`); break;
      case 'timing': appendLog(`[${e.t}s] ⏱ ${e.stage}: ${e.sec}s`); break;
      case 'done':
        finishProgress(true, e.elapsed); closeES();
        showResult(`${API}${e.result_url}`, `실시간 실행 결과 · ${did} × ${region} (${fmt(e.elapsed)})`);
        break;
      case 'error':
        finishProgress(false, 0, e.message); closeES();
        appendLog(`!! ${e.message}`); $('log').hidden = false;
        break;
    }
  };
  es.onerror = () => { closeES(); pollJob(jobId, did, region); };
}
function closeES() { if (es) { es.close(); es = null; } updateButtons(); }
async function pollJob(jobId, did, region) {
  $('now-detail').textContent = '연결이 끊겨 상태를 폴링합니다…';
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const j = await getJson(`${API}/api/jobs/${jobId}`);
      if (j.status === 'done') { finishProgress(true, j.elapsed); showResult(`${API}${j.result_url}`, `실시간 실행 결과 · ${did} × ${region} (${fmt(j.elapsed)})`); return; }
      if (j.status === 'error') { finishProgress(false, 0, j.error); return; }
      $('now-detail').textContent = `실행 중 (폴링) · ${fmt(j.elapsed)}`;
    } catch (e) { /* 다음 폴링 */ }
  }
}

// ---------- 사전 계산 ----------
function showPrecomputed() {
  if (!cur) return;
  const region = $('region').value;
  if (!es) $('progress').hidden = true;
  const url = online ? `${API}/api/precomputed/${encodeURIComponent(cur.id)}/${encodeURIComponent(region)}`
                     : `precomputed/${encodeURIComponent(cur.id)}/${encodeURIComponent(region)}.html`;
  showResult(url, `사전 계산 결과 · ${cur.id} × ${region}`);
}
function showResult(url, title) {
  $('result-title').textContent = title;
  $('result-open').href = url;
  const f = $('result');
  f.src = url;
  $('result-card').hidden = false;
  f.onload = () => { try { const h = f.contentDocument && f.contentDocument.documentElement.scrollHeight; if (h) f.style.height = Math.min(Math.max(h + 20, 600), 4000) + 'px'; } catch (e) {} };
  $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------- 부트 ----------
$('region').innerHTML = REGIONS.map(r => `<option value="${r}">${r}</option>`).join('');
$('btn-live').onclick = runLive;
$('btn-pre').onclick = showPrecomputed;
$('btn-log').onclick = () => { $('log').hidden = !$('log').hidden; $('btn-log').textContent = $('log').hidden ? '로그 보기' : '로그 닫기'; };
$('btn-attach').onclick = () => {
  if (!serverCurrent) return;
  if (dialogues.some(d => d.id === serverCurrent.did)) selectDialogue(serverCurrent.did, true);
  $('region').value = serverCurrent.region;
  watch(serverCurrent.job_id, serverCurrent.did, serverCurrent.region, 0);
};
(async () => {
  await checkHealth();
  await loadDialogues();
  setInterval(checkHealth, 10000);
  const q = new URLSearchParams(location.search);
  if (q.get('d')) selectDialogue(q.get('d'));
  if (q.get('region') && REGIONS.includes(q.get('region'))) $('region').value = q.get('region');
  if (q.get('pre') && cur) showPrecomputed();
  if (q.get('mock')) { // test-only: 진행 화면 정적 확인용
    $('progress').hidden = false; resetProgress();
    watching = { jobId: 'mock', did: cur ? cur.id : 'd_hi_01', region: $('region').value, t0: Date.now() - 150000 };
    const n = +q.get('mock'); let t = watching.t0;
    for (let k = 1; k <= n; k++) { stageStart[k] = t; if (k < n) { stageDur[k] = STAGES[k - 1].sec; t += STAGES[k - 1].sec * 1000; } }
    curStage = n - 1; setStage(n); stageStart[n] = Date.now() - 12000;
    appendLog('[150.2s] [경상남도] description 로드: 2237개 장소'); appendLog('[153.0s]   [d_hi_03] 병렬 검수 29개 (workers=8, 37.1s)');
    updateBar();
  }
})();
})();
