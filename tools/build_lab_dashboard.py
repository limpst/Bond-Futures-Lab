# -*- coding: utf-8 -*-
"""Bond Futures Lab — 시뮬레이션 대시보드 생성기 (pair 서브 메뉴 포함).

reports/econ_*.json · reports/econ_daily.jsonl · data/minbars.db 를 읽어
self-contained HTML(frontend/sim_dashboard.html)을 만든다.
아주 쉽게(초등 reader) + Formal Definition 접힘 병행.

★ 2026-08-25: pair 서브 메뉴 추가.
    탭 1  KTB3 − KTB10    한국 3년 vs 10년 (동일 통화 → 단순 가격차 pt)
    탭 2  KTB10 − ZN      한·미 10년 (이종 통화 → 로그 가격비 ×100)

  왜 US pair 는 단순 차가 아닌가: KTB10 은 원화 액면, ZN 은 달러 액면이라
  105.65 − 108.69 같은 뺄셈은 단위가 섞여 뜻이 없다. 여기서는 단위 없는
  s = 100·(ln P_KTB10 − ln P_ZN) 을 쓴다. 정식 헤지비율 β 는 아직 추정하지
  않는다(표본 부족) — 화면에도 그렇게 적는다.

재실행: python tools/build_lab_dashboard.py
"""
import datetime
import io
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import econ_pair as ep                                    # ols · hac_t 재사용

DB = ROOT / "data" / "minbars.db"
GAP_MIN = 60
W = 120                     # rolling 창 (분)
MIN_W = 30                  # 창이 이만큼은 차야 z 를 낸다
GATE = -1.64

