#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_runner.py — 대화 1개 × 권역 1개를 실제 파이프라인으로 실행하고 대시보드 HTML 생성

단계:
  1. 입력 txt 준비 (runs/input/<did>.txt)
  2. v3 상주 워커(v3_worker.py, 모델·인덱스 메모리 상주) : 요약(LLM) → bge-m3 검색 → Nash  -> runs/v3/<did>/<r>/
     └ 요약이 끝나는 즉시 백필용 '조건 분해' LLM 호출을 검색·Nash 와 겹쳐서 미리 수행 (캐시에 저장)
  3. rerank_from_saved.py : 카테고리 필터 + Nash 재랭킹 + LLM 검수(병렬, 10개 통과 시 조기 종료) -> runs/v4/<did>/<r>/
  4. backfill_partial_match.py : 부분충족 백필(LLM, 병렬)
  5. 대시보드 HTML (analyze_results_v4_final2 템플릿) -> runs/v4/<did>/<r>.html

CLI:
  python live_runner.py d_hi_01 강원도 [--skip-summarize]
  python live_runner.py --precomputed d_hi_01 강원도   # output_v6 배치 결과로 HTML 만 생성
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
HTM = HERE.parent
V3 = HTM / "talk_pipeline_v3"
V6 = HTM / "talk_pipeline_v6"
PY = "/home/teedlab/miniconda3/envs/torch_env/bin/python"
RUNS = HERE / "runs"
DIALOGUE_DIR = V6 / "group_travel_dialogues"
PRECOMPUTED_ROOT = V6 / "output_v6"
PRECOMPUTED_STEM = "group_travel_dialogues"
REGIONS = ["강원도", "경상남도", "경상북도", "수도권", "전라남도",
           "전라북도", "제주도", "충청남도", "충청북도"]
GEN_CONCURRENCY = os.environ.get("DEMO_GEN_CONCURRENCY", "16")  # Gemma 배칭 서버 배치 크기와 맞춤
TOPK = os.environ.get("DEMO_TOPK", "30")            # 검수 깊이 (배치 실험은 50; 데모 속도를 위해 30)
EARLY_EXIT = os.environ.get("DEMO_EARLY_EXIT", "10")  # Nash 순서로 앞 10개 통과 확보 시 검수 조기 종료 (결과 동일)

sys.path.insert(0, str(V6))
import analyze_results_v4 as base            # noqa: E402
import analyze_results_v4_final2 as final2   # noqa: E402

Log = Callable[[dict], None]

STAGE_MARKS = [  # (로그 부분 문자열, 단계 번호)
    ("[1/3] Summarize", 1),
    ("[2/3] Retrieval", 2),
    ("[3/3] Wilson/Nash", 3),
    ("Nash 재랭킹 완료", 4),
    ("description 로드", 4),
    ("[backfill]", 5),
]

UTT_RE = re.compile(r"^\s*(?P<name>[^:(（]+?)\s*[\(（](?P<sid>P\d+)[\)）]\s*[:：]\s*(?P<utt>.+)$")
HEADER_RE = re.compile(r"^\s*\[(?P<id>[^\]]+)\]")


