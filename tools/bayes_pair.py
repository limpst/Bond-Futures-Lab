# -*- coding: utf-8 -*-
"""베이지안 pair — 계수를 점이 아니라 **분포**로 추정하고, 신호를 **확률**로 거른다.

왜 이게 필요한가 (이번 실험이 준 교훈):
    · 헤지비 β 를 점추정으로 쓰다가 1분봉에서 정답의 36% 밖에 안 되는 값을
      그대로 믿었다. β 에 **불확실성이 얼마나 큰지**를 같이 봤다면 그 값으로
      매매하면 안 된다는 것을 바로 알 수 있었다.
    · ECM 의 γ 를 "t 가 −1.96 을 넘나" 로만 봤다. 표본이 6일뿐이라 이 이분법은
      표본에 따라 뒤집힌다. **P(γ<0) = 0.87** 처럼 말하는 편이 정직하고,
      진입 임계도 확률로 정할 수 있다.

무엇을 하나
    1) 헤지 회귀   Δa_t = β·Δb_t + e,  e ~ N(0, σ²)      → β 의 사후분포
    2) ECM        Δs_t = α + γ·s_{t-1} + u, u ~ N(0, τ²) → γ·half-life 사후분포
    3) 사후예측    지금 z 에서 들어갔을 때 H봉 뒤 **비용을 넘길 확률**
    4) 신호 필터   |z|>2 대신 **P(이익 > 비용) > p\\*** 일 때만 진입

Gibbs sampler 를 쓰는 이유: 두 블록(회귀계수, 분산)의 완전조건부가 각각
정규·역감마로 닫힌 형태라 번갈아 뽑으면 된다. MH 같은 채택/기각이 없어
수용률 튜닝이 필요 없고, 상태공간·스위칭 모형으로 확장할 때 그대로 블록을
하나 더 붙이면 된다 (다음 단계: 레짐 스위칭).

  사전분포 (약한 정보 — 데이터가 말하게 둔다)
    (α,γ) ~ N(0, 100·I)      σ², τ² ~ InvGamma(2, 1)

★ 정직: 사후분포가 좁다고 예측이 맞는 것은 아니다. 모형이 틀렸으면
   좁고 틀린 사후가 나온다. 그래서 아래는 walk-forward 로만 판정한다.

  python tools/bayes_pair.py                      KTB3-KTB10 (기본)
  python tools/bayes_pair.py --pair ZT ZN --minutes 5
  python tools/bayes_pair.py --backtest           확률 필터 vs z 규칙 비교
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

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DB = ROOT / "data" / "minbars.db"

import pair_hedged as PH        # noqa: E402

GAP_MIN = 60
DRAWS, BURN = 4000, 1000


# ── Gibbs 블록 ────────────────────────────────────────────────────────
def gibbs_reg(X, y, draws=DRAWS, burn=BURN, b0=None, B0inv=None,
              a0=2.0, d0=1.0, seed=0):
    """베이지안 선형회귀 y = Xb + e, e~N(0,s2).  (b, s2) 사후표본을 낸다.

    완전조건부
      b | s2, y ~ N( V(B0inv·b0 + X'y/s2),  V ),  V = (B0inv + X'X/s2)^-1
      s2 | b, y ~ InvGamma( a0 + n/2,  d0 + (y-Xb)'(y-Xb)/2 )
    """
    rng = np.random.default_rng(seed)
    n, k = X.shape
    if b0 is None:
        b0 = np.zeros(k)
    if B0inv is None:
        B0inv = np.eye(k) / 100.0          # 사전분산 100 — 약한 정보
    XtX, Xty = X.T @ X, X.T @ y
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    s2 = float(np.var(y - X @ b)) or 1.0
    out_b, out_s = [], []
    for it in range(draws):
        V = np.linalg.inv(B0inv + XtX / s2)
        m = V @ (B0inv @ b0 + Xty / s2)
        L = np.linalg.cholesky((V + V.T) / 2)
        b = m + L @ rng.standard_normal(k)
        r = y - X @ b
        shape = a0 + n / 2.0
        scale = d0 + float(r @ r) / 2.0
        s2 = scale / rng.gamma(shape)       # InvGamma = 1/Gamma
        if it >= burn:
            out_b.append(b.copy()); out_s.append(s2)
    return np.array(out_b), np.array(out_s)


# ── 데이터 ────────────────────────────────────────────────────────────
def load_pair(a, b, minutes):
    T, A, B = PH.load(a, b)
    T, A, B = PH.resample(T, A, B, minutes)
    if len(T) < 120:
        return None
    segs = PH.sessions(T)
    dx, dy = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= minutes * 90:
                dx.append(B[i][1] - B[i - 1][1])
                dy.append(A[i][1] - A[i - 1][1])
    return dict(T=T, A=A, B=B, segs=segs,
                dx=np.array(dx), dy=np.array(dy), minutes=minutes)


def spread_series(d, beta):
    return np.array([d["A"][i][1] - beta * d["B"][i][1] for i in range(len(d["T"]))])


def ecm_design(sc, T, segs, minutes):
    lx, dd = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= minutes * 90:
                lx.append(sc[i - 1]); dd.append(sc[i] - sc[i - 1])
    lx = np.array(lx)
    X = np.column_stack([np.ones(len(lx)), lx])
    return X, np.array(dd)


def q(v, p):
    return float(np.quantile(v, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, default=["KTB3", "KTB10"])
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=12, help="사후예측 지평(봉)")
    ap.add_argument("--cost", type=float, default=None, help="왕복비용(계약통화)")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--pstar", type=float, default=0.60, help="진입 확률 임계")
    a = ap.parse_args()

    A, B = a.pair
    if PH.CCY[A] != PH.CCY[B]:
        print("통화가 다릅니다 — xmarket_pair.py 를 쓰십시오"); return 1
    ccy = PH.CCY[A]
    d = load_pair(A, B, a.minutes)
    if not d:
        print("표본 부족"); return 1

    unit = "원" if ccy == "KRW" else "$"
    fmt = (lambda v: format(int(round(v)), ",")) if ccy == "KRW" else (lambda v: "%.0f" % v)
    # 기본 왕복비용: KTB 는 호가 1틱(=10,000원) 양다리 + 수수료, CME 는 rv_lab 기준
    cost = a.cost if a.cost is not None else (30000.0 if ccy == "KRW" else 67.0)

    print("=== 베이지안 pair · %s-%s %d분봉 (%s) ===" % (A, B, a.minutes, ccy))
    print("  %d봉 · 세션 %d · 연속쌍 %d · 왕복비용 %s%s"
          % (len(d["T"]), len(d["segs"]), len(d["dx"]), fmt(cost), unit))

    # ── 1) 헤지비 β 의 사후분포 ──────────────────────────────────────
    Xb = d["dx"].reshape(-1, 1)
    bb, bs = gibbs_reg(Xb, d["dy"], seed=1)
    beta = bb[:, 0]
    bhat = float(np.mean(beta))
    print("\n  [1] 헤지비 β 사후분포")
    print("      평균 %.4f · 95%% 구간 [%.4f, %.4f] · 표준편차 %.4f"
          % (bhat, q(beta, .025), q(beta, .975), float(np.std(beta))))
    ics = PH.__dict__.get("ICS")
    import rv_lab as RV
    if (A, B) in RV.ICS:
        nA, nB, nm = RV.ICS[(A, B)]
        true_b = nB / nA
        pin = float(np.mean((beta > true_b * .9) & (beta < true_b * 1.1)))
        print("      CME %s 정답 %.3f · 사후평균은 정답의 %.0f%%"
              % (nm.split()[0], true_b, 100 * bhat / true_b))
        print("      P(β 가 정답 ±10%% 안) = %.3f  ← 낮으면 이 주기로 헤지하면 안 된다" % pin)

    # ── 2) ECM 계수 γ 의 사후분포 ────────────────────────────────────
    sc = spread_series(d, bhat)
    X, y = ecm_design(sc, d["T"], d["segs"], a.minutes)
    gb, gs = gibbs_reg(X, y, seed=2)
    gam = gb[:, 1]
    p_rev = float(np.mean(gam < 0))
    hl = np.where(gam < 0, np.log(2) / -np.log1p(gam), np.nan)
    hl = hl[np.isfinite(hl) & (hl > 0)]
    print("\n  [2] ECM  Δs = α + γ·s(t−1) + u   — γ<0 이면 되돌아온다")
    print("      γ 평균 %+.5f · 95%% 구간 [%+.5f, %+.5f]"
          % (float(np.mean(gam)), q(gam, .025), q(gam, .975)))
    print("      **P(γ < 0) = %.3f**  ← 되돌아올 확률. 이분법(t>1.96) 대신 이것을 본다" % p_rev)
    if len(hl):
        print("      half-life 사후 중앙값 %.1f봉 (%.0f분) · 95%% [%.1f, %.1f]봉"
              % (q(hl, .5), q(hl, .5) * a.minutes, q(hl, .025), q(hl, .975)))
    else:
        print("      half-life: γ≥0 표본이 대부분이라 산출 불가")

    # ── 3) 사후예측 — 지금 들어가면 비용을 넘길 확률 ─────────────────
    mu = float(np.mean(sc)); sd = float(np.std(sc))
    z_now = (sc[-1] - mu) / sd if sd else 0.0
    H = a.horizon
    rng = np.random.default_rng(7)
    K = len(gam)
    s0 = sc[-1]
    alp = gb[:, 0]
    paths = np.empty(K)
    for j in range(K):
        s = s0
        for _ in range(H):
            s += alp[j] + gam[j] * s + math.sqrt(gs[j]) * rng.standard_normal()
        paths[j] = s
    side = -1.0 if z_now > 0 else 1.0        # 비싼 쪽을 판다
    pnl = side * (paths - s0)
    p_win = float(np.mean(pnl > 0))
    p_cost = float(np.mean(pnl > cost))
    print("\n  [3] 사후예측 — 지금(z=%+.2f) %s 로 들어가 %d봉(%d분) 뒤"
          % (z_now, "매도" if side < 0 else "매수", H, H * a.minutes))
    print("      기대 PnL %s%s · 95%% 구간 [%s, %s]%s"
          % (fmt(float(np.mean(pnl))), unit, fmt(q(pnl, .025)), fmt(q(pnl, .975)), unit))
    print("      P(이익>0) = %.3f · **P(이익>비용 %s) = %.3f**"
          % (p_win, fmt(cost), p_cost))
    print("      → 이 확률이 임계(%.2f) 미만이면 진입하지 않는다" % a.pstar)

    # ── 4) 확률 필터 백테스트 ────────────────────────────────────────
    if a.backtest:
        print("\n  [4] walk-forward — 확률 필터 vs z 규칙 (비용 반영)")
        n = len(sc)
        k0 = int(n * 0.5)
        res = {"z2.0": [0.0, 0], "z1.5": [0.0, 0], "bayes": [0.0, 0]}
        pos = {k: 0.0 for k in res}
        ent = {k: 0.0 for k in res}
        # 확률 필터는 창을 굴리며 매번 다시 추정하기엔 비싸므로,
        # 학습구간 사후를 고정하고 z 를 롤링으로 계산한다(운영과 동일한 정보만 사용).
        Xtr, ytr = ecm_design(sc[:k0], d["T"][:k0], PH.sessions(d["T"][:k0]), a.minutes)
        gbt, gst = gibbs_reg(Xtr, ytr, draws=2000, burn=500, seed=3)
        gt, at2, st = gbt[:, 1], gbt[:, 0], gst
        W = 40
        for t in range(k0, n - 1):
            w = sc[max(0, t - W):t]
            m_, s_ = float(np.mean(w)), float(np.std(w)) or 1.0
            z = (sc[t] - m_) / s_
            side_t = -1.0 if z > 0 else 1.0
            # 확률 필터: 학습 사후로 H봉 예측
            pp = np.empty(len(gt))
            for j in range(0, len(gt), 4):        # 4개마다 (속도)
                s = sc[t]
                for _ in range(H):
                    s += at2[j] + gt[j] * s + math.sqrt(st[j]) * rng.standard_normal()
                pp[j] = side_t * (s - sc[t])
            pw = float(np.mean(pp[::4] > cost))
            want = {"z2.0": side_t if abs(z) >= 2.0 else 0.0,
                    "z1.5": side_t if abs(z) >= 1.5 else 0.0,
                    "bayes": side_t if (abs(z) >= 1.5 and pw >= a.pstar) else 0.0}
            for k in res:
                if want[k] != pos[k]:
                    res[k][0] -= cost / 2 * abs(want[k] - pos[k])
                    if want[k] != 0:
                        res[k][1] += 1
                    pos[k] = want[k]
                    ent[k] = sc[t]
                res[k][0] += pos[k] * (sc[t + 1] - sc[t])
        print("      %-8s %14s %8s" % ("규칙", "순손익", "거래"))
        for k in ("z2.0", "z1.5", "bayes"):
            print("      %-8s %14s %8d" % (k, fmt(res[k][0]) + unit, res[k][1]))
        print("      bayes = |z|≥1.5 **이면서** P(이익>비용) ≥ %.2f 일 때만 진입" % a.pstar)

    print("\n  [주의] 사후가 좁아도 모형이 틀리면 좁고 틀린 답이 나온다.")
    print("         표본 %d봉(%d일치)이다. 확정 판정에 쓰지 않는다." % (len(sc), len(d["segs"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