econ = json.loads((ROOT / "reports" / "econ_20260824.json").read_text(encoding="utf-8"))
daily_rows = []
dj = ROOT / "reports" / "econ_daily.jsonl"
if dj.exists():
    daily_rows = [json.loads(l) for l in dj.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── pair 계산 ────────────────────────────────────────────────────────────
def load(a, b):
    con = sqlite3.connect(DB)
    rows = list(con.execute(
        "SELECT x.bar_time, x.close, y.close FROM minbar x"
        " JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id=? AND y.instr_id=? ORDER BY x.bar_time", (a, b)))
    T = [datetime.datetime.strptime(r[0], "%Y-%m-%d %H:%M") for r in rows]
    return T, [r[1] for r in rows], [r[2] for r in rows]


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def rolling_z(S, segs):
    """세션 안에서만 창을 잡는다 — 12시간 공백 너머를 '최근'으로 보면 안 된다."""
    Z = [None] * len(S)
    for s, e in segs:
        for i in range(s, e):
            v = S[max(s, i - W + 1):i + 1]
            if len(v) < MIN_W:
                continue
            m = sum(v) / len(v)
            sd = math.sqrt(sum((q - m) ** 2 for q in v) / len(v))
            if sd > 1e-9:
                Z[i] = (S[i] - m) / sd
    return Z


def pair_stats(key, a, b, mode, tab, title, oneline, unit, note):
    T, PA, PB = load(a, b)
    P = dict(key=key, tab=tab, title=title, oneline=oneline, unit=unit, note=note,
             legs="%s − %s" % (a, b), n_bars=len(T))
    if len(T) < MIN_W:
        P.update(status="표본 부족", n_sessions=0, span_from="—", span_to="—")
        return P
    S = ([PA[i] - PB[i] for i in range(len(T))] if mode == "diff"
         else [100 * (math.log(PA[i]) - math.log(PB[i])) for i in range(len(T))])
    segs = sessions(T)
    Z = rolling_z(S, segs)
    mu = sum(S) / len(S)
    sd = math.sqrt(sum((v - mu) ** 2 for v in S) / len(S))
    z_last = next((Z[i] for i in range(len(Z) - 1, -1, -1) if Z[i] is not None), None)

    # 세션 안에서 만든 (t-1, t) 쌍 — 공백 너머 차분은 버린다
    lx, ly, dif = [], [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                lx.append(S[i - 1]); ly.append(S[i]); dif.append(S[i] - S[i - 1])

    P.update(status="ok", n_sessions=len(segs), n_pairs=len(lx),
             span_from=T[0].strftime("%m-%d %H:%M"), span_to=T[-1].strftime("%m-%d %H:%M"),
             spread_now=S[-1], spread_mean=mu, spread_std=sd, z_now=z_last,
             sessions=[{"n": e - s, "from": T[s].strftime("%m-%d %H:%M"),
                        "to": T[e - 1].strftime("%m-%d %H:%M")} for s, e in segs])
    if len(lx) >= MIN_W:
        f = ep.ols(lx, ly)
        g = ep.ols(lx, dif)
        hl = (math.log(2) / -math.log(f["b"])) if 0 < f["b"] < 1 else None
        thac = ep.hac_t(lx, dif, g, 10)
        P.update(ar1_b=f["b"], half_life_min=hl, gamma=g["b"], t_ols=g["t"], t_hac=thac,
                 gate_pass=bool(g["b"] < 0 and thac <= GATE))
    else:
        P.update(gate_pass=False, t_hac=None, half_life_min=None)

    # 차트 — z 가 아직 안 나온 앞구간(창이 덜 참)은 그리지 않는다
    idx = [i for i in range(len(S)) if Z[i] is not None]
    if len(idx) < 2:
        idx = list(range(len(S)))
        zs = [0.0] * len(idx)
    else:
        idx = idx[-900:]
        zs = [round(Z[i], 2) for i in idx]
    P["chart"] = {"t": [T[i].strftime("%m-%d %H:%M") for i in idx],
                  "s": [round(S[i], 4) for i in idx], "z": zs,
                  "mu": round(sum(S[i] for i in idx) / len(idx), 4)}
    P["n_charted"] = len(idx)
    return P


PAIRS = [
    pair_stats(
        "ktb", "KTB3", "KTB10", "diff",
        "🇰🇷 KTB3 − KTB10", "한국 3년 vs 10년",
        "한국 국채선물 3년물과 10년물의 가격 차이(spread)를 1분마다 보고, "
        "그 차이가 평소보다 많이 벌어지면 알려주는 화면이에요.",
        "pt",
        "같은 원화 액면이라 두 가격을 그냥 빼면 된다. 1pt = 100만원."),
    pair_stats(
        "kus", "KTB10", "ZN", "log",
        "🇰🇷🇺🇸 KTB10 − ZN", "한·미 10년 (본 pair)",
        "한국 국채선물 10년물과 미국 국채선물 10년물의 가격 차이(spread)를 1분마다 보고, "
        "그 차이가 평소보다 많이 벌어지면 알려주는 화면이에요.",
        "×100",
        "원화·달러 액면이 달라 그냥 빼면 단위가 섞인다. 그래서 단위 없는 "
        "로그 가격비 s = 100·(ln P_KTB10 − ln P_ZN) 를 쓴다."),
]

# ── 판정 일지 (KTB pair 전용 — 일일 기록이 이 pair 기준) ─────────────────
ou, ecm, adf = econ["ou"], econ["ecm"], econ["adf_level"]
verdicts = [{"label": "8/24 보고 (711봉·하루)", "adf_p": 0.318, "hl": "23분",
             "t": -1.38, "verdict": "증거 부족"}]
for r in daily_rows:
    verdicts.append({"label": f"{r['date']} 마감 기록 ({r['n_aligned']:,}봉)",
                     "adf_p": r["adf_p"], "hl": f"{r['half_life_min']:.0f}분",
                     "t": r["t_hac10"],
                     "verdict": "게이트 통과" if r["t_hac10"] <= GATE else "증거 부족"})
K = PAIRS[0]
verdicts.append({"label": f"지금 ({K['n_bars']:,}봉·세션 {K['n_sessions']}개)",
                 "adf_p": adf["p"],
                 "hl": (f"{K['half_life_min']:.0f}분" if K.get("half_life_min") else "산출 불가"),
                 "t": K.get("t_hac") or 0.0,
                 "verdict": "게이트 통과" if K.get("gate_pass") else "증거 부족"})
vrows = "".join(
    f"<tr><td class='tx'>{v['label']}</td><td>{v['adf_p']:.3f}</td><td>{v['hl']}</td>"
    f"<td>{v['t']:.2f}</td><td class='tx'>{'🟢 ' if v['verdict']=='게이트 통과' else '🟠 '}"
    f"{v['verdict']}</td></tr>" for v in verdicts)
srows = "".join(
    f"<tr><td>{s['th']:.1f}σ</td><td>{s['trades']}</td><td>{'있음' if s['open_pos'] else '없음'}</td>"
    f"<td>{s['pnl_pts']:+.2f}</td><td>{s['pnl_krw_1lot']:+,}원</td></tr>" for s in econ["sweep"])


# ── pane 조립 ────────────────────────────────────────────────────────────
def fmt(v, d=2, plus=True):
    if v is None:
        return "—"
    return ("%+.*f" if plus else "%.*f") % (d, v)


def pane(P, extra_html):
    ok = P["status"] == "ok"
    gate = P.get("gate_pass")
    gcls, gtxt = ("p-ok", "통과 (거래 자격 있음)") if gate else ("p-warn", "미통과 — 오늘은 거래 안 함")
    sess = "".join(
        f"<tr><td class='tx'>{i+1}</td><td>{s['n']:,}</td>"
        f"<td class='tx mono'>{s['from']} ~ {s['to']}</td></tr>"
        for i, s in enumerate(P.get("sessions", [])))
    warn = ("" if P["n_bars"] >= 400 else
            f"<div class='warnbox'>표본 <b>{P['n_bars']:,}봉</b>은 판정에 못 미칩니다 — "
            f"이 탭의 수치는 <b>배관 점검</b>이지 성적표가 아닙니다. "
            f"CME 수집이 상시화되면 하루 약 1,300봉씩 쌓입니다.</div>")
    return f"""
<div class="pane" id="pane-{P['key']}" data-pair="{P['key']}">
 <p class="oneline">{P['oneline']}</p>
 <div class="badges">
  <span class="pill"><i style="background:var(--teal)"></i>{P['legs']} · 표본 {P['n_bars']:,}봉 · 세션 {P['n_sessions']}개</span>
  <span class="pill {gcls}"><i></i>진입 게이트 {gtxt}</span>
  <span class="pill p-warn"><i></i>{P['span_from']} ~ {P['span_to']}</span>
 </div>

 <section>
  <h2><span class="n">A</span>지금 이 pair 는 어떤 상태인가요</h2>
  <div class="grid g4">
   <div class="card kpi"><div class="lbl">지금 spread</div>
    <div class="val">{fmt(P.get('spread_now'), 4)}</div><div class="note">{P['unit']}</div></div>
   <div class="card kpi"><div class="lbl">평소 대비 (z-score)</div>
    <div class="val">{fmt(P.get('z_now'))}</div><div class="note">±2σ 부터 진입 후보</div></div>
   <div class="card kpi"><div class="lbl">half-life (되돌아오는 속도)</div>
    <div class="val">{(f"{P['half_life_min']:.0f}" if P.get('half_life_min') else '—')}</div>
    <div class="note">분 · 벗어난 거리 절반이 돌아오는 시간</div></div>
   <div class="card kpi"><div class="lbl">t_HAC (증거 세기)</div>
    <div class="val">{fmt(P.get('t_hac'), 2, False)}</div>
    <div class="note">≤ −1.64 라야 게이트 통과</div></div>
  </div>
  <div class="warnbox">{P['note']}</div>
 </section>

 <section>
  <h2><span class="n">B</span>차이가 얼마나 벌어졌나 — 그림으로</h2>
  <div class="card chartbox"><canvas class="cs" height="210"></canvas></div>
  <div class="card chartbox" style="margin-top:12px"><canvas class="cz" height="150"></canvas></div>
  <p class="sub" style="margin-top:8px">위: spread({P['unit']}) — 노란 점선 = 구간 평균.
   아래: z-score — 붉은 띠 = ±2σ 진입선. 그린 구간 {P.get('n_charted', 0):,}봉
   (창 {W}분이 차기 전 구간은 z 를 만들 수 없어 잘라냈습니다).</p>
 </section>

 <section>
  <h2><span class="n">C</span>표본이 어떻게 끊겨 있나 (세션)</h2>
  <p class="lead">12시간 넘는 공백을 사이에 둔 두 봉을 "연속"으로 보면 통계가 오염돼요.
   그래서 60분 넘게 비면 <b>다른 세션</b>으로 끊습니다. 아래가 그 조각들이에요.</p>
  <div class="card tblwrap"><table>
   <tr><th class="tx">#</th><th>봉 수</th><th class="tx">구간</th></tr>{sess}
  </table></div>
  {warn}
 </section>
{extra_html}
</div>"""


ktb_extra = f"""
 <section>
  <h2><span class="n">D</span>판정 일지 — 통계가 하루하루 뒤집히는 중</h2>
  <p class="lead">같은 질문("되돌아오는 성질이 있나?")에 대한 답이 데이터가 쌓일 때마다 흔들려요.
   <b>이 흔들림이야말로 "아직 돈 걸면 안 된다"는 가장 확실한 증거</b>예요.</p>
  <div class="card tblwrap"><table>
   <tr><th class="tx">시점</th><th>ADF p (≤.05 합격)</th><th>half-life</th>
    <th>t_HAC (≤−1.64 통과)</th><th class="tx">판정</th></tr>
   {vrows}
  </table></div>
 </section>

 <section>
  <h2><span class="n">E</span>시뮬레이션 결과 — threshold sweep</h2>
  <p class="lead">"몇 σ에서 들어가는 게 좋았을까"를 과거 데이터로 흉내낸 결과예요.
   <b>표본이 짧아 성적표가 아니라 배관 점검</b>입니다.</p>
  <div class="card tblwrap"><table>
   <tr><th>진입 기준</th><th>완결 거래</th><th>미청산</th><th>손익 (pt)</th><th>손익 (1계약)</th></tr>
   {srows}
  </table></div>
  <div class="warnbox">비용(호가 스프레드 + 수수료) 미반영 · 미청산 포지션은 현재가 평가 포함 ·
   비용까지 반영한 전략 비교는 <span class="mono">tools/strategy_lab.py</span> · 대외 인용 금지.</div>
 </section>"""

kus_extra = """
 <section>
  <h2><span class="n">D</span>이 pair 는 아직 무엇을 못 하나</h2>
  <div class="card tblwrap"><table>
   <tr><th class="tx">항목</th><th class="tx">상태</th><th class="tx">왜</th></tr>
   <tr><td class="tx">헤지비율 β</td><td class="tx">🔴 미적용 (β=1)</td>
    <td class="tx">정식으로는 s = ln P¹ − β·ln P², β는 rolling OLS.
     표본이 짧아 β가 노이즈라 지금은 추정하지 않는다</td></tr>
   <tr><td class="tx">환율(FX) 조정</td><td class="tx">🔴 미반영</td>
    <td class="tx">로그비는 단위를 없앨 뿐 USDKRW 변동을 제거하지 못한다</td></tr>
   <tr><td class="tx">duration 매칭</td><td class="tx">🔴 미반영</td>
    <td class="tx">KTB10과 ZN의 금리 민감도(DV01)가 달라 1:1이 아니다</td></tr>
   <tr><td class="tx">threshold sweep</td><td class="tx">🟠 미실시</td>
    <td class="tx">위 3개가 정리되기 전 손익 숫자는 뜻이 없다</td></tr>
   <tr><td class="tx">겹치는 봉</td><td class="tx">🟠 축적 중</td>
    <td class="tx">CME 수집기가 8/24 19:46~8/25 11:20 (965분) 멈춰 그만큼 비었다</td></tr>
  </table></div>
  <div class="warnbox">그래서 이 탭은 지금 <b>"모이고 있다"를 보는 화면</b>입니다.
   위 4개가 채워지면 KTB 탭과 같은 판정 일지·시뮬레이션이 여기에도 붙습니다.</div>
 </section>"""

panes = pane(PAIRS[0], ktb_extra) + pane(PAIRS[1], kus_extra)
tabs = "".join(
    f'<button class="tab{" on" if i == 0 else ""}" data-go="{P["key"]}">'
    f'<b>{P["tab"]}</b><span>{P["title"]}</span></button>' for i, P in enumerate(PAIRS))

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
header{padding:44px 0 10px}
header h1{font-size:30px;font-weight:700;letter-spacing:-.01em;text-wrap:balance}
header h1 .tag{color:var(--amber)}
.sub{color:var(--muted);font-size:13px}
.oneline{font-size:17px;line-height:1.7;margin:18px 0 14px;max-width:70ch;text-wrap:pretty}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
 border-radius:999px;padding:4px 12px;font-size:11.5px;color:var(--muted);background:var(--panel)}
.pill i{width:7px;height:7px;border-radius:50%;flex:none;background:var(--faint)}
.p-ok i{background:var(--ok)} .p-warn i{background:var(--amber)} .p-crit i{background:var(--crit)}
.p-ok{color:var(--ok)} .p-warn{color:var(--amber)} .p-crit{color:var(--crit)}
nav{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line);
 padding-top:14px;margin-top:8px}
