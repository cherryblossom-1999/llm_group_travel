#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — 데모 API 서버 (FastAPI)

  GET  /api/health                       서버·Gemma 상태, 큐 길이
  GET  /api/dialogues                    대화록 10개 메타
  GET  /api/regions                      권역 목록
  POST /api/run  {did, region, skip_summarize?, text?}  -> {job_id, position}
  GET  /api/jobs/{job_id}/events         SSE (stage / log / timing / done / error)
  GET  /api/jobs/{job_id}                작업 상태(JSON)
  GET  /api/result/{did}/{region}        실시간 실행 결과 대시보드 HTML
  GET  /api/precomputed/{did}/{region}   배치(사전 계산) 결과 대시보드 HTML
  /                                      site/ 정적 파일 (직접 포트 공개 시 프론트도 여기서 서빙)

GPU 1개이므로 작업은 워커 스레드 1개가 순서대로 처리한다.

실행:
  bash run_server.sh      (nohup, 포트 8090)
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import urllib.request
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import live_runner as lr

HERE = Path(__file__).resolve().parent
SITE = HERE / "docs"
MAX_QUEUE = 5
MAX_TEXT_CHARS = 6000

app = FastAPI(title="Group Travel Recommender Demo API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------
# 작업 큐
# ------------------------------------------------------------
class Job:
    def __init__(self, did: str, region: str, skip_summarize: bool, text: str | None):
        self.id = uuid.uuid4().hex[:10]
        self.did, self.region = did, region
        self.skip_summarize, self.text = skip_summarize, text
        self.status = "queued"          # queued | running | done | error
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: Path | None = None
        self.error: str | None = None

    def emit(self, ev: dict) -> None:
        ev = {**ev, "t": round(time.time() - (self.started or self.created), 1)}
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def to_dict(self) -> dict:
        return {"job_id": self.id, "did": self.did, "region": self.region, "status": self.status,
                "position": queue_position(self), "error": self.error,
                "result_url": f"/api/result/{self.did}/{self.region}" if self.result else None,
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1)}


JOBS: dict[str, Job] = {}
QUEUE: deque[Job] = deque()
_qlock = threading.RLock()   # health() 안에서 to_dict()->queue_position() 재진입
_current: Job | None = None


def queue_position(job: Job) -> int:
    with _qlock:
        for i, j in enumerate(QUEUE):
            if j is job:
                return i + 1
    return 0


def _worker() -> None:
    global _current
    while True:
        job = None
        with _qlock:
            if QUEUE:
                job = QUEUE.popleft()
                _current = job
        if job is None:
            time.sleep(0.3)
            continue
        job.status = "running"
        job.started = time.time()
        job.emit({"type": "start", "did": job.did, "region": job.region})
        try:
            out = lr.run_live(job.did, job.region, text=job.text,
                              skip_summarize=job.skip_summarize, log=job.emit)
            job.result = out
            job.status = "done"
            job.finished = time.time()
            job.emit({"type": "done", "result_url": f"/api/result/{job.did}/{job.region}",
                      "elapsed": round(job.finished - job.started, 1)})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)
            job.finished = time.time()
            job.emit({"type": "error", "message": str(e)})
        finally:
            with _qlock:
                _current = None


threading.Thread(target=_worker, daemon=True, name="demo-worker").start()


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
class RunRequest(BaseModel):
    did: str
    region: str
    skip_summarize: bool = False
    text: str | None = None


_gemma_last_ok = 0.0


def gemma_ok() -> bool:
    """Gemma 서버 준비 여부. 직렬 서버는 생성 중 /health 가 막히므로
    타임아웃이면 포트가 열려 있는지로 '작업 중' 판정한다."""
    global _gemma_last_ok
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=2) as r:
            ok = bool(json.loads(r.read().decode()).get("model_loaded"))
            if ok:
                _gemma_last_ok = time.time()
            return ok
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            # 포트는 열림 = 프로세스 살아 있음(생성 중이라 응답 지연). 최근 10분 내 정상이었으면 OK
            return (time.time() - _gemma_last_ok) < 600 or _gemma_last_ok == 0.0
    except Exception:
        return False


@app.get("/api/health")
def health():
    with _qlock:
        q = len(QUEUE)
        cur = _current.to_dict() if _current else None
    return {"status": "ok", "gemma": gemma_ok(), "queue": q, "current": cur,
            "time": time.time()}


@app.get("/api/regions")
def regions():
    return lr.REGIONS


@app.get("/api/dialogues")
def dialogues():
    return [{k: v for k, v in d.items()} for d in lr.list_dialogues()]


@app.post("/api/run")
def run(req: RunRequest):
    if req.region not in lr.REGIONS:
        raise HTTPException(400, "권역 오류")
    known = {d["id"] for d in lr.list_dialogues()}
    if req.text is None and req.did not in known:
        raise HTTPException(400, "대화록 ID 오류")
    if req.text is not None:
        if len(req.text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"대화 길이 제한 {MAX_TEXT_CHARS}자")
        req.did = "user_" + uuid.uuid4().hex[:8]
    if not gemma_ok():
        raise HTTPException(503, "LLM 서버(Gemma)가 준비되지 않았습니다")
    with _qlock:
        if len(QUEUE) >= MAX_QUEUE:
            raise HTTPException(429, "대기열이 가득 찼습니다. 잠시 후 다시 시도하세요")
        job = Job(req.did, req.region, req.skip_summarize, req.text)
        JOBS[job.id] = job
        QUEUE.append(job)
        pos = len(QUEUE)
    return {"job_id": job.id, "position": pos, "did": job.did, "region": job.region}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "작업 없음")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "작업 없음")

    async def gen():
        i = 0
        last_beat = time.time()
        while True:
            with job.cond:
                pending = job.events[i:]
            for ev in pending:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                i += 1
                if ev["type"] in ("done", "error"):
                    return
            if job.status == "queued":
                pos = queue_position(job)
                if time.time() - last_beat > 5:
                    yield f"data: {json.dumps({'type': 'queued', 'position': pos})}\n\n"
                    last_beat = time.time()
            elif time.time() - last_beat > 15:
                yield ": keep-alive\n\n"
                last_beat = time.time()
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/result/{did}/{region}", response_class=HTMLResponse)
def result(did: str, region: str):
    p = lr.RUNS / "v4" / did / f"{region}.html"
    if not p.exists():
        raise HTTPException(404, "결과 없음")
    return FileResponse(str(p), media_type="text/html")


@app.get("/api/precomputed/{did}/{region}", response_class=HTMLResponse)
def precomputed(did: str, region: str):
    if region not in lr.REGIONS:
        raise HTTPException(400, "권역 오류")
    p = lr.precomputed_html(did, region)
    if p is None:
        raise HTTPException(404, "사전 계산 결과 없음 (배치 미완료)")
    return FileResponse(str(p), media_type="text/html")


# 정적 프론트 (직접 포트 공개 시)
if SITE.exists():
    app.mount("/", StaticFiles(directory=str(SITE), html=True), name="site")
