# llm_group_travel — 그룹 여행지 추천 파이프라인 데모

다자간 여행 계획 대화를 입력으로, 참가자별·그룹 선호를 LLM으로 요약하고
bge-m3 임베딩 검색 → 카테고리 필터 + Nash 합의 재랭킹 → LLM 검수 → 부분충족 백필을 거쳐
권역별 여행지 Top-10 을 추천하는 파이프라인(`talk_pipeline_v6`)의 공개 데모입니다.

- 데모: https://cherryblossom-1999.github.io/llm_group_travel/
- `docs/` — 정적 프론트 (GitHub Pages). 대화록 + 권역을 고르면 연구실 GPU 서버에서 파이프라인이 실제로 실행되고, 단계별 진행과 결과 대시보드가 표시됩니다.
- `server.py`, `live_runner.py` — 실행 API 서버 (FastAPI, SSE). 대화 1개 × 권역 1개를 실제 파이프라인으로 실행합니다.
- `docs/precomputed/` — 같은 파이프라인이 미리 산출한 결과 (서버 오프라인 시 폴백).

요약·검수 모델: Gemma-4-26B-A4B (로컬) · 검색: bge-m3 · 합의: Wilson 점수 + Nash 곱