.tab{background:none;border:1px solid var(--line);border-bottom:none;color:var(--muted);
 font:inherit;text-align:left;cursor:pointer;padding:10px 16px;border-radius:12px 12px 0 0;
 line-height:1.35;transform:translateY(1px)}
.tab b{display:block;font-size:14.5px}
.tab span{font-size:11.5px;color:var(--faint)}
.tab.on{background:var(--panel);color:var(--text);border-bottom:1px solid var(--panel)}
.tab.on b{color:var(--amber)}
.tab:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.pane{display:none} .pane.on{display:block}
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
td.tx,th.tx{text-align:left;white-space:normal} tr:last-child td{border-bottom:none}
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
 <h1>Bond Futures Lab <span class="tag">— 채권 Spread 실험 노트</span></h1>
 <p class="sub">개인 학습 실험 · 소액 · 자동 주문 없음 · 어떤 수치도 투자 권유 아님 — __STAMP__ 생성</p>
</header>

<nav aria-label="pair 선택">__TABS__</nav>
__PANES__

<section>
 <h2><span class="n">0</span>이 화면 읽는 법</h2>
 <p class="lead">① 맨 위 <b>탭 두 개</b>가 보는 대상이에요 — 왼쪽은 한국 3년 vs 10년, 오른쪽은 한국 10년 vs 미국 10년.
  ② 탭 안에서 <b>지금 상태 → 그림 → 세션 → 판정</b> 순서로 읽어요.
  ③ 수식이 궁금하면 아래 "Formal Definition"을 펼치세요. 굵은 글씨만 읽어도 충분해요.</p>
 <div class="card tblwrap"><table>
  <tr><th class="tx">기호</th><th class="tx">이름</th><th class="tx">쉬운 뜻</th></tr>
  <tr><td class="tx mono">spread</td><td class="tx">스프레드</td><td class="tx">두 선물 가격의 차이. 우리가 사고파는 대상</td></tr>
  <tr><td class="tx mono">z</td><td class="tx">z-score</td><td class="tx">spread가 평소(최근 2시간)보다 몇 σ 벗어났나. ±2σ = 진입 후보</td></tr>
  <tr><td class="tx mono">half-life</td><td class="tx">반감기</td><td class="tx">벗어난 거리의 절반이 돌아오는 데 걸리는 시간 (고무줄의 세기)</td></tr>
  <tr><td class="tx mono">ADF p</td><td class="tx">정상성 검정</td><td class="tx">"정말 되돌아오는 성질이 있나?" 시험 점수 — 0.05보다 작아야 합격</td></tr>
  <tr><td class="tx mono">t_HAC</td><td class="tx">ECM 보정 t</td><td class="tx">"벌어지면 돌아온다"의 증거 세기 (자기상관 보정) — −1.64보다 작아야 게이트 통과</td></tr>
  <tr><td class="tx mono">세션</td><td class="tx">session</td><td class="tx">주간(09:00~15:45)·야간(18:00~05:00). 사이 공백을 연속으로 보면 안 됨</td></tr>
  <tr><td class="tx mono">1 pt</td><td class="tx">1포인트</td><td class="tx">KTB 선물 1계약 기준 100만원</td></tr>
  <tr><td class="tx mono">×100</td><td class="tx">로그 가격비</td><td class="tx">통화가 다른 pair에서 쓰는 단위 없는 척도 (1 ≈ 1%)</td></tr>
  <tr><td class="tx mono">RV</td><td class="tx">relative value (상대가치)</td><td class="tx"><b>전략 분류</b> — 두 자산의 "상대적" 가격 관계가 정상 범위를 벗어나면, 관계가 되돌아오는 데 베팅. 이 랩의 spread 전략이 정확히 RV</td></tr>
  <tr><td class="tx mono">delta-one</td><td class="tx">델타원</td><td class="tx"><b>상품 분류</b> — 옵션성 없이 기초자산과 1:1로 움직이는 상품(선물·포워드·스왑·ETF). RV와 synonym이 아님: 델타원은 재료, RV는 요리법 — 이 랩은 "델타원 상품(선물)만으로 하는 RV 전략"</td></tr>
 </table></div>
