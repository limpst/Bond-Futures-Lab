# -*- coding: utf-8 -*-
"""국경을 넘는 pair 를 **매매 가능한 형태**로 만든다 — KTB10 (KRW) vs ZN (USD).

왜 필요한가: 지금까지 KTB10 − ZN 을 가격끼리 그냥 뺐다(106.04 − 108.73).
단위가 다른 두 숫자라 그 차이는 **매매할 수 없는 숫자**다.

  KTB10  1pt = 1,000,000 KRW · 액면 1억원
  ZN     1pt = 1,000 USD    · 액면 $100,000 · 호가 1/64

그래서 두 가지를 맞춘다.
  1) 통화   ZN 손익을 USDKRW 로 원화 환산한다 (환헤지 없음 — 환율이 요인으로 남는다)
  2) 헤지비  ΔKTB10_KRW = a + b·ΔZN_KRW 회귀로 b 를 구해 ZN 다리를 b 배 잡는다.
             듀레이션 가정(CTD) 없이 데이터가 정하게 한다. 세션 안에서만 차분한다.

그 결과 spread_KRW = KTB10_KRW − b·ZN_KRW 는 **원화 손익 단위**이고,
1 계약 조합을 사고팔았을 때 실제로 오가는 돈이다.

★ 정직: 환헤지를 안 하므로 환율 변동이 스프레드에 섞인다. 그 크기를 함께 보고한다.

  python tools/xmarket_pair.py
  python tools/xmarket_pair.py --strategies
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

KTB_PT_KRW = 1_000_000.0     # KTB 선물 1pt
ZN_PT_USD = 1_000.0          # ZN 선물 1pt
GAP_MIN = 60


def load3():
    """KTB10 · ZN · USDKRW 를 같은 분에 맞춰 읽는다. FX 는 직전값 캐리포워드."""
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    fx = {r["bar_time"]: r["close"] for r in c.execute(
        "SELECT bar_time, close FROM minbar WHERE instr_id='USDKRW'")}
    fxk = sorted(fx)
    rows = list(c.execute(
        "SELECT a.bar_time t, a.open ao, a.close ac, b.open bo, b.close bc"
        " FROM minbar a JOIN minbar b ON a.bar_time=b.bar_time"
        " WHERE a.instr_id='KTB10' AND b.instr_id='ZN' ORDER BY a.bar_time"))
    import bisect
    T, K, Z, F = [], [], [], []
    for r in rows:
        t = r["t"]
        i = bisect.bisect_right(fxk, t) - 1      # 그 시각 이전의 마지막 환율
        if i < 0:
            continue
        T.append(dt.datetime.strptime(t, "%Y-%m-%d %H:%M"))
        K.append((r["ao"], r["ac"]))
        Z.append((r["bo"], r["bc"]))
        F.append(fx[fxk[i]])
    return T, K, Z, F


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def ols(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    b = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    res = [y[i] - (my - b * mx + b * x[i]) for i in range(n)]
    s2 = sum(r * r for r in res) / max(1, n - 2)
    se = math.sqrt(s2 / sxx)
    return b, (b / se if se > 0 else float("nan")), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", action="store_true", help="전략 비교까지")
    a = ap.parse_args()

    T, K, Z, F = load3()
    if len(T) < 200:
        print("표본 부족: %d봉" % len(T)); return 1
    segs = sessions(T)
    print("=== KTB10 (KRW) vs ZN (USD) — 매매 가능한 형태로 ===")
    print("  %d봉 · 세션 %d개 · %s ~ %s · USDKRW %.1f ~ %.1f"
          % (len(T), len(segs), T[0].strftime("%m-%d %H:%M"), T[-1].strftime("%m-%d %H:%M"),
             min(F), max(F)))

    # 원화 환산 계약 가치 (종가 기준)
    kv = [K[i][1] * KTB_PT_KRW for i in range(len(T))]
    zv = [Z[i][1] * ZN_PT_USD * F[i] for i in range(len(T))]

    # 세션 안 차분으로 헤지비 추정
    dx, dy = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                dx.append(zv[i] - zv[i - 1]); dy.append(kv[i] - kv[i - 1])
    r = ols(dx, dy)
    if not r:
        print("헤지비 추정 불가"); return 1
    beta, tb, nb = r
    print("\n  헤지비 b = %.4f  (t=%.1f · 연속쌍 %d)" % (beta, tb, nb))
    print("    ΔKTB10_KRW = a + b·ΔZN_KRW  — ZN 1계약당 KTB10 %.2f 계약 상당" % beta)
    print("    (듀레이션 가정 없이 데이터가 정한 값. CTD 듀레이션을 알면 대조 가능)")

    # 매매 가능한 스프레드 (원화)
    sp_c = [kv[i] - beta * zv[i] for i in range(len(T))]
    sp_o = [K[i][0] * KTB_PT_KRW - beta * (Z[i][0] * ZN_PT_USD * F[i]) for i in range(len(T))]
    mu = sum(sp_c) / len(sp_c)
    sd = math.sqrt(sum((v - mu) ** 2 for v in sp_c) / len(sp_c))
    fmt = lambda v: format(int(round(v)), ",")
    print("\n  spread(원화)  현재 %s · 평균 %s · 표준편차 %s · z %+.2f"
          % (fmt(sp_c[-1]), fmt(mu), fmt(sd), (sp_c[-1] - mu) / sd if sd else 0))

    # 환율이 스프레드에 얼마나 섞였나 — FX 를 고정했을 때와 비교
    f0 = F[0]
    sp_fixed = [kv[i] - beta * (Z[i][1] * ZN_PT_USD * f0) for i in range(len(T))]
    d_fx = [sp_c[i] - sp_fixed[i] for i in range(len(T))]
    sd_fx = math.sqrt(sum((v - sum(d_fx) / len(d_fx)) ** 2 for v in d_fx) / len(d_fx))
    print("  환율 기여   표준편차 %s 원 (스프레드 표준편차의 %.0f%%)"
          % (fmt(sd_fx), 100 * sd_fx / sd if sd else 0))
    print("    환헤지를 안 하므로 이만큼이 금리와 무관한 잡음으로 섞인다.")

    # 계량
    import econ_pair as EP
    lx, ly, dd = [], [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                lx.append(sp_c[i - 1]); ly.append(sp_c[i]); dd.append(sp_c[i] - sp_c[i - 1])
    f = EP.ols(lx, ly)
    hl = (math.log(2) / -math.log(f["b"])) if 0 < f["b"] < 1 else None
    g = EP.ols(lx, dd)
    th = EP.hac_t(lx, dd, g, 10)
    print("\n  AR(1) b=%.4f · half-life %s · ECM t(HAC10) %.2f → %s"
          % (f["b"], ("%.1f분" % hl) if hl else "—", th,
             "유의" if abs(th) > 1.96 else "유의하지 않음"))
    try:
        from statsmodels.tsa.stattools import adfuller
        L = max(segs, key=lambda se: se[1] - se[0])
        st, p, *_ = adfuller(sp_c[L[0]:L[1]], autolag="AIC")
        print("  ADF(최장세션 %d봉) p=%.4f → %s"
              % (L[1] - L[0], p, "정상성 채택" if p < 0.05 else "기각 실패"))
    except ImportError:
        print("  ADF: statsmodels 없음")

    if a.strategies:
        import strategy_lab as SL
        print("\n=== 전략 비교 (원화 단위 · 비용·IS·leg risk 반영) ===")
        d = [dd[i] for i in range(len(dd))]
        sig_min = math.sqrt(sum(v * v for v in d) / len(d))
        leg = sig_min / math.sqrt(60.0) * math.sqrt(6.0) * math.sqrt(2 / math.pi)
        # 비용: KTB 편도 1,000원 + ZN 편도 약 $2.5(≈3,500원) → 왕복 약 9,000원
        comm = 9000.0
        # 유효 스프레드: 각 다리 호가폭의 절반 합 — 보수적으로 원화 15,000원 가정
        half = 15000.0
        print("  leg risk %s원/회 (6초 노출) · 왕복 수수료 %s원 · 유효 스프레드 %s원"
              % (fmt(leg), fmt(comm), fmt(half)))
        STR = [("bb  볼린저2.0", SL.sig_bb(sp_c, segs, 120, 2.0)),
               ("z   평균회귀2.0", SL.sig_z(sp_c, segs, 120, 2.0)),
               ("z   평균회귀1.5", SL.sig_z(sp_c, segs, 120, 1.5)),
               ("ou  OU밴드", SL.sig_ou(sp_c, segs, 240)),
               ("mom 추세추종", SL.sig_mom(sp_c, segs, 120, 2.0)),
               ("bh  매수후보유", SL.sig_bh(sp_c, segs))]
        res = []
        for nm, sg in STR:
            x = SL.run(sg, T, sp_o, sp_c, segs, half, commission=comm, leg_sigma=leg)
            x["name"] = nm; res.append(x)
        res.sort(key=lambda x: -x["net"])
        print("\n  %-16s %14s %12s %12s %6s %5s"
              % ("전략", "순손익(원)", "비용", "legrisk", "거래", "승"))
        for x in res:
            print("  %-16s %14s %12s %12s %6d %5d"
                  % (x["name"], fmt(x["net"]), fmt(x["cost"]), fmt(x["legrisk"]),
                     x["n"], x["wins"]))
        print("\n  [주의] 거래 수가 한 자리면 성과가 아니라 기능 검증이다.")
        print("         비용 가정(수수료 9,000원·유효스프레드 15,000원)은 실측이 아니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
