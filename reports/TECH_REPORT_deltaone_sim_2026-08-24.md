# 델타원 Spread 시뮬레이션 기술 보고서 — KTB 1분봉 실측 · ADF·OU·Microstructure (2026-08-24)

> (Notion 원문에서 변환, 2026-08-24) 원문: https://www.notion.so/3c66585be6ea8087a1a6d571d4c01e51
> 수치 원본: `reports/econ_20260824.json` · 재현: `python tools/econometrics_report.py`

핵심 결론 (요약):
- ADF: spread 수준 p=0.318 → 하루(711봉) 표본으로는 정상성 확정 실패. Δspread p<0.001.
- OU 적합: κ=0.0304/분, half-life 22.8분, μ=−1.972pt, σ=0.0136 — 가설적 추정치.
- ECM: γ=−0.0299, t_OLS=−3.28 이지만 HAC(NW, lag10) t=−1.38 → 유의성 소멸.
  → 진입 게이트의 ECM 검사는 HAC t 기준으로 교체 권고 (8/24 보고 t=−1.82는 보정 전 수치).
- Microstructure(1분봉 proxy): KTB3 Roll spread 0.0033pt · Amihud 격차 5배(KTB10 열위)
  · OFI proxy 쏠림 없음 · KTB3 주문흐름 반전 성향(AC1 −0.16). 정식 OFI/OBI는 호가 TR 수집 필요.
- Duration: |z|≥1σ 이벤트 169개가 6개 군집(CV 2.02 과분산) — ACD 동기 확인, 적합은 표본 부족.
- 시뮬레이션 sweep(1.0/1.5/2.0σ): 각 완결 1건 +0.06pt(+6만원/1계약, 미청산 MTM 포함) — 기능 검증 의미만.
- 다음 단계: ①5~10 영업일 축적 후 재검정 ②ECM 게이트 HAC화 ③호가 수집 ④일일 자동화 ⑤CME 반영 후 KTB10–ZN.

상세 표·해설은 Notion 원문 참조 (섹션: 읽는 법 · Notation · 유니버스 · 데이터 · ADF · OU · ECM · Microstructure · ACD · 시뮬레이션 · 다음 단계).