</section>

<section>
 <h2><span class="n">1</span>상품이 뭐예요? — 국채 <b>선물</b></h2>
 <p class="lead">채권은 "나라가 돈을 갚겠다"는 약속 문서예요. <b>국채 선물</b>은 그 약속 문서를
  <b>정해진 날짜에 정해진 값으로 사고팔기로 한 표준 계약</b>이고, 거래소에 상장돼 있어 주식처럼 사고팔 수 있어요.
  이 실험실은 채권 선물 <b>딱 8개</b>만 봅니다.</p>
 <div class="card tblwrap"><table>
  <tr><th class="tx">시장</th><th class="tx">상품</th><th class="tx">쉬운 뜻</th><th class="tx">수집</th></tr>
  <tr><td class="tx">KRX</td><td class="tx mono">KTB3 · KTB10 · KTB30</td>
   <td class="tx">한국 국고채 3·10·30년 선물</td>
   <td class="tx">🟢 주간 실시간(WS) + 야간 REST</td></tr>
  <tr><td class="tx">CME</td><td class="tx mono">ZT · ZF · ZN · ZB · TN</td>
   <td class="tx">미국 국채 2·5·10·30년(+Ultra 10) 선물</td>
   <td class="tx">🟠 WebSocket 수집 — 8/24 밤 965분 결측 후 재가동</td></tr>
 </table></div>
