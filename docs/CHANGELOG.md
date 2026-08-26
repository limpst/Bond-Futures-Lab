# 📜 변경 이력 (CHANGELOG)

> `python tools/gen_changelog.py` 로 **git 이력에서 자동 생성**합니다 — 손으로 고치지 마세요.
> 커밋 20건 · 변경 파일 137건 · 최신 2026-08-26 23:24

각 줄의 뜻: `유형 · 제목` / `해시 · 시각 · 파일수 (+추가/−삭제)` / 건드린 영역.
**왜 그렇게 했나**는 커밋 본문에 있습니다 — `git show <해시>` 로 보세요.

## 2026-08-26

- **기능** · feat(lab): RV lab + RRL(direct RL) + Bayesian pair — 헤지비 감쇠를 정량화
  - `78f5262` · 23:24 · 6개 파일 (+1202/−5) · tools(6)
  - 세 가지를 추가하고, 그 결과로 지금까지의 실패 원인이 특정됐다.
- **기능** · feat(fx+backfill): USDKRW source found, CME gaps ARE recoverable
  - `378a589` · 20:03 · 15개 파일 (+852/−302) · data(4), frontend(1), reports(3)
  - Two corrections to claims I made earlier, both from measurement:
- **기능** · feat(pair): readiness gate for KTB10-ZN - rolling beta + honest FX status
  - `8ed601f` · 19:30 · 6개 파일 (+530/−45) · frontend(1), reports(1), tools(4)
  - - tools/verify_feed.py: independent collection probe (write-age, growth
- **수정** · fix(db): busy_timeout everywhere - the lock that was killing collectors
  - `3774ad2` · 18:51 · 21개 파일 (+330/−141) · data(1), frontend(1), reports(3)
  - Root cause found: collect_cme_ws.py died at startup with
- **기능** · feat(ops): admin console - live monitoring, data health, auto-backfill
  - `dafa918` · 13:44 · 16개 파일 (+6296/−100) · .gitignore(1), data(1), frontend(1)
  - - tools/data_health.py: session-aware gap analysis that separates
- **기능** · feat: main.py 단일 진입점 + 체결/호가 오분류 버그 수정
  - `f632e0b` · 12:13 · 21개 파일 (+1659/−117) · .gitignore(1), docs(1), frontend(1)
  - ■ 버그 — 체결이 전부 호가로 오분류되어 봉이 하나도 안 만들어졌다

## 2026-08-25

- **기능** · feat(i18n): KR/EN toggle via post-processing phrase map + RV/delta-one glossary rows
  - `0f46827` · 21:31 · 3개 파일 (+1045/−172) · frontend(1), tools(2)
  - - tools/i18n_en_map.py: ordered KR->EN phrase map applied to the
- **화면** · ui: rebrand page as Delta-One Spread Monitor - KTB10-ZN one-line summary header
  - `b883db8` · 21:04 · 2개 파일 (+24/−18) · frontend(1), tools(1)
- **기능** · feat: KTB 주간 실시간 수집(체결+호가) · 세션 인식 계량 · 수집 감시
  - `4b480de` · 14:29 · 7개 파일 (+711/−15) · tools(7)
  - ■ 어제 결론 정정 — 국내 실시간은 '미신청' 이 아니라 '주간 전용' 이었다
- **기능** · feat(ui): simulation dashboard - products/algorithm/verdict-diary/formal defs
  - `761a1dd` · 11:40 · 4개 파일 (+542/−80) · frontend(1), reports(2), tools(1)
  - Self-contained page (frontend/sim_dashboard.html, builder in tools/):
- **수정** · fix: econ report robust to multi-symbol universe (CME inflow crash)
  - `7d0c90e` · 10:16 · 3개 파일 (+65/−56) · reports(1), tools(2)
  - Overnight CME symbols (ZT/ZF/TN...) entered minbar; the 7-symbol pivot

## 2026-08-24

- **기능** · feat(cme): CME 채권선물 1분봉 WebSocket 수집기 — REST 가 막힌 자리를 뚫었다
  - `2bd56b6` · 18:45 · 2개 파일 (+332/−0) · tools(2)
  - ■ 발견 (2026-08-24 실측) — 앞선 판단을 뒤집는다
- **기능** · feat: Newey-West HAC ECM gate + daily econ metrics log
  - `19360d9` · 16:35 · 6개 파일 (+109/−48) · reports(2), tools(4)
  - - ecm_gamma_hac(): pure-python NW se, verified against statsmodels to
- **수정** · fix(ls): 토큰 캐시가 만료를 잘못 믿는 문제 + CME 시세 반영 진단기
  - `b642669` · 16:31 · 2개 파일 (+164/−0) · tools(2)
  - ■ 토큰 결함 (실측 2026-08-24)
- **분석** · analysis: ADF/OU/ECM(HAC)/microstructure/ACD + threshold-sweep sim on KTB3-KTB10 1-min bars
  - `ea894ae` · 14:40 · 3개 파일 (+401/−0) · reports(2), tools(1)
  - Key finding: ECM significance vanishes under Newey-West (t -3.28 OLS ->

## 2026-08-23

- **잡무** · chore: zseries.json gitignore (생성물)
  - `b3bbe8d` · 18:33 · 2개 파일 (+1/−1) · .gitignore(1), data(1)
- **기능** · feat: 유니버스 스캔 + health 원커맨드 + multi-pair z-score 차트
  - `17fdacc` · 18:33 · 6개 파일 (+183/−7) · data(1), frontend(1), tools(4)
  - - --scan: o3121 마스터 -> 상품별 근월물(월문자 F~Z 순번) -> o3106 거래량
- **문서** · docs: CME 분리 검증 확정 - 파이프라인/심볼 정상, 계좌 CME 시세 미신청이 원인
  - `051a5f9` · 17:30 · 1개 파일 (+2/−1) · README.md(1)
- **운영** · ops: run_collect.cmd - 작업 스케줄러(월~금 08:55, 5분 간격 x 7h) 러너
  - `ff25147` · 16:27 · 1개 파일 (+6/−0) · tools(1)
- **시작** · init: Bond Futures Lab - KTB+CME 델타원 z-score 랩 (개인 소액·학습, 별도 프로젝트)
  - `7e8571f` · 16:12 · 10개 파일 (+1270/−0) · .gitignore(1), README.md(1), frontend(1)
  - - ls_openapi.py: LS Open API 토큰 발급(3채널 실검증 OK)+TR 요청, 조회 전용