# ------------------------------------------------------------
# 대화 목록 / 입력 준비
# ------------------------------------------------------------
def list_dialogues() -> list[dict]:
    out = []
    for p in sorted(DIALOGUE_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8-sig")
        lines = [l for l in text.splitlines() if l.strip()]
        header = lines[0] if lines else ""
        m = HEADER_RE.match(header)
        did = m.group("id") if m else p.stem
        label = header.split("]", 1)[1].strip() if "]" in header else ""
        participants = ""
        for l in lines[1:3]:
            if l.startswith("참가자"):
                participants = l.split(":", 1)[-1].strip()
        turns = [UTT_RE.match(l) for l in lines]
        turns = [t for t in turns if t]
        out.append({
            "id": did,
            "label": label,
            "consensus": "hi" if "_hi_" in did else ("lo" if "_lo_" in did else ""),
            "participants": participants,
            "n_turns": len(turns),
            "group_size": len({t.group("sid") for t in turns}),
            "preview": turns[0].group("utt")[:80] if turns else "",
            "text": text,
        })
    return out


def prepare_input(did: str, text: str | None = None) -> Path:
    """단일 대화 블록 txt 생성. text 가 None 이면 group_travel_dialogues/<did>.txt 사용."""
    inp_dir = RUNS / "input"
    inp_dir.mkdir(parents=True, exist_ok=True)
    if text is None:
        src = DIALOGUE_DIR / f"{did}.txt"
        if not src.exists():
            raise FileNotFoundError(f"대화록 없음: {src}")
        text = src.read_text(encoding="utf-8-sig")
    text = text.replace("\r\n", "\n").strip("﻿\n ")
    if not HEADER_RE.match(text.splitlines()[0]):
        n = len({m.group("sid") for m in (UTT_RE.match(l) for l in text.splitlines()) if m})
        text = f"[{did}] 사용자 입력 {n}명\n" + "-" * 60 + "\n" + text
    dst = inp_dir / f"{did}.txt"
    dst.write_text("=" * 60 + "\n" + text + "\n", encoding="utf-8")
    return dst


# ------------------------------------------------------------
# 환경 / 로그
# ------------------------------------------------------------
def _env() -> dict:
    env = dict(os.environ)
    env["PATH"] = "/home/teedlab/miniconda3/envs/torch_env/bin:" + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["GEN_CONCURRENCY"] = GEN_CONCURRENCY
    env["DEMO_EARLY_EXIT"] = EARLY_EXIT
    env["HF_HUB_OFFLINE"] = "1"          # 허깅페이스 허브 HEAD/GET 50여 회 생략 (모델은 로컬 캐시에 있음)
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def _emit_line(line: str, log: Log, on_line: Callable[[str], None] | None = None) -> None:
    line = line.rstrip("\n")
    if not line.strip() or line.startswith("####"):
        return
    for mark, st in STAGE_MARKS:
        if mark in line:
            log({"type": "stage", "stage": st})
    log({"type": "log", "text": line[:400]})
    if on_line:
        on_line(line)


def _run(cmd: list[str], cwd: Path, env: dict, log: Log) -> int:
    log({"type": "cmd", "text": " ".join(cmd[-12:])})
    proc = subprocess.Popen(
        ["stdbuf", "-oL", "-eL"] + cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _emit_line(line, log)
    return proc.wait()


# ------------------------------------------------------------
# v3 상주 워커 (요약 → 검색 → Nash), 모델·인덱스 메모리 상주
# ------------------------------------------------------------
class V3Worker:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.state = "stopped"          # stopped | warming | ready | busy | failed
        self.warm_log: list[str] = []
        self.warm_sec: float | None = None
        self.jobs_done = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        """워커 기동 + 워밍업 완료(__READY__)까지 대기. 호출자는 lock 을 잡고 있어야 한다."""
        self.stop()
        env = _env()
        env["OUTPUT_ROOT"] = str(RUNS / "v3")
        (RUNS / "v3").mkdir(parents=True, exist_ok=True)
        self.state = "warming"
        self.warm_log = []
        t0 = time.time()
        self.proc = subprocess.Popen(
            [PY, "-u", str(HERE / "v3_worker.py")], cwd=str(V3), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.strip():
                self.warm_log.append(line[:300])
                self.warm_log = self.warm_log[-200:]
            if line.strip() == "__READY__":
                self.state = "ready"
                self.warm_sec = round(time.time() - t0, 1)
                return
        self.state = "failed"
        raise RuntimeError("v3 워커가 준비 전에 종료됨:\n" + "\n".join(self.warm_log[-15:]))

    def stop(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    if self.proc.stdin:
                        self.proc.stdin.write("__QUIT__\n"); self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.state = "stopped"

    def ensure(self) -> None:
        with self.lock:
            if not self.alive():
                self.start()

    def run(self, job: dict, log: Log, on_line: Callable[[str], None] | None = None) -> dict:
        with self.lock:
            if not self.alive():
                log({"type": "log", "text": "[worker] v3 워커 기동 (모델 상주 워밍업)…"})
                self.start()
                log({"type": "log", "text": f"[worker] 워밍업 완료 {self.warm_sec}s"})
            assert self.proc and self.proc.stdin and self.proc.stdout
            self.state = "busy"
            try:
                self.proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                for line in self.proc.stdout:
                    if line.startswith("__RESULT__"):
                        res = json.loads(line[len("__RESULT__"):].strip())
                        self.jobs_done += 1
                        return res
                    _emit_line(line, log, on_line)
                raise RuntimeError("v3 워커가 작업 중 종료됨")
            finally:
                self.state = "ready" if self.alive() else "failed"

    def status(self) -> dict:
        return {"state": self.state, "alive": self.alive(), "warm_sec": self.warm_sec,
                "jobs_done": self.jobs_done, "last": self.warm_log[-1] if self.warm_log else ""}


WORKER = V3Worker()


# ------------------------------------------------------------
# 백필 조건 분해를 검색·Nash 와 겹쳐서 미리 수행 (같은 프롬프트·캐시 키 → 결과 동일)
# ------------------------------------------------------------
def _prefetch_decompose(did: str, region: str, refined_jsonl: Path, log: Log) -> None:
    try:
        import backfill_partial_match as bf  # V6 (sys.path)
        bf.load_env(V6 / ".env")
        gq = ""
        for line in refined_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("dialogue_id") == did:
                gq = str(d.get("group_query", "")).strip()
                break
        if not gq:
            return
        cache_p = RUNS / "v4" / did / region / "partial_backfill_cache.json"
        cache = {}
        if cache_p.exists():
            try:
                cache = json.loads(cache_p.read_text(encoding="utf-8"))
            except Exception:
                cache = {}
        key = f"cond::{did}::{bf.PROMPT_VER}"
        if key in cache:
            return
        t0 = time.time()
        conds = bf.decompose_query(bf.make_vllm_gen_fn(), gq)
        cache[key] = conds
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        log({"type": "log", "text": f"[prefetch] 백필 조건 분해 미리 수행 ({len(conds)}개, {time.time()-t0:.1f}s, 검색·Nash 와 병행)"})
    except Exception as e:  # noqa: BLE001
        log({"type": "log", "text": f"[prefetch] 조건 분해 선행 실패(무시, 백필 단계에서 수행): {e}"})


# ------------------------------------------------------------
# 대시보드 HTML
# ------------------------------------------------------------
def build_dashboard_html(v4_stem_dir: Path, did: str, region: str, title_note: str = "") -> str:
    rdir = v4_stem_dir / region
    transcripts = base.load_transcripts(rdir)
    refined = base.load_refined(rdir)
    methods_map = base.load_stage_methods(rdir, base.DISPLAY_K, base.RANK_DEPTH)
    if did not in methods_map:
        raise RuntimeError(f"결과 없음: {rdir} / {did}")
    final2.merge_backfill(rdir, methods_map, final2.FINAL_SIZE)
    meta = refined.get(did, {})
    data = {region: [{
        "id": did,
        "type": meta.get("type", ""),
        "group_size": meta.get("group_size", 0),
        "group_query": meta.get("group_query", ""),
        "key_aspects": meta.get("key_aspects", []),
        "transcript": transcripts.get(did, []),
        "speakers": meta.get("speakers", []),
        "methods": methods_map[did],
    }]}
    html = final2.build_dashboard(data, base.DISPLAY_K)
    if title_note:
        html = html.replace("</h1>", f" <span style='font-size:12px;color:#888;font-weight:400'>{title_note}</span></h1>", 1)
    return html


# ------------------------------------------------------------
# 실행
# ------------------------------------------------------------
def run_live(did: str, region: str, *, text: str | None = None,
             skip_summarize: bool = False, log: Log = lambda e: None) -> Path:
    if region not in REGIONS:
        raise ValueError(f"권역 오류: {region}")
    t_all = time.time()
    v3_root = RUNS / "v3"
    v4_root = RUNS / "v4"
    v3_root.mkdir(parents=True, exist_ok=True)
    v4_root.mkdir(parents=True, exist_ok=True)
    # rerank_from_saved 가 v3_root/step0_outputs_by_region/<region> 을 찾는다 -> 심볼릭 링크
    link = RUNS / "step0_outputs_by_region"
    if not link.exists():
        link.symlink_to(V3 / "step0_outputs_by_region")

    inp = prepare_input(did, text)
    log({"type": "stage", "stage": 1})
    log({"type": "log", "text": f"입력 준비: {inp.name} / 권역 {region}"})

    # 같은 대화의 다른 권역 요약이 있으면 재사용 가능
    stem_v3 = v3_root / did
    region_v3 = stem_v3 / region
    use_skip = False
    if skip_summarize:
        found = None
        for cand in sorted(stem_v3.glob("*/query_summaries_new.refined.jsonl")):
            found = cand
            break
        if found is not None:
            region_v3.mkdir(parents=True, exist_ok=True)
            for name in ("query_summaries_new.refined.jsonl", "query_summaries_new.refined.txt"):
                src = found.parent / name
                if src.exists() and src.resolve() != (region_v3 / name).resolve():
                    shutil.copyfile(src, region_v3 / name)
            use_skip = True
            log({"type": "log", "text": f"요약 재사용: {found.parent.name}"})

    # (1) v3 상주 워커: 요약 + 검색 + Nash. 요약이 끝나는 순간(검색 시작 로그) 백필 조건 분해를 병행 시작.
    env = _env()
    refined_jsonl = region_v3 / "query_summaries_new.refined.jsonl"
    prefetch: dict = {"thread": None}

    def on_line(line: str) -> None:
        if "[2/3] Retrieval" in line and prefetch["thread"] is None and refined_jsonl.exists():
            th = threading.Thread(target=_prefetch_decompose, args=(did, region, refined_jsonl, log), daemon=True)
            th.start()
            prefetch["thread"] = th

    t0 = time.time()
    log({"type": "cmd", "text": f"v3_worker: {inp.name} --region {region}" + (" --skip-summarize" if use_skip else "")})
    res = WORKER.run({"input": str(inp), "region": region, "skip_summarize": use_skip, "limit": 1}, log, on_line)
    if not res.get("ok"):
        raise RuntimeError(f"v3 파이프라인 실패: {res.get('error')}")
    log({"type": "timing", "stage": "v3", "sec": round(time.time() - t0, 1)})
    if prefetch["thread"] is None and refined_jsonl.exists():
        # 요약 재사용 등으로 검색 로그를 놓친 경우에도 조건 분해를 검수 단계와 병행
        th = threading.Thread(target=_prefetch_decompose, args=(did, region, refined_jsonl, log), daemon=True)
        th.start()
        prefetch["thread"] = th

    # (2) v4: 필터 + Nash 재랭킹 + LLM 검수 (병렬, 조기 종료)
    log({"type": "stage", "stage": 4})
    t0 = time.time()
    rc = _run([PY, "-u", "rerank_from_saved.py",
               "--v3-root", str(RUNS), "--v3-output-name", "v3",
               "--stem", did, "--region", region,
               "--output-root", str(v4_root),
               "--method", "serial_embed_nash", "--topk", TOPK, "--limit", "1",
               "--reason-mode", "llm", "--reviews-dir", str(V3 / "리뷰데이터"),
               "--filter-criterion", "hybrid"],
              cwd=V6, env=env, log=log)
    if rc != 0:
        raise RuntimeError(f"v4 재랭킹/검수 실패 (exit {rc})")
    log({"type": "timing", "stage": "v4", "sec": round(time.time() - t0, 1)})

    # (3) 부분충족 백필 (조건 분해는 이미 캐시에 있음)
    if prefetch["thread"] is not None:
        prefetch["thread"].join(timeout=120)
    log({"type": "stage", "stage": 5})
    log({"type": "log", "text": "[backfill] 부분충족 백필 시작"})
    t0 = time.time()
    rc = _run([PY, "-u", "backfill_partial_match.py",
               "--results-root", str(v4_root), "--stem", did,
               "--regions", region, "--reviews-dir", str(V3 / "리뷰데이터")],
              cwd=V6, env=env, log=log)
    if rc != 0:
        raise RuntimeError(f"백필 실패 (exit {rc})")
    log({"type": "timing", "stage": "backfill", "sec": round(time.time() - t0, 1)})

    # (4) 대시보드
    log({"type": "stage", "stage": 6})
    total = time.time() - t_all
    html = build_dashboard_html(v4_root / did, did, region,
                                title_note=f"실시간 실행 · {region} · {total/60:.1f}분")
    out = v4_root / did / f"{region}.html"
    out.write_text(html, encoding="utf-8")
    log({"type": "log", "text": f"대시보드 생성: {out.name} (총 {total/60:.1f}분)"})
    return out


def precomputed_html(did: str, region: str) -> Path | None:
    """배치(output_v6) 결과로 HTML 생성(캐시). 없으면 None."""
    stem_dir = PRECOMPUTED_ROOT / PRECOMPUTED_STEM
    marker = stem_dir / region / f"group_query_runs_{base.RUN_METHOD}" / did
    if not marker.exists():
        return None
    out_dir = RUNS / "precomputed" / did
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{region}.html"
    newest = max([f.stat().st_mtime for f in marker.iterdir()] + [marker.stat().st_mtime])
    if out.exists() and out.stat().st_mtime > newest:
        return out
    try:
        html = build_dashboard_html(stem_dir, did, region, title_note=f"사전 계산 결과 · {region}")
    except RuntimeError:
        return None
    out.write_text(html, encoding="utf-8")
    return out


# ------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("did")
    ap.add_argument("region")
    ap.add_argument("--skip-summarize", action="store_true")
    ap.add_argument("--precomputed", action="store_true")
    a = ap.parse_args()
    if a.precomputed:
        p = precomputed_html(a.did, a.region)
        print("->", p)
    else:
        def _p(e):
            if e["type"] == "log":
                print(e["text"], flush=True)
            else:
                print(json.dumps(e, ensure_ascii=False), flush=True)
        try:
            p = run_live(a.did, a.region, skip_summarize=a.skip_summarize, log=_p)
            print("->", p)
        finally:
            WORKER.stop()