</section>

<section>
 <h2><span class="n">2</span>알고리즘이 뭐예요? — 고무줄 달린 공</h2>
 <p class="lead">두 나라(또는 두 만기)의 금리는 멀리 못 떨어져요 — 고무줄로 묶인 두 공 같아서,
  많이 벌어지면 되돌아오는 경향이 있어요. 알고리즘은 딱 세 단계예요.</p>
 <div class="steps">
  <div class="step"><span class="no">STEP 1</span><b>차이를 잰다</b>
   <p>매 1분, 두 선물 가격의 차이(spread)를 기록해요.</p></div>
  <div class="step"><span class="no">STEP 2</span><b>많이 벌어졌나 본다</b>
   <p>평소(최근 2시간) 대비 몇 σ 벗어났는지(z-score) 계산 — ±2σ부터 진입 후보</p></div>
  <div class="step"><span class="no">STEP 3</span><b>자물쇠 3개를 확인하고 진입</b>
   <p>① |z| ≥ 2σ ② 분포 기준(IQR)으로도 밖 ③ "되돌아온다"는 통계 증거(ECM, 보정 t ≤ −1.64)
    — <b>셋 다</b> 열려야 진입, 되돌아오면(z≈0) 청산</p></div>
 </div>
 <div class="warnbox">자물쇠 ③이 핵심 안전장치예요. 통계가 "되돌아온다는 증거가 부족하다"고 하면
  <b>그날은 거래하지 않아요</b>. 두 탭 모두 지금이 그 상태입니다.</div>
