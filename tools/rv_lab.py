# -*- coding: utf-8 -*-
"""RV(relative value) 랩 — 헤지비를 제대로 걸고, 두 가지 실행 경로로 비용을 재본다.

왜 새로 만들었나 (2026-08-26):
  1분봉 헤지비가 CME ICS 정답 비율의 36~66% 밖에 안 된다는 것을 확인했다.
  즉 지금까지의 실패는 "전략이 나쁘다" 가 아니라 **헤지가 절반만 걸렸다** 였다.
  헤지가 절반이면 나머지 절반은 금리 방향 노출이고, 그건 델타원이 아니다.

두 실행 경로를 나눠 본다 — 이게 비용의 전부를 가른다:
  A. outright 2다리   ZT 2계약 + ZN 1계약 을 따로 친다.
                      유효 스프레드 두 번 + leg risk(한쪽만 체결된 구간 노출).
  B. CME ICS 1종목    거래소 상장 스프레드(TUT/FYT/TUF)를 한 번에 친다.
                      유효 스프레드 한 번 · **leg risk = 0** (동시 체결이 보장된다).

★ 정직: B 의 유효 스프레드 $15.625(ZN 1/64틱) 는 실측이 아니라 가정이다.
   ICS 호가를 받아 재기 전까지 B 의 숫자는 "이렇게 되면" 이지 "이렇다" 가 아니다.

  python tools/rv_lab.py                     기본(ZT-ZN · 5/15분)
  python tools/rv_lab.py --all               ICS 3종 전부
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pair_hedged as PH        # noqa: E402
import strategy_lab as SL       # noqa: E402

# CME 상장 inter-commodity spread — (front, back): (계약수, 계약수, Globex 코드)
ICS = {
    ("ZT", "ZN"): (2, 1, "TUT  2년 vs 10년"),
    ("ZT", "ZF"): (3, 2, "TUF  2년 vs 5년"),
    ("ZF", "ZN"): (2, 1, "FYT  5년 vs 10년"),
}

TICK_ZN = 1000.0 / 64        # $15.625 — ZN 1/64 틱
FEE_RT = 5.0                 # 왕복 수수료(2계약분, 보수적)


def build(a, b, minutes):
    """헤지비를 그 주기에서 다시 추정해 스프레드 시계열을 만든다."""
    T, A, B = PH.load(a, b)
    T, A, B = PH.resample(T, A, B, minutes)
    if len(T) < 150:
        return None
    segs = PH.sessions(T)
    import econ_pair as EP
    dx, dy = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= minutes * 90:
                dx.append(B[i][1] - B[i - 1][1]); dy.append(A[i][1] - A[i - 1][1])
    hb = EP.ols(dx, dy)
    if not hb:
        return None
    beta = hb["b"]
    sc = [A[i][1] - beta * B[i][1] for i in range(len(T))]
    so = [A[i][0] - beta * B[i][0] for i in range(len(T))]
    d = [sc[i] - sc[i - 1] for i in range(1, len(sc))]
    sig_bar = math.sqrt(sum(v * v for v in d) / len(d)) if d else 0.0
    return dict(T=T, segs=segs, sc=sc, so=so, beta=beta, sig_bar=sig_bar,
                n=len(T), minutes=minutes)


def strategies(sc, segs):
    w = 40                       # 재표본 후에는 봉 수가 적으므로 창을 줄인다
    return [("z   평균회귀2.0", SL.sig_z(sc, segs, w, 2.0)),
            ("z   평균회귀1.5", SL.sig_z(sc, segs, w, 1.5)),
            ("bb  볼린저2.0", SL.sig_bb(sc, segs, w, 2.0)),
            ("ou  OU밴드", SL.sig_ou(sc, segs, w * 2)),
            ("mom 추세추종", SL.sig_mom(sc, segs, w, 2.0)),
            ("bh  매수후보유", SL.sig_bh(sc, segs))]


def report(a, b, minutes):
    d = build(a, b, minutes)
    if not d:
        print("  %s-%s %d분: 표본 부족" % (a, b, minutes)); return
    nA, nB, name = ICS[(a, b)]
    sd = math.sqrt(sum((v - sum(d["sc"]) / d["n"]) ** 2 for v in d["sc"]) / d["n"])
    # leg risk: 한 다리를 친 뒤 LEG_LAG_SEC 동안 남는 기대 노출
    sig_sec = d["sig_bar"] / math.sqrt(minutes * 60.0)
    legr = sig_sec * math.sqrt(SL.LEG_LAG_SEC) * math.sqrt(2.0 / math.pi)

    print("\n=== %s-%s · %d분봉 · %s ===" % (a, b, minutes, name))
    print("  %d봉 · 헤지비 %.3f (정답 %.3f · %.0f%%) · spread sd $%.0f"
          % (d["n"], d["beta"], nB / nA, 100 * d["beta"] / (nB / nA), sd))

    PATHS = [("A outright 2다리", TICK_ZN * 2, FEE_RT, legr),
             ("B CME ICS 1종목", TICK_ZN,     FEE_RT, 0.0)]
    for pname, half, fee, lg in PATHS:
        rt = 2 * half + fee + 2 * lg
        print("  ── %s · 왕복비용 $%.0f (유효스프레드 $%.1f×2 + 수수료 $%.0f + legrisk $%.1f×2)"
              % (pname, rt, half, fee, lg))
        res = []
        for nm, sg in strategies(d["sc"], d["segs"]):
            x = SL.run(sg, d["T"], d["so"], d["sc"], d["segs"], half,
                       commission=fee, leg_sigma=lg)
            x["name"] = nm; res.append(x)
        res.sort(key=lambda x: -x["net"])
        for x in res[:3]:
            flag = " ◀ 양(+)" if x["net"] > 0 else ""
            print("       %-16s 순손익 %+9.0f · 총 %+9.0f · 비용 %8.0f · 거래 %3d · 승 %3d%s"
                  % (x["name"], x["net"], x["net"] + x["cost"] + x["legrisk"],
                     x["cost"] + x["legrisk"], x["n"], x["wins"], flag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--minutes", nargs="*", type=int, default=[5, 15])
    a = ap.parse_args()
    pairs = list(ICS) if a.all else [("ZT", "ZN")]
    print("RV 랩 — 헤지비 재추정 + 실행경로 2종 비교")
    print("(B 의 유효스프레드는 가정이다. ICS 호가를 재기 전까지 확정이 아니다)")
    for x, y in pairs:
        for m in a.minutes:
            report(x, y, m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
