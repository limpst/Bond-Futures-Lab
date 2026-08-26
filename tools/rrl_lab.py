# -*- coding: utf-8 -*-
"""RRL — 직접 강화학습(Direct Reinforcement) 트레이더.

두 논문의 key idea 를 그대로 가져와 우리 스프레드 문제에 맞춘다.

[1] Moody & Saffell (2001) "Learning to trade via direct reinforcement",
    IEEE Trans. Neural Networks 12(4). — https://dl.acm.org/doi/10.1109/72.935097
    · 가치함수(Q)를 추정하지 않는다. **정책(포지션)을 직접** 경사상승으로 배운다.
      금융 시계열은 잡음이 커서 가치함수 추정이 잘 안 된다는 것이 논지.
    · 트레이더가 **recurrent** 하다: F_t = tanh(w·x_t + u·F_{t-1} + b).
      직전 포지션이 입력으로 들어가야 **거래비용을 학습에 넣을 수 있다** —
      비용은 F 가 바뀔 때만 나가기 때문이다. 우리 문제의 핵심이 정확히 이것.
    · 보상은 PnL 이 아니라 **미분 샤프비(Differential Sharpe Ratio)**.
      온라인 EWMA 로 위험조정 성과를 매 스텝 준다. 표본이 적을 때 유리하다.
    · 학습은 RTRL (real-time recurrent learning) — dF_t/dw 를 재귀로 나른다.

[2] Spooner et al. (2018) "Market Making via Reinforcement Learning", AAMAS.
    — https://arxiv.org/pdf/1804.04216
    · **자연스러운 보상(증분 PnL)이 오히려 나쁘다** — 학습이 불안정해진다.
      대신 **비대칭 감쇠 보상**: 재고를 들고 있어서 유리하게 흘러 생긴 이익은
      깎고, 불리한 쪽은 그대로 물린다. 그래야 방향 베팅으로 새지 않는다.
    · 깊은 신경망보다 **선형 + tile coding** 이 표본효율에서 이겼다.

우리 문제에 왜 맞나 — 진단과 정확히 맞물린다:
    · gross ≈ 0 이고 손실이 전부 비용이었다 → 비용을 목적함수에 직접 넣는
      RRL 의 recurrent 구조가 맞다. DQN 은 비용을 간접적으로만 본다.
    · 1분봉 헤지비가 정답의 36~66% 라 **헤지가 절반만 걸려 방향 노출이 남았다**
      → Spooner 의 비대칭 감쇠가 바로 그 방향 베팅을 억제한다.
    · 표본이 5분봉 1,400개뿐 → DQN(10⁴~10⁶ 스텝)은 불가. RRL 은 파라미터가
      수십 개라 이 크기에서 학습이 된다.

★ 정직하게 지키는 것
    · walk-forward 만 보고한다. in-sample 성과는 판정에 쓰지 않는다.
    · 시드 20개를 돌려 **분포**를 보고한다. 평균만 보고하지 않는다 —
      시드마다 답이 다르면 그건 정책이 아니라 노이즈다(SA 시드 스윕 교훈).
    · 비용은 규칙기반 랩과 **같은 값**을 쓴다. 비교가 성립해야 한다.

  python tools/rrl_lab.py                      ZT-ZN 5분 · 기본
  python tools/rrl_lab.py --pair ZF ZN --minutes 15
  python tools/rrl_lab.py --ics                ICS 비용(1종목 체결) 로
  python tools/rrl_lab.py --no-damp            비대칭 감쇠 끄고 비교
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
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pair_hedged as PH        # noqa: E402
import rv_lab as RV             # noqa: E402

M_LAGS = 8          # 과거 수익률 몇 개를 볼 것인가
ETA = 0.02          # 미분 샤프 EWMA 감쇠 (Moody&Saffell 의 η)
LR = 0.05           # 학습률
EPOCHS = 60


# ── 데이터 ────────────────────────────────────────────────────────────
def build(a, b, minutes):
    """헤지비를 그 주기에서 재추정한 스프레드(달러) 시계열."""
    d = RV.build(a, b, minutes)
    if not d:
        return None
    sc = d["sc"]
    r = [sc[i] - sc[i - 1] for i in range(1, len(sc))]   # 스프레드 1스텝 변화(달러)
    d["r"] = r
    return d


def zscale(r):
    """수익률을 표준화한다. tanh 입력이 포화되지 않게."""
    n = len(r)
    mu = sum(r) / n
    sd = math.sqrt(sum((v - mu) ** 2 for v in r) / n) or 1.0
    return [(v - mu) / sd for v in r], sd


# ── RRL ───────────────────────────────────────────────────────────────
class RRL:
    """F_t = tanh(w·[1, x_{t-M+1..t}, F_{t-1}]).  파라미터 M+2 개."""

    def __init__(self, m=M_LAGS, seed=0):
        rnd = random.Random(seed)
        self.m = m
        self.w = [rnd.uniform(-0.05, 0.05) for _ in range(m + 2)]  # bias·lags·F_prev

    def feat(self, x, t, fprev):
        return [1.0] + x[t - self.m + 1:t + 1] + [fprev]

    def forward(self, x, t, fprev):
        z = sum(wi * fi for wi, fi in zip(self.w, self.feat(x, t, fprev)))
        return math.tanh(z)


def reward(fprev, f, r_t, cost_unit, damp):
    """한 스텝 보상 (달러).

    기본: R = F_{t-1}·r_t − 비용·|ΔF|
    비대칭 감쇠(Spooner 적응): 포지션을 **줄이지 않은** 스텝에서 생긴
    **유리한** 평가이익만 damp 만큼 깎는다. 불리한 쪽은 그대로 물린다.
    → 재고를 들고 방향이 맞아떨어지길 기다리는 행동이 보상받지 못한다.

    ★ 원논문은 마켓메이킹의 미체결 재고에 적용한 것이고, 여기서는
      '포지션을 유지·확대하는 스텝' 으로 옮긴 **적응**이다. 동일하지 않다.
    """
    pnl = fprev * r_t
    if damp > 0 and abs(f) >= abs(fprev) and pnl > 0:
        pnl -= damp * pnl
    return pnl - cost_unit * abs(f - fprev)


def train(x, r, cost_unit, damp, seed, epochs=EPOCHS, lr=LR):
    """RTRL 로 미분 샤프비를 경사상승. Moody&Saffell §III."""
    net = RRL(seed=seed)
    m = net.m
    n = len(x)
    for _ in range(epochs):
        A = B = 0.0
        fprev = 0.0
        dfprev = [0.0] * len(net.w)       # dF_{t-1}/dw
        grad = [0.0] * len(net.w)
        for t in range(m - 1, n - 1):
            feat = net.feat(x, t, fprev)
            f = math.tanh(sum(wi * fi for wi, fi in zip(net.w, feat)))
            R = reward(fprev, f, r[t + 1], cost_unit, damp)

            # 미분 샤프비의 R 에 대한 미분 (Moody&Saffell eq.16)
            den = B - A * A
            if den > 1e-12:
                dD = (B - A * R) / (den ** 1.5)
            else:
                dD = 1.0                   # 초기 몇 스텝은 PnL 방향으로
            # dR/dF, dR/dFprev
            s = 1.0 if f > fprev else (-1.0 if f < fprev else 0.0)
            dR_df = -cost_unit * s
            dpnl = 0.0 if (damp > 0 and abs(f) >= abs(fprev) and fprev * r[t + 1] > 0) \
                else r[t + 1]
            if damp > 0 and abs(f) >= abs(fprev) and fprev * r[t + 1] > 0:
                dpnl = r[t + 1] * (1 - damp)
            dR_dfp = dpnl + cost_unit * s

            # RTRL: dF/dw = (1-F²)(feat + w_last·dFprev/dw)
            g = 1.0 - f * f
            df = [g * (feat[i] + net.w[-1] * dfprev[i]) for i in range(len(net.w))]
            for i in range(len(net.w)):
                grad[i] += dD * (dR_df * df[i] + dR_dfp * dfprev[i])

            # 미분 샤프 EWMA 갱신
            A += ETA * (R - A)
            B += ETA * (R * R - B)
            fprev, dfprev = f, df

        nrm = math.sqrt(sum(v * v for v in grad)) or 1.0
        for i in range(len(net.w)):
            net.w[i] += lr * grad[i] / nrm      # 정규화 경사 — 발산 방지
    return net


def run(net, x, r, cost_unit, damp):
    """학습된 정책을 실행. 순손익·비용·거래수를 낸다 (달러)."""
    fprev, pnl, cost, trades = 0.0, 0.0, 0.0, 0
    path = []
    for t in range(net.m - 1, len(x) - 1):
        f = net.forward(x, t, fprev)
        c = cost_unit * abs(f - fprev)
        p = fprev * r[t + 1]
        if abs(f - fprev) > 0.05:
            trades += 1
        pnl += p
        cost += c
        path.append(pnl - cost)
        fprev = f
    net_pnl = pnl - cost
    sd = 0.0
    if len(path) > 2:
        dd = [path[i] - path[i - 1] for i in range(1, len(path))]
        mu = sum(dd) / len(dd)
        sd = math.sqrt(sum((v - mu) ** 2 for v in dd) / len(dd))
    sharpe = (sum(path[-1:]) / len(path) / sd * math.sqrt(len(path))) if sd else 0.0
    return dict(net=net_pnl, gross=pnl, cost=cost, n=trades,
                sharpe=sharpe, path=path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, default=["ZT", "ZN"])
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--damp", type=float, default=0.5)
    ap.add_argument("--no-damp", action="store_true")
    ap.add_argument("--ics", action="store_true", help="CME ICS 비용(1종목 체결)")
    ap.add_argument("--split", type=float, default=0.6, help="학습 구간 비율")
    a = ap.parse_args()
    damp = 0.0 if a.no_damp else a.damp

    A, B = a.pair
    d = build(A, B, a.minutes)
    if not d:
        print("표본 부족"); return 1
    r_raw = d["r"]
    x, sd = zscale(r_raw)

    half = RV.TICK_ZN if a.ics else RV.TICK_ZN * 2
    cost_unit = half + RV.FEE_RT / 2      # F 가 0→1 로 갈 때 드는 편도 비용
    name = RV.ICS.get((A, B), (0, 0, ""))[2]

    k = int(len(x) * a.split)
    print("=== RRL 직접 강화학습 · %s-%s %d분봉 %s ===" % (A, B, a.minutes, name))
    print("  %d봉 · 헤지비 %.3f · 스프레드 sd(1스텝) $%.1f" % (d["n"], d["beta"], sd))
    print("  실행경로 %s · 편도비용 $%.1f · 비대칭감쇠 damp=%.2f %s"
          % ("B CME ICS 1종목" if a.ics else "A outright 2다리", cost_unit, damp,
             "(Spooner)" if damp else "(끔 — 순수 PnL)"))
    print("  학습 %d봉 / 검증(walk-forward) %d봉" % (k, len(x) - k))

    outs = []
    for s in range(a.seeds):
        net = train(x[:k], r_raw[:k], cost_unit, damp, seed=s)
        oos = run(net, x[k:], r_raw[k:], cost_unit, damp)
        ins = run(net, x[:k], r_raw[:k], cost_unit, damp)
        outs.append((s, ins, oos))

    oo = sorted(o[2]["net"] for o in outs)
    med = oo[len(oo) // 2]
    pos = sum(1 for v in oo if v > 0)
    print("\n  --- walk-forward 순손익 분포 (시드 %d개) ---" % a.seeds)
    print("      최소 %+8.0f · 25%% %+8.0f · 중앙 %+8.0f · 75%% %+8.0f · 최대 %+8.0f"
          % (oo[0], oo[len(oo) // 4], med, oo[3 * len(oo) // 4], oo[-1]))
    print("      양(+) 시드 %d/%d" % (pos, a.seeds))

    best = max(outs, key=lambda o: o[2]["net"])
    s, ins, oos = best
    print("\n  --- 최고 시드 %d ---" % s)
    print("      in-sample   순 %+8.0f · 총 %+8.0f · 비용 %8.0f · 거래 %3d"
          % (ins["net"], ins["gross"], ins["cost"], ins["n"]))
    print("      walk-fwd    순 %+8.0f · 총 %+8.0f · 비용 %8.0f · 거래 %3d"
          % (oos["net"], oos["gross"], oos["cost"], oos["n"]))

    print("\n  [판정] ", end="")
    if pos >= a.seeds * 0.75 and med > 0:
        print("시드 %d/%d 가 양수이고 중앙값 %+.0f — 정책일 가능성이 있다."
              % (pos, a.seeds, med))
    elif pos <= a.seeds * 0.25:
        print("시드 %d/%d 만 양수 — 이 표본에서 RRL 은 비용을 못 넘었다." % (pos, a.seeds))
    else:
        print("시드마다 부호가 갈린다(%d/%d 양수) — 정책이 아니라 노이즈다."
              % (pos, a.seeds))
    print("         in-sample 이 좋고 walk-forward 가 나쁘면 과적합이다. 둘 다 본다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