</section>

<section>
 <h2><span class="n">3</span>Formal Definition — 수식으로 정확하게</h2>
 <details><summary>스프레드 · z-score · 진입/청산 규칙</summary><div class="dbody">
  <div class="f">동일 통화(KTB pair): s_t = P¹_t − P²_t &nbsp;[pt]</div>
  <div class="f">이종 통화(KTB10–ZN): s_t = 100·(ln P¹_t − β·ln P²_t), 현재 <b>β = 1 고정</b>(표본 부족으로 미추정)</div>
  <div class="f">z_t = (s_t − μ̂_W) / σ̂_W, &nbsp;W = 120봉(2시간) rolling, <b>세션 내부에서만</b></div>
  <div class="f">진입: |z_t| ≥ 2 ∧ s_t ∉ [Q1 − 1.5·IQR, Q3 + 1.5·IQR] ∧ (γ̂ &lt; 0 ∧ t_HAC(γ̂) ≤ −1.64)
   → sign = −sign(z) · 청산: |z_t| ≤ 0.25</div>
 </div></details>
 <details><summary>OU process · half-life</summary><div class="dbody">
  <div class="f">ds_t = κ(μ − s_t)dt + σ dW_t &nbsp;→ 이산화 AR(1): s_t = a + b·s_{t−1} + ε_t</div>
  <div class="f">κ = −ln(b)/Δt · half-life = ln2/κ &nbsp;— KTB pair: b = __B__, half-life __HLK__ ·
   KTB10–ZN: b = __BU__, half-life __HLU__</div>
 </div></details>
 <details><summary>ECM과 Newey-West 보정 (왜 t가 두 개인가)</summary><div class="dbody">
  <div class="f">Δs_t = α + γ·s_{t−1} + ε_t &nbsp;— KTB pair: γ̂ = __GK__, t_OLS = __TOK__, t_HAC = __THK__</div>
  <div class="f">KTB10–ZN: γ̂ = __GU__, t_OLS = __TOU__, t_HAC = __THU__</div>
  <p class="sub">1분봉 잔차는 자기상관이 강해 OLS 표준오차가 과소평가된다(t 과대).
   Newey-West(lag 10)로 보정한 t만 게이트에 사용. 차분·lag 쌍은 <b>세션 안에서만</b> 만든다.</p>
 </div></details>
 <details><summary>ADF 정상성 검정</summary><div class="dbody">
  <div class="f">Δs_t = α + ρ·s_{t−1} + Σφ_iΔs_{t−i} + ε_t, &nbsp;H₀: ρ=0(단위근) ·
   KTB pair 최신 stat __ADFSTAT__, p __ADFP__ (lag AIC)</div>
  <p class="sub">p ≤ 0.05일 때만 "되돌아오는 성질"을 통계적으로 인정.
   재현: <span class="mono">python tools/econ_pair.py --all</span> ·
   상세는 <span class="mono">reports/TECH_REPORT_deltaone_v0.4_2026-08-25.md</span></p>
 </div></details>
