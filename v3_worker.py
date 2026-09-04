#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3_worker.py — talk_pipeline_v3 상주 워커 (요약 → bge-m3 검색 → Wilson/Nash)

실행마다 새 프로세스가 bge-m3(15초)·e5-large(19초)·권역 임베딩 JSON(5~10초)을 다시 읽던 것을
한 번만 로드해 메모리에 들고, 표준입력으로 들어오는 작업을 같은 프로세스 안에서 처리한다.
결과 파일·로그 형식은 pipeline.py CLI 와 동일 (run_pipeline 을 그대로 호출).

프로토콜 (한 줄 JSON):
  stdin : {"input": "<txt>", "region": "강원도", "skip_summarize": false}
  stdout: 파이프라인 로그 그대로 … 마지막에
          __RESULT__ {"ok": true, "sec": 12.3}   또는   __RESULT__ {"ok": false, "error": "..."}
  준비 완료 시 __READY__ 한 줄.

live_runner.py 가 기동/재기동한다. 직접 실행: (cwd=talk_pipeline_v3, OUTPUT_ROOT 지정)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

V3 = Path(__file__).resolve().parent.parent / "talk_pipeline_v3"
os.chdir(V3)
sys.path.insert(0, str(V3))

from modules.llm_client import load_env  # noqa: E402
load_env(V3 / ".env")

import pipeline  # noqa: E402  (OUTPUT_ROOT 환경변수를 import 시점에 읽는다)
from modules.retrieval_textonly import load_embedding_index, load_retrieval_model  # noqa: E402
from modules.wilson_nash import _get_st_model  # noqa: E402

REGIONS = ["강원도", "경상남도", "경상북도", "수도권", "전라남도",
           "전라북도", "제주도", "충청남도", "충청북도"]
RETRIEVAL_MODEL = os.environ.get("RETRIEVAL_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "intfloat/multilingual-e5-large")
EMBEDDING_VERSION = os.environ.get("EMBEDDING_VERSION", "version_a")


def say(msg: str) -> None:
    print(msg, flush=True)


def warm(regions: list[str]) -> None:
    t0 = time.time()
    say(f"[worker] 모델 로딩: {RETRIEVAL_MODEL}, {RERANK_MODEL}")
    load_retrieval_model(RETRIEVAL_MODEL)
    _get_st_model(RERANK_MODEL)
    say(f"[worker] 모델 준비 {time.time()-t0:.1f}s")
    for r in regions:
        f = pipeline.EMBEDDINGS_DIR / f"{r}_bge_m3_{EMBEDDING_VERSION}.json"
        if f.exists():
            t1 = time.time()
            load_embedding_index(str(f))
            say(f"[worker] 인덱스 상주: {r} ({time.time()-t1:.1f}s)")
    say(f"[worker] 워밍업 완료 {time.time()-t0:.1f}s")


def run_job(job: dict) -> dict:
    t0 = time.time()
    pipeline.run_pipeline(
        input_file=job["input"],
        skip_summarize=bool(job.get("skip_summarize", False)),
        limit=int(job.get("limit", 1)),
        region=job["region"],
        method="nash_only",
        method_cut=100,
        retrieval_top_k=300,
        top_k_nash_category=10,
        reason_mode="off",
        empty_pref_policy="uniform",
        retrieval_model=RETRIEVAL_MODEL,
        rerank_model=RERANK_MODEL,
        embedding_version=EMBEDDING_VERSION,
        llm_backend="vllm",
    )
    return {"ok": True, "sec": round(time.time() - t0, 1)}


def main() -> int:
    regions = REGIONS if os.environ.get("WORKER_WARM_REGIONS", "all") == "all" else \
        [r for r in os.environ["WORKER_WARM_REGIONS"].split(",") if r]
    try:
        warm(regions)
    except Exception as e:  # noqa: BLE001
        say(f"[worker] 워밍업 실패: {e}")
        traceback.print_exc()
    say("__READY__")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "__QUIT__":
            break
        try:
            job = json.loads(line)
            res = run_job(job)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        say("__RESULT__ " + json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
