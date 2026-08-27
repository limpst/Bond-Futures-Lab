# 🧪 Bond Futures Lab — 한·미 국채선물 RV(상대가치) 랩

> **성격** 개인 계좌 · 소액 · 학습 목적 — **회사 PoC 와 분리된 별도 프로젝트** (2026-08-23 개설)
> **범위** 시세 수집 · 계량 검정 · 백테스트 · 시그널까지. **자동 주문 실행 코드는 이 저장소에 만들지 않는다.** 주문은 사람이 HTS/MTS 에서 직접 한다.
> **저장소** `github.com/limpst/Bond-Futures-Lab` (**private**) · 브랜치 `master`
> **데이터** LS증권 Open API (국내·해외 선물옵션) + yfinance (CME 복구·FX) · 자격증명은 `.env.ls` (git 제외)

---

## 📖 읽는 법 (3줄)

1. 이 저장소는 **"한국 국채선물과 미국 국채선물의 가격 차이가 정말 제자리로 돌아오는가"** 를 통계로 검사하는 실험실이에요.
2. 아래를 **동기 → 알고리즘 → 방법론 → 문헌 → 구조 → 현재 상태** 순서로 읽으면 전체가 잡혀요. **굵은 글씨만** 읽어도 충분해요.
3. "지금 어떤 상태인가"는 [§8 현재 상태](#8-현재-상태-실측)와 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)를 보면 돼요. 화면은 `python tools/lab_api.py` → http://127.0.0.1:8010

## 🔤 Notation & Abbreviations

| 기호 | 이름 | 쉬운 뜻 |
|---|---|---|
| KTB3 / KTB10 / KTB30 | 국고채 3·10·30년 선물 (KRX) | 한국 금리를 사고파는 상장 선물 |
| ZT / ZF / ZN / ZB / TN | CME 미 국채 선물 | 기초자산 = 미 국채 2/5/10/30년(+Ultra 10). **ZN(10Y)** 이 유동성 최대 |
| RV | relative value (상대가치) | **전략** 분류 — 두 자산의 상대 가격이 정상 범위를 벗어나면 되돌아오는 데 베팅 |
| delta-one | 델타원 | **상품** 분류 — 옵션성 없이 기초자산과 1:1로 움직이는 상품(선물·스왑·ETF). RV와 동의어가 아니다: **델타원은 재료, RV는 요리법** |
| s (spread) | 스프레드 | 이 전략이 사고파는 대상. 동일 통화 pair 는 단순 가격차, 이종 통화 pair 는 로그 가격비 |
| β (beta) | 헤지비율 | "ZN 이 1% 움직일 때 KTB10 이 몇 % 움직이나" — 회귀로 추정 |
| z-score | 표준화 점수 | s 가 평소(rolling 창) 대비 몇 σ 벗어났나. **±2σ 가 기본 진입 threshold** |
| IQR | interquartile range | s 의 25~75% 구간 폭 — z 와 **둘 다** 확인하는 이중 게이트 (팻테일에서 z 단독 과신 방지) |
| ECM | error correction model | Δs = α + **γ·s(t−1)** + ε 에서 γ<0 유의면 "벌어지면 돌아온다"가 통계로 확인 |
| HAC | Newey-West 보정 | 자기상관 때문에 부풀려진 t 값을 깎는 보정. **게이트는 HAC t 만 쓴다** |
| ADF | Augmented Dickey-Fuller | 정상성(제자리로 돌아오는 성질) 검정. p ≤ 0.05 라야 인정 |
| OU | Ornstein-Uhlenbeck | "고무줄 달린 공" 모형 — 평균에서 벗어나면 κ 의 세기로 끌려 돌아온다 |
| half-life | 반감기 | ln2/κ — 벗어난 거리의 **절반**이 돌아오는 데 걸리는 시간 |
| 세션 | session | 주간 09:00~15:45 · 야간 18:00~05:00 (KST). **세션 경계를 넘는 차분은 버린다** |
| 1 pt | 1포인트 | KTB 선물 1계약 기준 100만원 |

---

## 1. 🎯 Motivation — 왜 이 랩을 만들었나

**배운 것을 내 돈으로 검증하기.** 회사 PoC(태국 채권 QUBO·ETF 포트폴리오)에서 다루는 최적화·백테스트 규율을, **내가 직접 체결 가능한 가장 단순한 형태**로 옮겨 확인한다. 그래서 유니버스를 채권 **선물 8종**으로 좁혔다 — 옵션성이 없고(델타원), 레버리지와 비용 구조가 명확하며, 두 다리만 있으면 전략이 성립한다.

**세 가지 구체적 동기**

1. **금리는 국경을 넘어 같이 움직인다** — 자본 이동이 자유로운 세상에서 각국 중앙은행은 통화 방어를 위해서라도 글로벌 금리 사이클을 따라간다. 그렇다면 한·미 10년 금리의 **격차는 무한정 벌어지지 못한다**. 이 명제가 1분봉 수준에서도 통계로 잡히는지 보고 싶었다.
2. **"성과가 났다"보다 "증거가 있나"를 먼저 묻는 습관** — 회사 PoC에서 격자 최적 1개만 보고하는 위험(격자 재최적화로 궤적 전체가 바뀜)을 겪었다. 여기서는 **진입 전에 통계 게이트를 통과해야만** 거래하도록 설계했다.
3. **데이터 파이프라인을 끝에서 끝까지 소유하기** — 남이 준 정제 데이터가 아니라 API 토큰 발급부터 결측 복구까지 직접 만든다. 데이터가 어디서 끊기고 왜 오염되는지는 만들어 봐야 안다(실제로 세 번 크게 배웠다 — §9).

**하지 않는 것**: 자동 주문 실행, 레버리지 확대, 성과 대외 인용.

## 2. 🧠 알고리즘 — 고무줄 달린 공

두 나라(또는 두 만기)의 금리는 고무줄로 묶인 두 공 같아서, 많이 벌어지면 되돌아오는 **경향**이 있다. 알고리즘은 세 단계다.

```
STEP 1  차이를 잰다      매 1분, 두 선물의 spread s_t 를 기록
STEP 2  얼마나 벌어졌나  최근 W=120분 기준 z_t = (s_t − μ̂_W) / σ̂_W
STEP 3  자물쇠 3개 확인  ① |z_t| ≥ 2σ  ② IQR 울타리 밖  ③ ECM 증거(γ<0 ∧ t_HAC ≤ −1.64)
                        → 셋 다 열려야 진입 · 되돌아오면(|z| ≤ 0.25) 청산
```

**자물쇠 ③이 이 랩의 핵심**이다. 통계가 "되돌아온다는 증거가 부족하다"고 말하면 **그날은 거래하지 않는다.** 신호가 예뻐 보여도 마찬가지다.

**수식 (Formal)**

| 항목 | 정의 |
|---|---|
| 동일 통화 pair | `s_t = P¹_t − P²_t` [pt] — 예: KTB3−KTB10 |
| 이종 통화 pair | `s_t = 100·(ln P¹_t − β·ln P²_t)` — 예: KTB10−ZN. 단위를 없애기 위해 로그비를 쓴다 |
| z-score | `z_t = (s_t − μ̂_W)/σ̂_W`, W = 120봉, **세션 내부에서만** |
| OU / half-life | `ds = κ(μ − s)dt + σdW` → AR(1) `s_t = a + b·s_{t−1} + ε`, `κ = −ln b`, `half-life = ln2/κ` |
| ECM | `Δs_t = α + γ·s_{t−1} + ε_t`, 게이트는 `γ < 0 ∧ t_HAC(γ) ≤ −1.64` |
| 진입/청산 | 진입 `sign = −sign(z)` · 청산 `\|z\| ≤ 0.25` · 손절 `진입 후 반대로 +1σ 추가 이탈` |
| 헤지비율 | `β` = ln P_ZN 에 대한 ln P_KTB10 의 rolling OLS 기울기 (창 240분) |

## 3. 📐 Methodology — 데이터에서 판정까지

### 3.1 수집 (collect)

| 대상 | 경로 | 주기 |
|---|---|---|
| KTB 주간 | LS WebSocket `FC9` (체결) | 09:00~15:45 실시간 |
| KTB 야간 | LS REST `t8461` 폴링 | 18:00~05:00, 5분 간격 |
| CME | LS WebSocket `OVC` (해외 체결) | 07:05~06:55 (세션 전 구간) |
| USDKRW | yfinance `KRW=X` (fallback: Alpha Vantage 스냅샷) | 마감 후 1회 |

### 3.2 오염을 막는 세 가지 규율

1. **세션 경계** — 12시간 공백을 사이에 둔 두 봉을 "연속"으로 보면 그 이음매의 가격 변화가 표본 최대값이 되어 AR(1)·ADF·ECM 을 직접 오염시킨다(2026-08-24 실측). 60분 넘는 공백은 **다른 세션**으로 끊고, 차분·lag 쌍은 세션 안에서만 만든다.
2. **무거래 ≠ 결측** — 선물은 그 분에 체결이 없으면 봉이 아예 생기지 않는다. 수집기 가동 로그(`collect_log`)와 대조해 **무거래(정상)** 와 **수집 중단(사고)** 를 가른다.
3. **출처 보존** — 외부 소스로 채운 봉은 `symbol` 열에 `yfinance:ZN=F` 처럼 출처를 남긴다. LS 가 넣은 봉은 **절대 덮지 않는다**.

### 3.3 결측 복구 (backfill) — 어디까지 되돌릴 수 있나

| 대상 | 복구 범위 | 근거 |
|---|---|---|
| KTB (국내) | **최근 900분(약 15시간)** | `t8461` 에 날짜 파라미터가 없고 `cnt` 상한이 900. t8415·t8414·t2209·t8416~19 는 게이트웨이가 거부 |
| CME | **최근 7일** | LS 는 못 주지만 yfinance 1분봉으로 복구. LS ZN 과 겹치는 433분에서 **가격차 중앙값 0.000000 · 최대 0.015625(반 틱)** 로 동일함을 확인 |
| FX | 최근 7일 | yfinance `KRW=X` |

**가드**: 겹침 구간 가격차가 0.05 를 넘으면(월물 불일치 의심) 자동으로 채우지 않는다 — 실제로 ZB(0.44)·TN(0.30)이 차단됐다.

### 3.4 검정 (validate)

- **ADF** — spread 수준의 정상성. p ≤ 0.05 라야 "되돌아온다"를 인정.
- **ECM + Newey-West** — 1분봉 잔차는 자기상관이 강해 OLS t 가 부풀려진다(실측: t_OLS −3.28 → t_HAC −1.38). **게이트는 HAC t 만** 쓴다.
- **β 이중 문턱** — 표본 수(겹침 ≥ 1,000봉) **그리고** 안정성(최근 rolling β 의 sd ≤ 0.10). 표본만 보고 켜면 β 가 0.9~1.9 를 오가는 상태에서 한 값을 집어 쓰게 된다 — 그건 헤지가 아니라 도박이다.
- **판정 일지** — 같은 질문에 대한 답이 표본이 쌓일 때마다 어떻게 바뀌는지 시계열로 기록(`reports/econ_daily.jsonl`). **판정이 흔들린다는 사실 자체가 증거**다.

### 3.5 백테스트 규율

- **look-ahead 금지** — 시그널은 t 봉 종가로 만들고 체결은 t+1 봉 시가.
- **비용** — 왕복 수수료 + Roll(1984) 유효 스프레드 + implementation shortfall 을 전부 뺀다. 비용 전 숫자는 의미 없음.
- **오버나이트 미보유** — 세션 끝에서 강제 청산.
- **전 조합 공개** — 격자 최적 1개만 보고하지 않는다(회사 PoC 격자 감사의 교훈).

## 4. 📚 Literature Review — 무엇을 어디서 가져왔나

| 문헌 | 이 랩에서 쓰는 것 | 코드 |
|---|---|---|
| **Engle & Granger (1987)**, *Co-integration and Error Correction*, Econometrica | ECM 형태 `Δs = α + γ·s(t−1)`, γ<0 = 되돌림의 통계적 정의 | `tools/econ_pair.py` |
| **Dickey & Fuller (1979)** / ADF | spread 정상성 검정 — 진입 자격의 1차 관문 | `tools/econometrics_report.py` |
| **Newey & West (1987)**, Econometrica | 자기상관·이분산 보정 표준오차 → HAC t. 1분봉에서 필수 | `ecm_gamma_hac()` in `tools/deltaone_backtest.py` |
| **Ornstein & Uhlenbeck (1930)** / **Elliott, van der Hoek & Malcolm (2005)**, *Pairs trading*, Quantitative Finance | OU 평균회귀 모형과 half-life — "얼마나 빨리 돌아오나"의 정량화 | `tools/beta_fx.py`, `tools/econ_pair.py` |
| **Gatev, Goetzmann & Rouwenhorst (2006)**, *Pairs Trading*, Review of Financial Studies | distance/z-score 기반 pair 진입·청산 규칙의 원형 | `tools/deltaone_backtest.py` |
| **Avellaneda & Lee (2010)**, *Statistical arbitrage in the US equities market*, Quantitative Finance | 잔차 기반 s-score 와 평균회귀 속도 필터 | 진입 게이트 설계 |
| **Vidyamurthy (2004)**, *Pairs Trading: Quantitative Methods and Analysis* | 공적분 기반 헤지비율 β 추정과 밴드 설정 | `beta_fx.py` rolling β |
| **Roll (1984)**, *A Simple Implicit Measure of the Effective Bid-Ask Spread*, Journal of Finance | 체결가만으로 유효 스프레드 추정 → 비용 모형의 기본항 | `tools/microstructure.py`, `strategy_lab.py` |
| **Amihud (2002)**, *Illiquidity and stock returns*, Journal of Financial Markets | 비유동성 지표 — 두 다리의 유동성 격차 측정(KTB10 이 KTB3 보다 5배 열위) | `tools/microstructure.py` |
| **Engle & Russell (1998)**, *Autoregressive Conditional Duration*, Econometrica | 이벤트 간격의 군집(기회는 소나기처럼 몰려온다) 진단 | `econometrics_report.py` (ACD 통계) |
| **Cont, Kukanov & Stoikov (2014)**, *The Price Impact of Order Book Events*, Journal of Financial Econometrics | OFI(주문흐름 불균형) — 현재는 체결 기반 proxy, 호가 수집 후 정식 계산 예정 | `tools/microstructure.py` |
| **Hasbrouck (1995)**, *One Security, Many Markets*, Journal of Finance | 가격발견 기여도 — 한·미 중 어느 쪽이 먼저 움직이나(lead-lag) | 계획 |
| **Almgren & Chriss (2001)**, *Optimal execution of portfolio transactions*, Journal of Risk | implementation shortfall 회계 — 결정가 대비 체결가 차이를 비용에 포함 | `tools/strategy_lab.py` |
| **Bailey & López de Prado (2014)**, *The Deflated Sharpe Ratio*, Journal of Portfolio Management | 다중검정 보정 — 여러 파라미터를 시도한 뒤의 샤프는 그대로 믿지 않는다 | 회사 PoC 와 공통 규율 |
| **Kupiec (1995)**, *Techniques for Verifying the Accuracy of Risk Measurement Models*, Journal of Derivatives | VaR 위반 횟수 검정 — 리스크 모형이 현실과 맞는지 | 계획 |

> 인용은 **개념 출처 표시**이지 구현이 원문과 동일하다는 주장이 아니다. 각 항목의 실제 구현 범위는 해당 코드의 docstring 에 적어 두었다.

## 5. 🗂 Structure

```
bond-futures-lab/
├─ README.md                      ← 이 문서 (work hub)
├─ docs/
│   ├─ CHANGELOG.md               변경 이력 (git 에서 자동 생성)
│   └─ DASHBOARD_MANUAL.md        화면 사용 설명
├─ reports/                       분석 산출물
│   ├─ TECH_REPORT_deltaone_sim_2026-08-24.md   기술 보고서(ADF·OU·microstructure)
│   ├─ TECH_REPORT_deltaone_v0.4_2026-08-25.md  개정판
│   ├─ econ_daily.jsonl           일일 계량 지표 (판정 일지의 원본)
│   └─ pair_readiness.json        본 pair 준비도(겹침·β·FX 게이트)
├─ tools/
│   ├─ ls_openapi.py              토큰 발급·캐시 + TR 요청 (조회 전용)
│   ├─ collect_minbars.py         KTB REST 1분봉 (야간 폴링·backfill)
│   ├─ collect_kr_ws.py           KTB 주간 실시간 (체결·호가)
│   ├─ collect_cme_ws.py          CME 실시간 (OVC 틱 → 1분봉)
│   ├─ collect_fx.py              USDKRW (yfinance · AV fallback)
│   ├─ backfill_yf.py             CME 결측 복구 (빠진 분만 · 출처 표시 · 가드)
│   ├─ data_health.py             결측/무거래 분류 + 자동 backfill + 건강 리포트
│   ├─ verify_feed.py             수집 검증 probe (신선도·증가·타당성)
│   ├─ watch_collect.py           수집 정지 감시
│   ├─ econ_pair.py               세션 인식 ADF·AR(1)·ECM(HAC)
│   ├─ econometrics_report.py     기술 보고서용 종합 계량 + 일일 기록
│   ├─ beta_fx.py                 rolling β · 준비도 게이트 · FX 상태
│   ├─ microstructure.py          Roll spread · Amihud · OFI proxy
│   ├─ deltaone_backtest.py       z-score spread 백테스트 + sweep
│   ├─ strategy_lab.py            비용·IS 반영 전략 비교
│   ├─ signal_monitor.py          실시간 시그널 판정 (3중 게이트)
│   ├─ execution.py               OMS (dry 기본 · 실주문 TR 미구현)
│   ├─ lab_api.py                 라이브/관리자 API (127.0.0.1:8010)
│   └─ build_lab_dashboard.py     대시보드 생성기 (KR/EN)
├─ frontend/sim_dashboard.html    대시보드 (탭: KTB pair · KTB10−ZN · ⚙️ 운영·관리자)
└─ data/minbars.db                SQLite — instrument · minbar · collect_log · data_gap
```

## 6. 🚀 사용 순서

```bash
python tools/ls_openapi.py                 # 토큰 3채널 확인
python tools/collect_minbars.py --discover # 월물 코드 탐색
python tools/collect_minbars.py --live --count 900   # KTB 수집(+15h backfill)
python tools/collect_fx.py --days 7        # USDKRW
python tools/backfill_yf.py --syms ZN,ZF,ZT # CME 결측 복구
python tools/data_health.py                # 건강 점검 + 자동 backfill
python tools/verify_feed.py                # 수집이 진짜 도는지 검증
python tools/econ_pair.py --all            # 계량 검정
python tools/beta_fx.py                    # 준비도 · rolling β
python tools/build_lab_dashboard.py        # 화면 생성
python tools/lab_api.py                    # http://127.0.0.1:8010
```

**자동화**: 작업 스케줄러 4개 — `KTB day ticks`(08:55~) · `KTB night bars`(18:00~05:00, 5분) · `CME bars`(07:05~06:55) · `Collect watchdog`(15분). 전부 **놓친 실행 복구 켜짐 · 배터리에서도 실행**.

## 7. 🔁 Tracking — 변경사항을 어떻게 남기나

| 무엇 | 어디에 | 갱신 |
|---|---|---|
| 코드·설계 변경 | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | `python tools/gen_changelog.py` (git 이력에서 자동 생성) |
| 일일 계량 지표 | `reports/econ_daily.jsonl` | 마감 후 스케줄러가 1행 추가 |
| 판정 변화 | 대시보드 **판정 일지** 표 | 빌드할 때마다 econ_daily 에서 재구성 |
| 데이터 결측 | `data_gap` 테이블 + 관리자 탭 | 5분마다 점검, 복구되면 자동 닫힘 |
| 수집 상태 | 관리자 탭 ⚙️ / `tools/verify_feed.py` | 실시간 |

**원칙**: 판정이 바뀌면 **이전 판정을 지우지 않는다.** 틀린 것으로 드러난 값도 일지에 남겨, 무엇이 언제 왜 뒤집혔는지 추적할 수 있게 한다.

## 8. 현재 상태 (실측)

> 2026-08-26 기준 · 수치는 [`reports/pair_readiness.json`](reports/pair_readiness.json) · `econ_daily.jsonl` 원본

**표본**: 총 34,205봉 — USDKRW 9,198 · ZN 6,821 · ZF 6,449 · ZT 6,297 · KTB10 2,126 · KTB3 2,087 · ZB 610 · TN 597

| pair | 겹침 | ADF p | half-life | ECM t_HAC | 판정 |
|---|---|---|---|---|---|
| **KTB10−ZN** (본 pair) | 1,771봉 | **0.0132** | **17.8분** | **−2.26** | 🟢 정상성 채택 + ECM 유의 |
| KTB3−KTB10 | 1,859봉 | 0.847 | 112~633분 | −1.05 | 🟠 증거 사라짐 |

**KTB3−KTB10 의 반전**: 8/24 에는 half-life 23분·t_HAC −1.95 로 게이트를 통과했지만, 표본이 쌓이자 8/25 −1.64 → 8/26 −0.97 로 **증거가 무너졌다.** 짧은 표본의 추정치를 믿으면 안 된다는 것을 이 프로젝트 스스로가 보여준 사례다.

**β 는 아직 켜지 않았다**: 겹침은 문턱(1,000)을 넘겼지만 rolling β 의 최근 sd 가 **0.794** (전체표본 β = 1.032). 안정성 조건을 못 넘어 **β = 1.0 고정** 상태로 둔다.

**FX**: 데이터는 확보(USDKRW 9,198봉, pair 와 3중 겹침 1,743분)했으나 **손익 환산식에는 미적용**. 신호는 FX 없이 성립하지만 손익은 FX 없이 틀린다.

## 9. 🧾 정직성 노트 — 틀렸던 것들

이 랩이 배운 것의 절반은 **내가 틀렸던 기록**이다.

| 언제 | 무엇을 틀렸나 | 어떻게 드러났나 |
|---|---|---|
| 8/24 | 야간 세션 날짜 합성 버그로 시계열이 12시간 거꾸로 점프 | 그 이음매가 표본 최대 가격변화 → 계량 전체 오염. 세션 인식으로 수정, 오염 구간 격리 |
| 8/24 | ECM t = −1.82 "유의" 라고 보고 | Newey-West 보정하니 −1.38 — 유의성 소멸. 게이트를 HAC 로 교체 |
| 8/26 | "CME 는 backfill 경로가 없다, 영구 결측" (2회 단언) | yfinance 로 복구 가능했다. LS 안에서만 찾고 밖을 안 봤다 |
| 8/26 | 표본만 보고 β 를 켜는 게이트 | β sd 0.955 인 상태에서 β=1.886 을 적용해버림 → 안정성 조건 추가 |
| 8/26 | 수집기 사망 원인을 외부 요인으로 추정 | 실제로는 SQLite 락 — busy_timeout 부재. WAL + 60초 대기로 해결 |

**대외 인용 금지**: 비용·슬리피지·롤오버가 반영되지 않은 어떤 수치도 성과 주장에 쓰지 않는다. 소액·학습 목적이며 투자 권유가 아니다.
