/* 그룹 여행지 추천 데모 — 프론트
 * 대화록 + 권역 선택 → (실제 실행: API/SSE) 또는 (사전 계산 결과) → 기존 대시보드 HTML을 iframe 에 표시 */
(() => {
'use strict';

const CFG = window.DEMO_CONFIG || {};
const API = (CFG.apiBase || '').replace(/\/$/, '');
const REGIONS = ['강원도', '경상남도', '경상북도', '수도권', '전라남도', '전라북도', '제주도', '충청남도', '충청북도'];
const CONSENSUS = { hi: '합의 높음', lo: '합의 낮음' };

const $ = id => document.getElementById(id);
// test-only (?sync=1): 헤드리스 캡처가 데이터 로딩을 기다리도록 동기 XHR 사용
const SYNC = new URLSearchParams(location.search).get('sync');
async function getJson(url) {
  if (SYNC) { const x = new XMLHttpRequest(); x.open('GET', url, false); x.send(); if (x.status !== 200) throw new Error(x.status); return JSON.parse(x.responseText); }
  const r = await fetch(url, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); return r.json();
}
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

let dialogues = [];
let cur = null;
let online = false;
let es = null;              // EventSource
let timer = null;
let stageStart = {};

// ---------- 서버 상태 ----------
async function checkHealth() {
  try {
    const j = await getJson(`${API}/api/health`);
    online = !!j.gemma;
    setStatus(online ? 'on' : 'warn',
      online ? `서버 온라인${j.queue ? ` · 대기 ${j.queue}` : ''}${j.current ? ' · 실행 중' : ''}` : '서버는 켜졌지만 LLM 미준비');
  } catch (e) {
    online = false;
    setStatus('off', '실행 서버 오프라인 — 사전 계산 결과만 볼 수 있습니다');
  }
  updateButtons();
}
function setStatus(cls, text) {
  const el = $('status');
  el.className = `status ${cls}`;
  $('status-text').textContent = text;
}

// ---------- 대화 목록 ----------
async function loadDialogues() {
  try {
    dialogues = await getJson(`${API}/api/dialogues`);
  } catch (e) {
    // 서버 오프라인: 정적 목록으로 폴백
    try { dialogues = await getJson('dialogues.json'); }
    catch (e2) { dialogues = []; }
  }
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
function selectDialogue(id) {
  cur = dialogues.find(d => d.id === id) || null;
  if (!cur) return;
  renderList();
  $('empty').hidden = true;
  $('r-did').textContent = cur.id;
  $('r-label').textContent = `${cur.label} · ${CONSENSUS[cur.consensus] || ''}`.replace(/ · $/, '');
  renderTranscript(cur.text || '');
  $('transcript-card').hidden = false;
  $('result-card').hidden = true;
  $('progress').hidden = true;
  updateButtons();
}
function renderTranscript(text) {
  const lines = text.replace(/^﻿/, '').split('\n').filter(l => l.trim());
  const rows = [];
  for (const l of lines) {
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
  $('prog-msg').textContent = '';
  $('prog-time').textContent = '';
  stageStart = {};
}
function setStage(n) {
  const now = Date.now();
  document.querySelectorAll('.step').forEach(el => {
    const k = +el.dataset.step;
    if (k < n) {
      if (el.classList.contains('active')) {
        el.classList.remove('active');
        if (stageStart[k]) el.querySelector('.ms').textContent = fmt((now - stageStart[k]) / 1000);
      }
      el.classList.add('done');
    } else if (k === n) {
      if (!el.classList.contains('active')) { el.classList.add('active'); stageStart[k] = stageStart[k] || now; }
    }
  });
}
function finishStages() {
  const now = Date.now();
  document.querySelectorAll('.step').forEach(el => {
    const k = +el.dataset.step;
    if (el.classList.contains('active') && stageStart[k]) el.querySelector('.ms').textContent = fmt((now - stageStart[k]) / 1000);
    el.classList.remove('active'); el.classList.add('done');
  });
}
const fmt = s => s >= 60 ? `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초` : `${Math.round(s)}초`;
function appendLog(line) {
  const el = $('log');
  el.textContent += line + '\n';
  if (el.textContent.length > 60000) el.textContent = el.textContent.slice(-50000);
  el.scrollTop = el.scrollHeight;
}
function startTimer(t0) {
  stopTimer();
  timer = setInterval(() => { $('prog-time').textContent = `경과 ${fmt((Date.now() - t0) / 1000)}`; }, 1000);
}
function stopTimer() { if (timer) clearInterval(timer); timer = null; }

// ---------- 실제 실행 ----------
async function runLive() {
  if (!cur || !online) return;
  const region = $('region').value;
  resetProgress();
  $('progress').hidden = false;
  $('result-card').hidden = true;
  $('prog-msg').textContent = '작업 요청 중…';
  let job;
  try {
    const r = await fetch(`${API}/api/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ did: cur.id, region, skip_summarize: $('skip-sum').checked }),
    });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || `HTTP ${r.status}`); }
    job = await r.json();
  } catch (e) {
    $('prog-msg').textContent = `실행 요청 실패: ${e.message}`;
    return;
  }
  const t0 = Date.now();
  startTimer(t0);
  $('prog-msg').textContent = job.position > 1 ? `대기열 ${job.position}번째 — 앞 작업이 끝나면 시작합니다` : '시작 대기 중…';
  es = new EventSource(`${API}/api/jobs/${job.job_id}/events`);
  updateButtons();
  es.onmessage = ev => {
    let e; try { e = JSON.parse(ev.data); } catch (_) { return; }
    switch (e.type) {
      case 'queued': $('prog-msg').textContent = `대기열 ${e.position}번째…`; break;
      case 'start': $('prog-msg').textContent = `실행 중 — ${e.did} × ${e.region}`; setStage(1); break;
      case 'stage': setStage(e.stage); break;
      case 'log': appendLog(`[${e.t}s] ${e.text}`); break;
      case 'cmd': appendLog(`$ ${e.text}`); break;
      case 'timing': appendLog(`[${e.t}s] ⏱ ${e.stage}: ${e.sec}s`); break;
      case 'done':
        finishStages(); stopTimer(); closeES();
        $('prog-msg').textContent = `완료 — 총 ${fmt(e.elapsed)}`;
        $('prog-time').textContent = '';
        showResult(`${API}${e.result_url}`, `실시간 실행 결과 · ${cur.id} × ${region}`);
        break;
      case 'error':
        stopTimer(); closeES();
        document.querySelectorAll('.step.active').forEach(el => el.classList.add('error'));
        $('prog-msg').textContent = `오류: ${e.message}`;
        appendLog(`!! ${e.message}`);
        $('log').hidden = false;
        break;
    }
  };
  es.onerror = () => {
    // 연결 끊김: 상태를 폴링해서 마무리
    closeES();
    pollJob(job.job_id, region);
  };
}
function closeES() { if (es) { es.close(); es = null; } updateButtons(); }
async function pollJob(jobId, region) {
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const j = await (await fetch(`${API}/api/jobs/${jobId}`, { cache: 'no-store' })).json();
      if (j.status === 'done') { finishStages(); stopTimer(); $('prog-msg').textContent = `완료 — 총 ${fmt(j.elapsed)}`; showResult(`${API}${j.result_url}`, `실시간 실행 결과 · ${cur.id} × ${region}`); return; }
      if (j.status === 'error') { stopTimer(); $('prog-msg').textContent = `오류: ${j.error}`; return; }
      $('prog-msg').textContent = `실행 중 (연결 재시도) · ${fmt(j.elapsed)}`;
    } catch (e) { /* 다음 폴링 */ }
  }
}

// ---------- 사전 계산 ----------
function showPrecomputed() {
  if (!cur) return;
  const region = $('region').value;
  $('progress').hidden = true;
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
(async () => {
  await checkHealth();
  await loadDialogues();
  setInterval(checkHealth, 20000);
  const q = new URLSearchParams(location.search);
  if (q.get('d')) selectDialogue(q.get('d'));
  if (q.get('region') && REGIONS.includes(q.get('region'))) $('region').value = q.get('region');
  if (q.get('pre') && cur) showPrecomputed();
})();
})();
