# 🧪 Bond Futures Lab — 채권 선물 델타원 z-score 랩

> **성격** 개인 계좌 · 소액 · 학습 목적(채권 유니버스 risk factor) — **회사 PoC 와 분리된 별도 프로젝트** (2026-08-23 개설)
> **범위** 시세 조회·수집·백테스트·시그널까지만. **자동 주문 실행 코드는 이 저장소에 만들지 않는다.** 주문은 사람이 HTS/MTS 에서 직접 한다.
> **데이터 채널** LS증권 Open API (국내 선물옵션 · 해외 선물옵션 · 종합매매) — 자격증명은 `.env.ls` (git 제외)

---

## 📖 읽는 법 · 🔤 Notation & Abbreviations

**이 프로젝트는 뭐예요?** 한국(KTB)과 미국(CME) **국채 선물**만 담은 작은 실험실이에요. 두 나라 금리의 '벌어짐(spread)'이 통계적으로 제자리로 돌아오는 성질을 이용해, 소액으로 델타원 전략을 배우고 시험해요.

| 기호 | 이름 | 쉬운 뜻 |
|---|---|---|
| KTB3 / KTB10 | 국고채 3·10년 선물 (KRX) | 한국 금리를 사고파는 상장 선물 |
| ZT / ZF / ZN / ZB | CME 미 국채 선물 | 기초자산 = 미 국채 2/5/10/30년. ZN(10Y)이 유동성 최대 |
| delta-one | 델타원 | 기초자산과 1:1 로 움직이는 상품·전략 (옵션성 없음) |
| spread | 스프레드 | 두 상품 가격(또는 비율)의 차이 — 이 전략이 사고파는 대상 |
| z-score | 표준화 점수 | 스프레드가 평소 대비 몇 σ 벗어났나. **±2σ 가 기본 진입 threshold** |
| IQR | interquartile range | 스프레드의 25~75% 구간 폭 — z-score 와 **둘 다** 확인하는 이중 게이트 (분포가 두꺼운 꼬리일 때 z-score 단독의 과신 방지) |
| ECM | error correction model | Δspread = α + **γ·spread(t-1)** + ε 에서 γ<0(유의)이면 '벌어지면 돌아온다'가 통계로 확인됨 — 진입 자격 게이트 |
| QUBO+SA / QUBO SA InvVol | 하이브리드 선택기 | 어떤 pair/상품을 담을지(선택)는 QUBO+SA, 비중은 고전(InvVol) — 기존 PoC 와 동일 철학 |
| 분·일·주·월봉 | bar frequency | 백테스트를 네 주기 전부에서 돌려 주기 의존성을 본다 |
| 1계약 | contract | 선물 최소 거래 단위. 소액 원칙: **pair 당 1계약**부터 |

## 🎯 투자 thesis (왜 이 spread 인가)

**전 세계 금리는 시차를 두고 같은 방향으로 움직인다** — 자본이 국경을 넘는 세상에서 각국 중앙은행은 통화 가치를 방어하기 위해서라도 궁극적으로 글로벌 금리 사이클을 따라간다. 그래서:

- 한국 금리(KTB)와 미국 금리(UST 선물)의 **스프레드는 무한정 벌어지지 못하고** 되돌아오는 경향이 있다 (시차 = lead-lag).
- 이 되돌림이 실제로 있는지는 믿음이 아니라 **ECM 의 γ<0 유의성**으로 매번 검사한다 — 통계가 무너진 구간에서는 거래하지 않는다.

## 📐 전략 스펙 — main: 델타원 z-score spread

| 요소 | 규칙 |
|---|---|
| 유니버스 | 채권 **선물만**: KTB3·KTB10 (KRX) + ZT·ZF·ZN·ZB (CME) |
| pair (우선순위) | ① **KTB10–ZN** (한미 10Y — thesis 직결) ② KTB3–ZT (한미 단기) ③ KTB3–KTB10 · ZT–ZN · ZN–ZB (커브) |
| 진입 | spread z-score ≥ +2σ → spread 매도 (비싼 다리 short·싼 다리 long) / ≤ −2σ → 반대. threshold 는 sweep 파라미터 |
| 이중 게이트 | ① z-score **그리고** ② spread 가 median ± f×IQR 밖 — 둘 다 켜져야 진입 |
| 자격 게이트 | 롤링 창 ECM γ<0 & t-stat 유의 — 아니면 그 pair 휴면 |
| 청산 | z-score 0 회귀 시 (또는 ±0.5σ 밴드) · 손절 = 진입 후 z 가 반대로 +1σ 추가 이탈 |
| 크기 | pair 당 1계약 (소액 원칙) · 두 다리 듀레이션-노셔널 비율로 헤지 비율 산정 |
| 백테스트 | 분·일·주·월봉 × 창 3·6개월 × threshold {1.5, 2.0, 2.5}σ × IQR fence {0, 1.5} × ECM 게이트 {on, off} — **parameter sweep** 후 multicum 차트로 전 조합 동시 비교 |
| 정직 규율 | 격자 최적 1개만 보고하지 않는다 (ETF 50 격자 감사의 교훈 — 전 조합 분포 공개) · 비용/슬리피지 반영 전 수치는 대외 인용 금지 |

## 🗂 구성

```
bond-futures-lab/
├─ .env.ls                 # LS Open API 자격증명 (git 제외 · 값 인용 금지)
├─ data/minbars.db         # SQLite — instrument/minbar/collect_log (git 제외)
├─ tools/ls_openapi.py     # 토큰 발급·캐시 + TR 요청 (조회 전용)
├─ tools/collect_minbars.py# 1분봉 수집기 — ★기본 dry-run
├─ tools/deltaone_backtest.py # z-score spread 백테스트 + parameter sweep
└─ frontend/index.html     # 랩 메뉴 페이지 (읽는 법·스펙·multicum 차트)
```

## 🚀 사용 순서

```
python tools/collect_minbars.py                 # dry-run (기본)
python tools/collect_minbars.py --issue-token   # 토큰 시험 (3채널 OK — 2026-08-23 확인)
python tools/collect_minbars.py --discover      # 월물 코드 탐색 → instrument.symbol 갱신
python tools/collect_minbars.py --live          # 1분봉 수집 → data/minbars.db
python tools/deltaone_backtest.py               # sweep (데이터 쌓인 뒤)
```

## 🧾 정직성 노트

- 2026-08-23 실측 현황: KTB3·KTB10 1분봉 수집 작동(t8461) · 작업 스케줄러 월~금 5분 간격 등록.
- **CME 분리 검증 확정 (2026-08-23)**: o3103 차트를 HKEX 실재 심볼(CUSU26)로 호출하면 50봉이 정상 수신된다 — **차트 파이프라인·심볼 표기 형식은 정상**. 그러나 현재가 TR(o3105/o3106)에서 CUSU26 은 정상, ZNU26/ZTU26 은 "해당자료가 없습니다"이고 종목 마스터에도 HKEX·LME 상품만 보인다 → **CME 미 국채 선물이 이 계좌/앱의 시세 유니버스에 등록되어 있지 않다. LS 포털에서 해외선물 CME 시세 신청이 선행 조건** (신청 후 마스터에서 실제 상품코드 재확인).
- 백테스트 결과가 나와도 **비용·슬리피지·롤오버 반영 전까지 성과 주장 없음**.
- 소액·학습 목적이며 투자 권유가 아니다.
