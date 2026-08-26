# -*- coding: utf-8 -*-
"""헤지비를 넣은 pair 분석 — 계약 가치 단위로 맞춰 다시 잰다.

왜: 지금까지 pair 를 '가격끼리 빼기' 로 만들었다. 그런데 만기가 다른 채권선물은
DV01(금리 1bp 당 손익)이 다르다. ZF(5년)와 ZN(10년)을 1:1 로 빼면 금리 중립이
아니고, 남는 것은 스프레드가 아니라 금리 방향 노출이다.

  ZT  액면 $200,000 · 1pt = $2,000     ← 나머지와 다르다
  ZF  액면 $100,000 · 1pt = $1,000
  ZN  액면 $100,000 · 1pt = $1,000
  TN  액면 $100,000 · 1pt = $1,000
  ZB  액면 $100,000 · 1pt = $1,000

여기서는 두 다리를 **달러 계약 가치**로 바꾼 뒤, 세션 안 차분으로 헤지비 b 를
회귀 추정한다(듀레이션 가정 없이 데이터가 정한다). spread = A − b·B 는 달러
손익 단위이고, 1 조합을 사고팔았을 때 실제로 오가는 돈이다.

ADF 는 두 가지로 돌린다.
  c   상수항만        — 중심선이 고정이라는 가정
  ct  상수+추세항     — 중심선이 움직여도 그 둘레로 돌아오는가
ECM 은 유의한데 ADF(c) 가 실패하면 보통 중심선이 흐르는 경우다. ct 가 그것을 가른다.

  python tools/pair_hedged.py --pair ZF ZN
  python tools/pair_hedged.py --all
  python tools/pair_hedged.py --pair ZN TN --strategies
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import datetime as dt
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DB = ROOT / "data" / "minbars.db"
GAP_MIN = 60

# 1 포인트가 얼마인가 (계약 통화 기준)
PT = {"ZT": 2000.0, "ZF": 1000.0, "ZN": 1000.0, "TN": 1000.0, "ZB": 1000.0,
      "KTB3": 1_000_000.0, "KTB10": 1_000_000.0, "KTB30": 1_000_000.0}
CCY = {"ZT": "USD", "ZF": "USD", "ZN": "USD", "TN": "USD", "ZB": "USD",
       "KTB3": "KRW", "KTB10": "KRW", "KTB30": "KRW"}


def load(a, b):
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        "SELECT x.bar_time t, x.open ao, x.close ac, y.open bo, y.close bc"
        " FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id=? AND y.instr_id=? ORDER BY x.bar_time", (a, b)))
    T = [dt.datetime.strptime(r["t"], "%Y-%m-%d %H:%M") for r in rows]
    A = [(r["ao"] * PT[a], r["ac"] * PT[a]) for r in rows]
    B = [(r["bo"] * PT[b], r["bc"] * PT[b]) for r in rows]
    return T, A, B


def resample(T, A, B, minutes):
    """1분봉을 N분봉으로 묶는다. open=구간 첫 open · close=구간 마지막 close.
    구간 경계는 벽시계(00,05,10...)에 맞춘다 — 세션 경계는 뒤에서 다시 끊는다."""
    if minutes <= 1:
        return T, A, B
    oT, oA, oB = [], [], []
    cur = None
    for i in range(len(T)):
        k = T[i].replace(second=0, microsecond=0,
                         minute=(T[i].minute // minutes) * minutes)
        if cur != k:
            oT.append(k); oA.append([A[i][0], A[i][1]]); oB.append([B[i][0], B[i][1]])
            cur = k
        else:
            oA[-1][1] = A[i][1]; oB[-1][1] = B[i][1]
    return oT, [tuple(x) for x in oA], [tuple(x) for x in oB]


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def analyse(a, b, verbose=True, strategies=False, minutes=1):
    if CCY[a] != CCY[b]:
        print("  %s-%s: 통화가 다르다(%s vs %s) — xmarket_pair.py 를 쓰십시오"
              % (a, b, CCY[a], CCY[b]))
        return None
    T, A, B = load(a, b)
    T, A, B = resample(T, A, B, minutes)
    if len(T) < 200:
        if verbose:
            print("  %s-%s: 표본 %d봉 — 부족" % (a, b, len(T)))
        return None
    segs = sessions(T)
    import econ_pair as EP

    # 세션 안 차분으로 헤지비
    dx, dy = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= minutes * 90:
                dx.append(B[i][1] - B[i - 1][1]); dy.append(A[i][1] - A[i - 1][1])
    hb = EP.ols(dx, dy)
    if not hb:
        return None
    beta, tb = hb["b"], hb["t"]
    # 설명력 R² — 헤지가 실제로 위험을 줄이는가
    my = sum(dy) / len(dy)
    sst = sum((v - my) ** 2 for v in dy)
    r2 = 1 - sum(r * r for r in hb["res"]) / sst if sst else 0.0

    sc = [A[i][1] - beta * B[i][1] for i in range(len(T))]
    so = [A[i][0] - beta * B[i][0] for i in range(len(T))]
    mu = sum(sc) / len(sc)
    sd = math.sqrt(sum((v - mu) ** 2 for v in sc) / len(sc))

    lx, ly, dd = [], [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= minutes * 90:
                lx.append(sc[i - 1]); ly.append(sc[i]); dd.append(sc[i] - sc[i - 1])
    f = EP.ols(lx, ly)
    hl = (math.log(2) / -math.log(f["b"])) if 0 < f["b"] < 1 else None
    g = EP.ols(lx, dd)
    th = EP.hac_t(lx, dd, g, 10)

    p_c = p_ct = None
    L = max(segs, key=lambda se: se[1] - se[0])
    seg = sc[L[0]:L[1]]
    try:
        from statsmodels.tsa.stattools import adfuller
        p_c = float(adfuller(seg, autolag="AIC", regression="c")[1])
        p_ct = float(adfuller(seg, autolag="AIC", regression="ct")[1])
    except ImportError:
        pass

    r = dict(pair="%s-%s" % (a, b), n=len(T), segs=len(segs), beta=beta, t_beta=tb,
             r2=r2, mean=mu, sd=sd, now=sc[-1], z=((sc[-1] - mu) / sd if sd else 0),
             ar1=f["b"], hl=hl, ecm_t=th, adf_c=p_c, adf_ct=p_ct,
             ccy=CCY[a], adf_n=L[1] - L[0], minutes=minutes)
    if verbose:
        u = r["ccy"]
        print("\n=== %s-%s (%s) ===" % (a, b, u))
        print("  %d봉 · 세션 %d · %s ~ %s"
              % (len(T), len(segs), T[0].strftime("%m-%d %H:%M"), T[-1].strftime("%m-%d %H:%M")))
        print("  헤지비 b = %.4f (t=%.1f · R²=%.3f)   %s 1계약당 %s %.3f계약"
              % (beta, tb, r2, b, a, beta))
        if abs(tb) < 2:
            print("     ⚠ b 가 0 과 구별되지 않는다 — 두 다리가 함께 움직이지 않는다")
        elif r2 < 0.1:
            print("     ⚠ R² 가 낮다 — 헤지해도 위험이 별로 안 줄어든다")
        print("  spread  현재 %s · 평균 %s · sd %s · z %+.2f"
              % (fm(sc[-1]), fm(mu), fm(sd), r["z"]))
        print("  AR(1) %.4f · half-life %s · ECM t(HAC10) %.2f → %s"
              % (f["b"], ("%.1f분" % hl) if hl else "—", th,
                 "유의" if abs(th) > 1.96 else "유의하지 않음"))
        if p_c is not None:
            print("  ADF(최장세션 %d봉)  상수항 p=%.4f %s · 상수+추세 p=%.4f %s"
                  % (r["adf_n"], p_c, "통과" if p_c < .05 else "실패",
                     p_ct, "통과" if p_ct < .05 else "실패"))
    if strategies:
        run_strategies(a, b, T, so, sc, segs, dd, r)
    return r


def fm(v):
    return format(int(round(v)), ",")


def run_strategies(a, b, T, so, sc, segs, dd, r):
    import strategy_lab as SL
    u = r["ccy"]
    sig_min = math.sqrt(sum(v * v for v in dd) / len(dd))
    leg = sig_min / math.sqrt(60.0) * math.sqrt(6.0) * math.sqrt(2 / math.pi)
    if u == "USD":
        comm, half = 5.0, 31.25       # 왕복 수수료 $5 · 각 다리 1/64 틱 절반씩
    else:
        comm, half = 9000.0, 15000.0
    print("\n  전략 비교 — leg risk %s/회 · 왕복수수료 %s · 유효스프레드 %s (%s)"
          % (fm(leg), fm(comm), fm(half), u))
    STR = [("bb  볼린저2.0", SL.sig_bb(sc, segs, 120, 2.0)),
           ("z   평균회귀2.0", SL.sig_z(sc, segs, 120, 2.0)),
           ("z   평균회귀1.5", SL.sig_z(sc, segs, 120, 1.5)),
           ("ou  OU밴드", SL.sig_ou(sc, segs, 240)),
           ("mom 추세추종", SL.sig_mom(sc, segs, 120, 2.0)),
           ("bh  매수후보유", SL.sig_bh(sc, segs))]
    res = []
    for nm, sg in STR:
        x = SL.run(sg, T, so, sc, segs, half, commission=comm, leg_sigma=leg)
        x["name"] = nm; res.append(x)
    res.sort(key=lambda x: -x["net"])
    print("  %-16s %12s %10s %10s %6s %5s" % ("전략", "순손익", "비용", "legrisk", "거래", "승"))
    for x in res:
        print("  %-16s %12s %10s %10s %6d %5d"
              % (x["name"], fm(x["net"]), fm(x["cost"]), fm(x["legrisk"]), x["n"], x["wins"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--strategies", action="store_true")
    ap.add_argument("--minutes", type=int, default=1, help="재표본 주기(분)")
    ap.add_argument("--sweep", action="store_true", help="1/5/15/30분 비교")
    a = ap.parse_args()
    if a.all:
        import itertools
        ids = ["ZT", "ZF", "ZN", "TN", "ZB"]
        out = []
        for x, y in itertools.combinations(ids, 2):
            r = analyse(x, y, verbose=False, minutes=a.minutes)
            if r:
                out.append(r)
        out.sort(key=lambda r: -(abs(r["ecm_t"]) if r["ecm_t"] == r["ecm_t"] else 0))
        print("=== CME 채권선물 pair — 헤지비 반영 (%d분봉) ===" % a.minutes)
        print("  %-8s %6s %9s %7s %6s %10s %9s %9s %9s"
              % ("pair", "봉", "헤지비", "t", "R²", "half-life", "ECM t", "ADF c", "ADF ct"))
        for r in out:
            print("  %-8s %6d %9.4f %7.1f %6.3f %10s %9.2f %9s %9s%s"
                  % (r["pair"], r["n"], r["beta"], r["t_beta"], r["r2"],
                     ("%.1f분" % r["hl"]) if r["hl"] else "—", r["ecm_t"],
                     ("%.4f" % r["adf_c"]) if r["adf_c"] is not None else "—",
                     ("%.4f" % r["adf_ct"]) if r["adf_ct"] is not None else "—",
                     "  ★" if (abs(r["ecm_t"]) > 1.96 and r["adf_ct"] is not None
                               and r["adf_ct"] < .05) else ""))
        print("\n  ★ = ECM 유의 + ADF(상수+추세) 통과")
        return 0
    if not a.pair:
        a.pair = ["ZF", "ZN"]
    if a.sweep:
        import itertools
        pairs = [tuple(a.pair)] if a.pair else []
        print("=== 재표본 주기 비교 — %s-%s ===" % tuple(a.pair))
        print("  %5s %7s %8s %6s %11s %9s %9s %9s"
              % ("주기", "봉", "헤지비", "R²", "half-life", "ECM t", "ADF c", "ADF ct"))
        for m in (1, 5, 15, 30, 60, 120):
            r = analyse(a.pair[0], a.pair[1], verbose=False, minutes=m)
            if not r:
                print("  %5d  표본 부족" % m); continue
            print("  %5d %7d %8.4f %6.3f %11s %9.2f %9s %9s%s"
                  % (m, r["n"], r["beta"], r["r2"],
                     ("%.0f분" % r["hl"]) if r["hl"] else "—", r["ecm_t"],
                     ("%.4f" % r["adf_c"]) if r["adf_c"] is not None else "—",
                     ("%.4f" % r["adf_ct"]) if r["adf_ct"] is not None else "—",
                     "  ★" if (abs(r["ecm_t"]) > 1.96 and r["adf_ct"] is not None
                               and r["adf_ct"] < .05) else ""))
        return 0
    analyse(a.pair[0], a.pair[1], verbose=True, strategies=a.strategies, minutes=a.minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
