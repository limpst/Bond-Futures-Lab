# -*- coding: utf-8 -*-
"""Bond Futures Lab — 시뮬레이션 대시보드 생성기.

reports/econ_*.json · reports/chart_series.json · reports/econ_daily.jsonl ·
data/signals.json 을 읽어 self-contained HTML(frontend/sim_dashboard.html)을
만든다. 아주 쉽게(초등 reader) + Formal Definition 접힘 병행.
재실행: python tools/build_lab_dashboard.py
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
econ = json.loads((ROOT / "reports" / "econ_20260824.json").read_text(encoding="utf-8"))
chart = json.loads((ROOT / "reports" / "chart_series.json").read_text(encoding="utf-8"))
daily_rows = []
dj = ROOT / "reports" / "econ_daily.jsonl"
if dj.exists():
    daily_rows = [json.loads(l) for l in dj.read_text(encoding="utf-8").splitlines() if l.strip()]

ou, ecm, adf = econ["ou"], econ["ecm"], econ["adf_level"]
sweep = econ["sweep"]
gate_pass = ecm["gamma"] < 0 and ecm["t_hac10"] <= -1.64

# 판정 일지: 스냅샷 3개 (8/24 보고 → 8/24 마감 기록 → 지금)
verdicts = [
    {"label": "8/24 보고 (711봉·하루)", "adf_p": 0.318, "hl": "23분", "t": -1.38,
     "verdict": "증거 부족"},
]
for r in daily_rows:
    verdicts.append({"label": f"{r['date']} 마감 기록 ({r['n_aligned']:,}봉)",
                     "adf_p": r["adf_p"], "hl": f"{r['half_life_min']:.0f}분",
                     "t": r["t_hac10"],
                     "verdict": "게이트 통과" if r["t_hac10"] <= -1.64 else "증거 부족"})
hl_now = ou["half_life_bars"]
hl_txt = f"{hl_now/60:.1f}시간" if hl_now >= 90 else f"{hl_now:.0f}분"
verdicts.append({"label": f"지금 ({econ['n_aligned']:,}봉·진행 중)", "adf_p": adf["p"],
                 "hl": hl_txt, "t": ecm["t_hac10"],
                 "verdict": "게이트 통과" if gate_pass else "증거 부족"})

vrows = "".join(
    f"<tr><td class='tx'>{v['label']}</td><td>{v['adf_p']:.3f}</td><td>{v['hl']}</td>"
    f"<td>{v['t']:.2f}</td><td class='tx'>{'🟢 ' if v['verdict']=='게이트 통과' else '🟠 '}{v['verdict']}</td></tr>"
    for v in verdicts)

srows = "".join(
    f"<tr><td>{s['th']:.1f}σ</td><td>{s['trades']}</td><td>{'있음' if s['open_pos'] else '없음'}</td>"
    f"<td>{s['pnl_pts']:+.2f}</td><td>{s['pnl_krw_1lot']:+,}원</td></tr>" for s in sweep)

HTML = """<title>Bond Futures Lab</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>
:root{--bg:#0B0E14;--panel:#121722;--panel2:#171E2C;--line:#232B3A;--text:#E8ECF4;
--muted:#8B95A7;--faint:#5A6478;--amber:#F5B84B;--teal:#3FD0C9;--ok:#46C077;--crit:#EF6A5A}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);
 font:15px/1.75 "IBM Plex Sans KR",-apple-system,"Malgun Gothic",sans-serif;padding:0 18px 80px}
.wrap{max-width:1040px;margin:0 auto}
.mono{font-family:"IBM Plex Mono",Consolas,monospace}
header{padding:44px 0 10px;border-bottom:1px solid var(--line)}
header h1{font-size:30px;font-weight:700;letter-spacing:-.01em;text-wrap:balance}
header h1 .tag{color:var(--amber)}
.sub{color:var(--muted);font-size:13px}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
 border-radius:999px;padding:4px 12px;font-size:11.5px;color:var(--muted);background:var(--panel)}