</section>
</div>
<script>
const PAIRS = __PAIRDATA__;
function draw(cv, vals, labels, opts){
 const dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth, H=parseInt(cv.getAttribute("height"),10);
 if(!W||!vals||vals.length<2) return;
 cv.width=W*dpr; cv.height=H*dpr; cv.style.height=H+"px";
 const x=cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0);
 const padL=58,padR=14,padT=12,padB=22;
 let lo=Math.min(...vals), hi=Math.max(...vals);
 if(opts.band){ lo=Math.min(lo,-opts.band*1.15); hi=Math.max(hi,opts.band*1.15); }
 if(opts.mu!==undefined){ lo=Math.min(lo,opts.mu); hi=Math.max(hi,opts.mu); }
 const pad=(hi-lo)*.08||.5; lo-=pad; hi+=pad;
 const N=vals.length, X=i=>padL+(W-padL-padR)*i/(N-1), Y=v=>padT+(H-padT-padB)*(1-(v-lo)/(hi-lo));
 x.font="10.5px 'IBM Plex Mono',monospace"; x.fillStyle="#5A6478"; x.strokeStyle="#232B3A";
 for(let k=0;k<=4;k++){ const v=lo+(hi-lo)*k/4;
  x.beginPath(); x.moveTo(padL,Y(v)); x.lineTo(W-padR,Y(v)); x.stroke();
  x.textAlign="right"; x.fillText(v.toFixed(opts.dec),padL-7,Y(v)+3.5); }
 x.textAlign="center";
 for(let k=0;k<5;k++){ const i=Math.round(k*(N-1)/4); x.fillText(labels[i]||"",X(i),H-6); }
 if(opts.band){ x.fillStyle="rgba(239,106,90,.10)";
  x.fillRect(padL,padT,W-padL-padR,Math.max(0,Y(opts.band)-padT));
  x.fillRect(padL,Y(-opts.band),W-padL-padR,Math.max(0,H-padB-Y(-opts.band)));
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
 document.querySelectorAll(".pane.on").forEach(p=>{
  const P=PAIRS[p.dataset.pair]; if(!P||!P.chart) return;
  const cs=p.querySelector(".cs"), cz=p.querySelector(".cz");
  if(cs) draw(cs,P.chart.s,P.chart.t,{color:"#3FD0C9",fill:"rgba(63,208,201,.16)",dec:2,mu:P.chart.mu});
  if(cz) draw(cz,P.chart.z,P.chart.t,{color:"#F5B84B",fill:"rgba(245,184,75,.14)",dec:1,band:2});
 });
}
function go(key){
 document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.go===key));
 document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.dataset.pair===key));
 if(location.hash.slice(1)!==key) history.replaceState(null,"","#"+key);
 render();
}
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>go(t.dataset.go)));
go(PAIRS[location.hash.slice(1)] ? location.hash.slice(1) : "ktb");
addEventListener("resize", render);
</script>
"""

U = PAIRS[1]
rep = {
    "__STAMP__": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    "__TABS__": tabs, "__PANES__": panes,
    "__B__": fmt(K.get("ar1_b"), 4, False),
    "__HLK__": (f"{K['half_life_min']:.0f}분" if K.get("half_life_min") else "산출 불가"),
    "__BU__": fmt(U.get("ar1_b"), 4, False),
    "__HLU__": (f"{U['half_life_min']:.0f}분" if U.get("half_life_min") else "산출 불가"),
    "__GK__": fmt(K.get("gamma"), 5), "__TOK__": fmt(K.get("t_ols"), 2, False),
    "__THK__": fmt(K.get("t_hac"), 2, False),
    "__GU__": fmt(U.get("gamma"), 5), "__TOU__": fmt(U.get("t_ols"), 2, False),
    "__THU__": fmt(U.get("t_hac"), 2, False),
    "__ADFSTAT__": f"{adf['stat']:.2f}", "__ADFP__": f"{adf['p']:.3f}",
    "__PAIRDATA__": json.dumps({P["key"]: P for P in PAIRS}, ensure_ascii=False),
}
html = HTML
for k, v in rep.items():
    html = html.replace(k, v)

# ── KR/EN 이중 언어 (후처리 — tools/i18n_en_map.py 문구 매핑) ────────────
from i18n_en_map import EN_MAP
_i = html.index('<div class="wrap">')
_j = html.index("<script>")
head, body_kr, tail = html[:_i], html[_i:_j], html[_j:]
body_en = body_kr
for _kr, _en in sorted(EN_MAP, key=lambda x: -len(x[0])):
    body_en = body_en.replace(_kr, _en)
TOGGLE = (
    '<div style="position:fixed;top:14px;right:16px;z-index:50;display:flex;'
    'border:1px solid var(--line);border-radius:999px;overflow:hidden;background:var(--panel)">'
    "<button id=\"lb-kr\" class=\"lb\" onclick=\"setL('kr')\">KR</button>"
    "<button id=\"lb-en\" class=\"lb\" onclick=\"setL('en')\">EN</button></div>"
    '<style>.lb{background:none;border:none;color:var(--muted);font:600 12px/1 '
    '"IBM Plex Sans KR",sans-serif;padding:7px 13px;cursor:pointer}'
    '.lb.on{background:var(--amber);color:#0B0E14}</style>')
LANGJS = """
<script>
function setL(l){
 document.getElementById("Lkr").style.display = (l==="kr") ? "" : "none";
 document.getElementById("Len").style.display = (l==="en") ? "" : "none";
 document.getElementById("lb-kr").classList.toggle("on", l==="kr");
 document.getElementById("lb-en").classList.toggle("on", l==="en");
 try{ localStorage.setItem("deltaone_lang", l); }catch(e){}
 if(typeof go==="function"){ var k=location.hash.slice(1)||"ktb"; go(k); }
}
(function(){ var l="kr"; try{ l=localStorage.getItem("deltaone_lang")||"kr"; }catch(e){}
 setL(l); })();
</script>"""
html = (head + TOGGLE
        + '<div id="Lkr">' + body_kr + "</div>"
        + '<div id="Len" style="display:none">' + body_en + "</div>"
        + tail + LANGJS)
out = ROOT / "frontend" / "sim_dashboard.html"
out.write_text(html, encoding="utf-8")
print("wrote %s (%.0f KB)" % (out, out.stat().st_size / 1024))
for P in PAIRS:
    print("  %-14s %5d봉 · 세션 %d · z %s · t_HAC %s · half-life %s"
          % (P["legs"], P["n_bars"], P["n_sessions"],
             fmt(P.get("z_now")), fmt(P.get("t_hac"), 2, False),
             (f"{P['half_life_min']:.0f}분" if P.get("half_life_min") else "—")))
