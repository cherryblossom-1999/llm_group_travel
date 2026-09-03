#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_precomputed.py — 배치(output_v6/group_travel_dialogues) 결과를 정적 HTML 로 내보내기

  docs/precomputed/<did>/<region>.html  (서버 오프라인 시 GitHub Pages 폴백)

사용:  python export_precomputed.py          # 완료된 (대화, 권역) 전부
"""
from __future__ import annotations

from pathlib import Path

import live_runner as lr

OUT = lr.HERE / "docs" / "precomputed"


def main() -> int:
    stem_dir = lr.PRECOMPUTED_ROOT / lr.PRECOMPUTED_STEM
    n = 0
    for d in lr.list_dialogues():
        did = d["id"]
        for region in lr.REGIONS:
            run_dir = stem_dir / region / f"group_query_runs_{lr.base.RUN_METHOD}"
            # 백필까지 끝난 권역만 (.done 마커 + partial_backfill.jsonl 유무는 대화별로 다를 수 있어 마커만 본다)
            if not (run_dir / ".done_v4_hybrid").exists() or not (run_dir / did).exists():
                continue
            try:
                html = lr.build_dashboard_html(stem_dir, did, region, title_note=f"사전 계산 결과 · {region}")
            except RuntimeError as e:
                print(f"  skip {did}/{region}: {e}")
                continue
            p = OUT / did / f"{region}.html"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html, encoding="utf-8")
            n += 1
    print(f"[완료] {n}개 HTML -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