.pill i{width:7px;height:7px;border-radius:50%;flex:none}
.p-ok i{background:var(--ok)} .p-warn i{background:var(--amber)} .p-crit i{background:var(--crit)}
.p-ok{color:var(--ok)} .p-warn{color:var(--amber)} .p-crit{color:var(--crit)}
section{margin-top:34px}
h2{font-size:19px;font-weight:700;margin-bottom:4px}
h2 .n{color:var(--faint);font-family:"IBM Plex Mono",monospace;font-size:14px;margin-right:8px}
.lead{color:var(--muted);margin-bottom:14px;max-width:68ch}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.grid{display:grid;gap:12px}
.g4{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.kpi .lbl{font-size:11.5px;color:var(--muted);letter-spacing:.04em}
.kpi .val{font-size:26px;font-weight:700;font-family:"IBM Plex Mono",monospace;margin:2px 0}
.kpi .note{font-size:12px;color:var(--faint)}
.tblwrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px}
td.tx,th.tx{text-align:left} tr:last-child td{border-bottom:none}
.steps{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.step{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.step .no{font-family:"IBM Plex Mono",monospace;color:var(--amber);font-weight:600;font-size:12px}
.step b{display:block;margin:4px 0 6px;font-size:15px}
.step p{font-size:13px;color:var(--muted)}
details{background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-top:12px}
summary{cursor:pointer;padding:14px 20px;font-weight:700;list-style:none}
summary::before{content:"▸ ";color:var(--amber)}
details[open] summary::before{content:"▾ "}
.dbody{padding:0 20px 18px;border-top:1px solid var(--line)}
.f{font-family:"IBM Plex Mono",monospace;font-size:13.5px;background:var(--panel2);
 border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0;overflow-x:auto}
.chartbox{position:relative}
canvas{width:100%;display:block}
.warnbox{border-left:3px solid var(--amber);background:var(--panel2);border-radius:10px;
 padding:12px 16px;font-size:13px;color:var(--muted);margin-top:14px}
a{color:var(--teal)}
:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
@media(prefers-reduced-motion:no-preference){.step{transition:border-color .2s}
.step:hover{border-color:#31405c}}
</style>
<div class="wrap">
<header>
 <h1>Bond Futures Lab <span class="tag">— KTB Spread 실험 노트</span></h1>
 <p class="sub">개인 학습 실험 · 소액 · 자동 주문 없음 · 어떤 수치도 투자 권유 아님 — __STAMP__ 생성</p>
 <div class="badges">
  <span class="pill p-ok"><i></i>KTB 수집 가동 (실시간 WS + 5분 REST)</span>
  <span class="pill p-crit"><i></i>CME 수집 정지 — 8/24 저녁 1시간분(11봉)뿐</span>
  <span class="pill __GATECLS__"><i></i>진입 게이트 __GATETXT__</span>
  <span class="pill p-warn"><i></i>표본 __NBARS__봉 (이틀) — 판정 유보 중</span>
 </div>
</header>

<section>
 <h2><span class="n">0</span>이 화면 읽는 법</h2>
 <p class="lead">① 위 배지 4개가 오늘의 요약이에요. ② 아래로 내려가며 <b>상품 → 알고리즘 → 판정 일지 → 결과</b> 순서로 읽어요. ③ 수식이 궁금하면 맨 아래 "Formal Definition"을 펼치세요. 굵은 글씨만 읽어도 충분해요.</p>
 <div class="card tblwrap"><table>
  <tr><th class="tx">기호</th><th class="tx">이름</th><th class="tx">쉬운 뜻</th></tr>
  <tr><td class="tx mono">spread</td><td class="tx">스프레드</td><td class="tx">두 선물 가격의 차이 (KTB3 − KTB10). 우리가 사고파는 대상</td></tr>
  <tr><td class="tx mono">z</td><td class="tx">z-score</td><td class="tx">spread가 평소(최근 2시간)보다 몇 σ 벗어났나. ±2σ = 진입 후보</td></tr>
  <tr><td class="tx mono">half-life</td><td class="tx">반감기</td><td class="tx">벗어난 거리의 절반이 돌아오는 데 걸리는 시간 (고무줄의 세기)</td></tr>
  <tr><td class="tx mono">ADF p</td><td class="tx">정상성 검정</td><td class="tx">"정말 되돌아오는 성질이 있나?" 시험 점수 — 0.05보다 작아야 합격</td></tr>
  <tr><td class="tx mono">t_HAC</td><td class="tx">ECM 보정 t</td><td class="tx">"벌어지면 돌아온다"의 증거 세기 (자기상관 보정) — −1.64보다 작아야 게이트 통과</td></tr>
  <tr><td class="tx mono">1 pt</td><td class="tx">1포인트</td><td class="tx">KTB 선물 1계약 기준 100만원</td></tr>
 </table></div>
</section>

<section>
 <h2><span class="n">1</span>상품이 뭐예요? — 국채 <b>선물</b></h2>
 <p class="lead">채권은 "나라가 돈을 갚겠다"는 약속 문서예요. <b>국채 선물</b>은 그 약속 문서를 <b>정해진 날짜에 정해진 값으로 사고팔기로 한 표준 계약</b>이고, 거래소에 상장돼 있어 주식처럼 사고팔 수 있어요. 이 실험실은 채권 선물 <b>딱 8개</b>만 봅니다.</p>
 <div class="card tblwrap"><table>
  <tr><th class="tx">시장</th><th class="tx">상품</th><th class="tx">쉬운 뜻</th><th class="tx">수집</th></tr>
  <tr><td class="tx">KRX</td><td class="tx mono">KTB3 · KTB10 · KTB30</td><td class="tx">한국 국고채 3·10·30년 선물</td><td class="tx">🟢 1분봉 수집 중 (__NBARS__봉)</td></tr>
  <tr><td class="tx">CME</td><td class="tx mono">ZT · ZF · ZN · ZB · TN</td><td class="tx">미국 국채 2·5·10·30년(+Ultra 10) 선물</td><td class="tx">🔴 정지 — 8/24 저녁 1시간분뿐</td></tr>
 </table></div>
 <div class="warnbox">본 pair는 <b>KTB10–ZN</b>(한미 10년)이지만 CME 표본이 아직 없어, 지금 분석은 ③순위 pair <b>KTB3–KTB10</b>으로 합니다. CME WebSocket 수집기가 상시화되면 자동으로 본 pair 표본이 쌓입니다.</div>
</section>

<section>
 <h2><span class="n">2</span>알고리즘이 뭐예요? — 고무줄 달린 공</h2>
 <p class="lead">두 나라(또는 두 만기)의 금리는 멀리 못 떨어져요 — 고무줄로 묶인 두 공 같아서, 많이 벌어지면 되돌아오는 경향이 있어요. 알고리즘은 딱 세 단계예요.</p>
 <div class="steps">
  <div class="step"><span class="no">STEP 1</span><b>차이를 잰다</b><p>매 1분, 두 선물 가격의 차이(spread)를 기록해요. 지금: <span class="mono">__SPREADNOW__ pt</span></p></div>
  <div class="step"><span class="no">STEP 2</span><b>많이 벌어졌나 본다</b><p>평소(최근 2시간) 대비 몇 σ 벗어났는지(z-score) 계산. 지금: <span class="mono">z = __ZNOW__</span> — ±2σ부터 진입 후보</p></div>
  <div class="step"><span class="no">STEP 3</span><b>자물쇠 3개를 확인하고 진입</b><p>① |z| ≥ 2σ ② 분포 기준(IQR)으로도 밖 ③ "되돌아온다"는 통계 증거(ECM, 보정 t ≤ −1.64) — <b>셋 다</b> 열려야 진입, 되돌아오면(z≈0) 청산</p></div>
 </div>
 <div class="warnbox">자물쇠 ③이 핵심 안전장치예요. 통계가 "되돌아온다는 증거가 부족하다"고 하면 <b>그날은 거래하지 않아요</b>. 지금이 바로 그 상태예요 (t_HAC = __THAC__).</div>
</section>

<section>
 <h2><span class="n">3</span>판정 일지 — 통계가 하루하루 뒤집히는 중</h2>
 <p class="lead">같은 질문("되돌아오는 성질이 있나?")에 대한 답이 데이터가 쌓일 때마다 흔들려요. <b>이 흔들림이야말로 "아직 돈 걸면 안 된다"는 가장 확실한 증거</b>예요. 5영업일치가 쌓이면 판정이 안정되는지 봅니다.</p>
 <div class="card tblwrap"><table>
  <tr><th class="tx">시점</th><th>ADF p (≤.05 합격)</th><th>half-life</th><th>t_HAC (≤−1.64 통과)</th><th class="tx">판정</th></tr>
  __VROWS__
 </table></div>
</section>

<section>
 <h2><span class="n">4</span>지금 시장 — spread와 z-score</h2>
 <div class="card chartbox"><canvas id="c1" height="210"></canvas></div>
 <div class="card chartbox" style="margin-top:12px"><canvas id="c2" height="150"></canvas></div>
 <p class="sub" style="margin-top:8px">위: spread(pt) — 노란 점선 = 장기 평균 μ. 아래: z-score — 붉은 띠 = ±2σ 진입선. 데이터: 실측 1분봉 __NBARS__봉(주간+야간).</p>
</section>

<section>
 <h2><span class="n">5</span>시뮬레이션 결과 — threshold sweep</h2>
 <p class="lead">"몇 σ에서 들어가는 게 좋았을까"를 과거 데이터로 흉내낸 결과예요. <b>표본이 이틀뿐이라 성적표가 아니라 배관 점검</b> — 파이프라인이 끝까지 돈다는 확인용입니다.</p>
 <div class="card tblwrap"><table>
  <tr><th>진입 기준</th><th>완결 거래</th><th>미청산</th><th>손익 (pt)</th><th>손익 (1계약)</th></tr>
  __SROWS__
 </table></div>
 <div class="warnbox">비용(호가 스프레드 ≈0.003pt×2 + 수수료) 미반영 · 미청산 포지션은 현재가 평가 포함 · 대외 인용 금지.</div>
</section>

<section>
 <h2><span class="n">6</span>Formal Definition — 수식으로 정확하게</h2>
 <details><summary>스프레드 · z-score · 진입/청산 규칙</summary><div class="dbody">
  <div class="f">s_t = P¹_t − P²_t &nbsp;(동일 통화 KTB pair) · 이종 통화는 s_t = ln P¹_t − β·ln P²_t, β = 직전 창 OLS</div>
  <div class="f">z_t = (s_t − μ̂_W) / σ̂_W, &nbsp;W = 120봉(2시간) rolling</div>
  <div class="f">진입: |z_t| ≥ 2 ∧ s_t ∉ [Q1 − 1.5·IQR, Q3 + 1.5·IQR] ∧ (γ̂ < 0 ∧ t_HAC(γ̂) ≤ −1.64) → sign = −sign(z) · 청산: |z_t| ≤ 0.25</div>
 </div></details>
 <details><summary>OU process · half-life</summary><div class="dbody">
  <div class="f">ds_t = κ(μ − s_t)dt + σ dW_t &nbsp;→ 이산화 AR(1): s_t = a + b·s_{t−1} + ε_t</div>
  <div class="f">κ = −ln(b)/Δt = __KAPPA__ /분 · half-life = ln2/κ = __HL__봉 · μ = a/(1−b) = __MU__ pt · b = __B__ (se __BSE__)</div>
 </div></details>
 <details><summary>ECM과 Newey-West 보정 (왜 t가 두 개인가)</summary><div class="dbody">
  <div class="f">Δs_t = α + γ·s_{t−1} + ε_t, &nbsp;γ̂ = __GAMMA__ · t_OLS = __TOLS__ · t_HAC(NW) = __THAC__</div>
  <p class="sub">1분봉 잔차는 자기상관이 강해 OLS 표준오차가 과소평가된다(t 과대). Newey-West(자동 lag)로 보정한 t만 게이트에 사용. 8/24 기술 보고서 §5의 finding.</p>
 </div></details>
 <details><summary>ADF 정상성 검정</summary><div class="dbody">
  <div class="f">Δs_t = α + ρ·s_{t−1} + Σφ_iΔs_{t−i} + ε_t, &nbsp;H₀: ρ=0(단위근) · 지금 stat __ADFSTAT__, p __ADFP__ (lag AIC)</div>
  <p class="sub">p ≤ 0.05일 때만 "되돌아오는 성질"을 통계적으로 인정. 재현: <span class="mono">python tools/econometrics_report.py</span> → <a href="https://www.notion.so/3c66585be6ea8087a1a6d571d4c01e51">기술 보고서</a></p>
 </div></details>
</section>
</div>
<script>
const CH = __CHART__;
function draw(id, vals, opts){
 const cv=document.getElementById(id), dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth, H=parseInt(cv.getAttribute("height"),10);
 cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+"px";
 const x=cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0);
 const padL=52,padR=14,padT=12,padB=22;
 let lo=Math.min(...vals), hi=Math.max(...vals);
 if(opts.band){ lo=Math.min(lo,-opts.band*1.15); hi=Math.max(hi,opts.band*1.15); }
 const pad=(hi-lo)*.08||.5; lo-=pad; hi+=pad;
 const N=vals.length, X=i=>padL+(W-padL-padR)*i/(N-1), Y=v=>padT+(H-padT-padB)*(1-(v-lo)/(hi-lo));
 x.font="10.5px 'IBM Plex Mono',monospace"; x.fillStyle="#5A6478"; x.strokeStyle="#232B3A";
 for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4;
  x.beginPath(); x.moveTo(padL,Y(v)); x.lineTo(W-padR,Y(v)); x.stroke();
  x.textAlign="right"; x.fillText(v.toFixed(opts.dec),padL-7,Y(v)+3.5); }
 x.textAlign="center";
 for(let k=0;k<5;k++){ const i=Math.round(k*(N-1)/4); x.fillText(CH.t[i],X(i),H-6); }
 if(opts.band){ x.fillStyle="rgba(239,106,90,.10)";
  x.fillRect(padL,Y(opts.band),W-padL-padR,Y(-opts.band)-Y(opts.band)>0?0:0);
  x.fillRect(padL,padT,W-padL-padR,Y(opts.band)-padT);
  x.fillRect(padL,Y(-opts.band),W-padL-padR,H-padB-Y(-opts.band));
  x.strokeStyle="rgba(239,106,90,.5)"; x.setLineDash([4,4]);
  [opts.band,-opts.band].forEach(b=>{ x.beginPath(); x.moveTo(padL,Y(b)); x.lineTo(W-padR,Y(b)); x.stroke(); });
  x.setLineDash([]); }
 if(opts.mu!==undefined){ x.strokeStyle="rgba(245,184,75,.65)"; x.setLineDash([5,4]);
  x.beginPath(); x.moveTo(padL,Y(opts.mu)); x.lineTo(W-padR,Y(opts.mu)); x.stroke(); x.setLineDash([]); }
 const g=x.createLinearGradient(0,padT,0,H-padB);
 g.addColorStop(0,opts.fill); g.addColorStop(1,"rgba(0,0,0,0)");
 x.beginPath(); x.moveTo(X(0),Y(vals[0]));
 for(let i=1;i<N;i++) x.lineTo(X(i),Y(vals[i]));
 x.lineTo(X(N-1),H-padB); x.lineTo(X(0),H-padB); x.closePath(); x.fillStyle=g; x.fill();
 x.strokeStyle=opts.color; x.lineWidth=1.8; x.beginPath(); x.moveTo(X(0),Y(vals[0]));
 for(let i=1;i<N;i++) x.lineTo(X(i),Y(vals[i])); x.stroke();
 x.fillStyle=opts.color; x.beginPath(); x.arc(X(N-1),Y(vals[N-1]),3.4,0,7); x.fill();
}
function render(){
 draw("c1", CH.s, {color:"#3FD0C9", fill:"rgba(63,208,201,.16)", dec:2, mu:__MU__});
 draw("c2", CH.z, {color:"#F5B84B", fill:"rgba(245,184,75,.14)", dec:1, band:2});
}
render(); addEventListener("resize", render);
</script>
"""

import datetime
rep = {
    "__STAMP__": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    "__NBARS__": f"{econ['n_aligned']:,}",
    "__GATECLS__": "p-ok" if gate_pass else "p-warn",
    "__GATETXT__": "통과 (거래 자격 있음)" if gate_pass else "미통과 — 오늘은 거래 안 함",
    "__SPREADNOW__": f"{econ['spread_now']:+.2f}",
    "__ZNOW__": f"{econ['z_now']:+.2f}",
    "__THAC__": f"{ecm['t_hac10']:.2f}",
    "__VROWS__": vrows, "__SROWS__": srows,
    "__KAPPA__": f"{ou['kappa_per_bar']:.4f}", "__HL__": f"{ou['half_life_bars']:.0f}",
    "__MU__": f"{ou['mu']:.3f}", "__B__": f"{ou['b_ar1']:.4f}", "__BSE__": f"{ou['b_se']:.4f}",
    "__GAMMA__": f"{ecm['gamma']:.4f}", "__TOLS__": f"{ecm['t_ols']:.2f}",
    "__ADFSTAT__": f"{adf['stat']:.2f}", "__ADFP__": f"{adf['p']:.3f}",
    "__CHART__": json.dumps(chart, ensure_ascii=False),
}
html = HTML
for k, v in rep.items():
    html = html.replace(k, v)
out = ROOT / "frontend" / "sim_dashboard.html"
out.write_text(html, encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB)")
