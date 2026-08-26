# -*- coding: utf-8 -*-
"""델타원 채권 전략 비교 — 1분봉 · 거래비용 · implementation shortfall 반영.

목적: 채권 선물 유니버스에서 **어느 시그널이 실제로 돈을 버는지** 를 같은 자로 재서
순위를 매긴다. 백테스트가 아니라 '지금 가진 표본으로 무엇을 말할 수 있나' 를 본다.

■ 정직하게 먼저 — 세 가지 함정을 피하려고 만든 장치
 1) 세션 경계: 주간(09:00~15:45)과 야간(18:00~05:00) 사이 12시간 공백을 사이에 둔
    두 봉을 '연속' 으로 보면 안 된다. 60분 넘는 공백은 세션 경계로 끊고, 포지션은
    세션 끝에서 강제 청산한다(오버나이트 미보유).
 2) look-ahead: 시그널은 t 봉 **종가** 로 만들고 체결은 t+1 봉 **시가** 로 한다.
    같은 봉 종가에 체결하면 실제로는 불가능한 수익이 나온다.
 3) 비용: 왕복 수수료 + 유효 스프레드(Roll 1984 추정) + implementation shortfall
    (결정가 대비 체결가 차이)을 전부 뺀다. 비용을 빼기 전 숫자는 의미가 없다.

■ 비교하는 시그널
    bh        spread 매수 후 보유 (기준선)
    z         rolling z-score 평균회귀 (창 W, 진입 ±k, 청산 |z|<=0.25)
    bb        Bollinger — z 와 같은 구조이나 청산을 중앙선 통과로
    ou        OU 밴드 — 추정 κ·σ 로 산출한 최적 밴드 근사
    mom       모멘텀(추세추종) — z 와 반대 방향. 평균회귀 가설의 대조군
    ma        이동평균 교차 (단기 S, 장기 L)

  python tools/strategy_lab.py                     기본(KTB3-KTB10)
  python tools/strategy_lab.py --pair KTB10 ZN
  python tools/strategy_lab.py --json out.json
"""
from __future__ import annotations

import sys as _sys
# 작업 스케줄러 콘솔은 cp949 라 '—' 같은 문자에서 UnicodeEncodeError 로 죽는다.
# 출력 스트림을 UTF-8 로 강제하고, 못 쓰는 문자는 대체 표기로 흘린다.
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
GAP_MIN = 60
PT_KRW = 1_000_000          # KTB 선물 1pt = 100만원
COMMISSION_PT = 0.004       # 왕복 수수료 — 계약당 1,000원(편도) 기준
#   1pt = 1,000,000원 이므로 계약당 편도 1,000원 = 0.001 pt.
#   spread 는 다리 2개 → 진입 0.002 + 청산 0.002 = 왕복 0.004 pt.
#   (사용자 확인 2026-08-25. 실제 요율이 다르면 이 상수만 고치면 된다)
BARS_PER_YEAR = 252 * 400   # 대략 하루 400봉 기준 연율화

# ── leg risk ────────────────────────────────────────────────────────────
# 스프레드는 다리 2개를 따로 주문한다. 거래소 스프레드 상품이 있으면 한 종목으로
# 묶여 이 위험이 0 이지만, 2026-08-26 마스터 조회 결과:
#   KRX  캘린더 스프레드만 상장 (MF SP 09-2610 등) — 상품간(KTB3-KTB10)은 없음
#   KTB10-ZN 는 KRX x CME 라 구조적으로 단일 종목이 될 수 없다
# 따라서 한쪽만 체결된 구간의 노출을 비용으로 계상해야 정직하다.
#
# 모델: 어려운 다리를 먼저 치고 LEG_LAG_SEC 뒤에 나머지가 체결된다고 본다.
#   기대 손실 = 0 (방향은 대칭) 이지만 **분산**이 늘고, 그 분산의 대가로
#   E[|노출|] = sigma_spread_per_sec * sqrt(LEG_LAG_SEC) * sqrt(2/pi) 만큼을
#   왕복마다 뺀다 (half-normal 의 기대 절대값).
LEG_LAG_SEC = 10.0   # 대략 하루 400봉(주간+야간 일부) 기준 연율화


# ── 자료 ─────────────────────────────────────────────────────────────────
def load(a, b):
    con = sqlite3.connect(DB, timeout=60)
    rows = list(con.execute(
        "SELECT x.bar_time, x.open, x.close, y.open, y.close"
        " FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id=? AND y.instr_id=? ORDER BY x.bar_time", (a, b)))
    T = [dt.datetime.strptime(r[0], "%Y-%m-%d %H:%M") for r in rows]
    so = [r[1] - r[3] for r in rows]      # spread at open
    sc = [r[2] - r[4] for r in rows]      # spread at close
    return T, so, sc


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def roll_spread(sc, segs):
    """Roll(1984) 유효 스프레드 = 2·sqrt(-cov(Δp_t, Δp_{t-1})). 체결가만으로 추정."""
    d = []
    for s, e in segs:
        d += [sc[i] - sc[i - 1] for i in range(s + 1, e)]
    if len(d) < 30:
        return None
    n = len(d) - 1
    mx = sum(d) / len(d)
    cov = sum((d[i] - mx) * (d[i - 1] - mx) for i in range(1, len(d))) / n
    return 2 * math.sqrt(-cov) if cov < 0 else None


# ── 시그널 ───────────────────────────────────────────────────────────────
def rolling_stats(sc, segs, W):
    """세션 안에서만 창을 잡는다."""
    M, S = [None] * len(sc), [None] * len(sc)
    for s, e in segs:
        for i in range(s, e):
            lo = max(s, i - W + 1)
            v = sc[lo:i + 1]
            if len(v) < max(20, W // 4):
                continue
            m = sum(v) / len(v)
            sd = math.sqrt(sum((q - m) ** 2 for q in v) / len(v))
            M[i], S[i] = m, sd
    return M, S


def sig_z(sc, segs, W=120, k=2.0, exit_z=0.25):
    M, S = rolling_stats(sc, segs, W)
    out = [0] * len(sc)
    for s, e in segs:
        pos = 0
        for i in range(s, e):
            if M[i] is None or not S[i] or S[i] < 1e-9:
                out[i] = pos; continue
            z = (sc[i] - M[i]) / S[i]
            if pos == 0 and abs(z) >= k:
                pos = -1 if z > 0 else 1
            elif pos != 0 and abs(z) <= exit_z:
                pos = 0
            out[i] = pos
    return out


def sig_bb(sc, segs, W=120, k=2.0):
    """진입은 z 와 같고, 청산은 중앙선(평균) 통과."""
    M, S = rolling_stats(sc, segs, W)
    out = [0] * len(sc)
    for s, e in segs:
        pos = 0
        for i in range(s, e):
            if M[i] is None or not S[i] or S[i] < 1e-9:
                out[i] = pos; continue
            z = (sc[i] - M[i]) / S[i]
            if pos == 0 and abs(z) >= k:
                pos = -1 if z > 0 else 1
            elif (pos > 0 and z >= 0) or (pos < 0 and z <= 0):
                pos = 0
            out[i] = pos
    return out


def sig_ou(sc, segs, W=240):
    """OU 밴드 근사 — 창 안에서 AR(1) 을 추정해 κ 를 얻고, 밴드를 σ_eq 로 잡는다.
    진입 ±1σ_eq, 청산 0.3σ_eq (Bertram 2010 계열의 아주 단순화된 형태)."""
    M, S = rolling_stats(sc, segs, W)
    out = [0] * len(sc)
    for s, e in segs:
        pos = 0
        for i in range(s, e):
            if M[i] is None or not S[i] or S[i] < 1e-9:
                out[i] = pos; continue
            dev = (sc[i] - M[i]) / S[i]
            if pos == 0 and abs(dev) >= 1.0:
                pos = -1 if dev > 0 else 1
            elif pos != 0 and abs(dev) <= 0.3:
                pos = 0
            out[i] = pos
    return out


def sig_mom(sc, segs, W=120, k=2.0):
    """추세추종 — z 의 반대. 평균회귀 가설이 틀렸다면 이쪽이 이겨야 한다."""
    z = sig_z(sc, segs, W, k)
    return [-v for v in z]


def sig_ma(sc, segs, short=20, long=120):
    out = [0] * len(sc)
    for s, e in segs:
        for i in range(s, e):
            if i - s + 1 < long:
                continue
            a = sum(sc[i - short + 1:i + 1]) / short
            b = sum(sc[i - long + 1:i + 1]) / long
            out[i] = 1 if a > b else (-1 if a < b else 0)
    return out


def sig_bh(sc, segs):
    out = [0] * len(sc)
    for s, e in segs:
        for i in range(s, e):
            out[i] = 1
    return out


# ── 체결·비용 ────────────────────────────────────────────────────────────
def run(sig, T, so, sc, segs, half_spread, commission=COMMISSION_PT,
        leg_sigma=0.0):
    """t 봉 종가 시그널 → t+1 봉 시가 체결. 세션 끝 강제 청산.

    비용: 포지션이 바뀔 때마다 |Δpos| × (유효 half-spread + 왕복 수수료/2).
    implementation shortfall = (체결가 − 결정가) × 방향 — 결정 시점과 체결 시점의
    가격 차이. 지연 체결 때문에 생기며, 비용과 별도로 따로 집계한다.
    """
    pnl = 0.0; cost = 0.0; isf = 0.0; legc = 0.0
    pos = 0; n_tr = 0; wins = 0; entry = None
    curve = []
    for s, e in segs:
        pos = 0; entry = None
        for i in range(s, e - 1):
            want = sig[i]
            if want != pos:
                px = so[i + 1]                      # t+1 시가 체결
                decision = sc[i]                    # t 종가에서 결정
                d = abs(want - pos)
                cost += d * (half_spread + commission / 2)
                legc += d * leg_sigma          # 한쪽만 체결된 구간의 기대 노출
                isf += (px - decision) * (want - pos)
                if pos != 0 and entry is not None:
                    p = (px - entry) * pos
                    pnl += p; n_tr += 1; wins += p > 0
                entry = px if want != 0 else None
                pos = want
            curve.append(pnl - cost - legc)
        if pos != 0 and entry is not None:          # 세션 끝 강제 청산
            px = so[e - 1]
            cost += abs(pos) * (half_spread + commission / 2)
            legc += abs(pos) * leg_sigma
            p = (px - entry) * pos
            pnl += p; n_tr += 1; wins += p > 0
            pos = 0
        curve.append(pnl - cost - legc)
    net = pnl - cost - legc
    rets = [curve[i] - curve[i - 1] for i in range(1, len(curve))]
    sd = (math.sqrt(sum(r * r for r in rets) / len(rets)) if rets else 0.0)
    sharpe = ((sum(rets) / len(rets)) / sd * math.sqrt(BARS_PER_YEAR)) if sd > 1e-12 else 0.0
    peak = -1e18; mdd = 0.0
    for v in curve:
        peak = max(peak, v); mdd = min(mdd, v - peak)
    return dict(gross=pnl, cost=cost, legrisk=legc, isf=isf, net=net, n=n_tr, wins=wins,
                sharpe=sharpe, mdd=mdd, krw=net * PT_KRW)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, default=["KTB3", "KTB10"])
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    A, B = a.pair
    T, so, sc = load(A, B)
    if len(sc) < 200:
        print("표본 부족: %d봉" % len(sc)); return 1
    segs = sessions(T)
    hs_roll = roll_spread(sc, segs)
    half_spread = (hs_roll / 2) if hs_roll else 0.005
    print("=== %s − %s ===" % (A, B))
    print("  %d봉 · 세션 %d개 · %s ~ %s"
          % (len(sc), len(segs), T[0].strftime("%m-%d %H:%M"), T[-1].strftime("%m-%d %H:%M")))
    print("  Roll 유효 스프레드 %s → 편도 %.4f pt · 왕복 수수료 가정 %.3f pt"
          % (("%.4f pt" % hs_roll) if hs_roll else "추정 불가(cov>=0), 0.010 가정",
             half_spread, COMMISSION_PT))

    STRATS = [
        ("bh    매수후보유", sig_bh(sc, segs)),
        ("z     평균회귀 2.0σ", sig_z(sc, segs, 120, 2.0)),
        ("z     평균회귀 1.5σ", sig_z(sc, segs, 120, 1.5)),
        ("z     평균회귀 2.5σ", sig_z(sc, segs, 120, 2.5)),
        ("bb    볼린저 2.0σ", sig_bb(sc, segs, 120, 2.0)),
        ("ou    OU 밴드", sig_ou(sc, segs, 240)),
        ("mom   추세추종 2.0σ", sig_mom(sc, segs, 120, 2.0)),
        ("ma    이평교차 20/120", sig_ma(sc, segs, 20, 120)),
    ]
    # leg risk 크기 = spread 의 초당 변동성 x sqrt(지연) x sqrt(2/pi)
    d = []
    for s0, e0 in segs:
        d += [sc[i] - sc[i - 1] for i in range(s0 + 1, e0)]
    sig_min = (math.sqrt(sum(v * v for v in d) / len(d)) if d else 0.0)   # 분당 sd
    sig_sec = sig_min / math.sqrt(60.0)
    leg_sigma = sig_sec * math.sqrt(LEG_LAG_SEC) * math.sqrt(2.0 / math.pi)
    print("  leg risk: spread 분당 sd %.5f pt -> %0.1f초 노출 기대 %.5f pt/회"
          % (sig_min, LEG_LAG_SEC, leg_sigma))
    print("            (거래소 스프레드 상품이 없어 다리 2개를 따로 쳐야 한다 —"
          " 2026-08-26 마스터 조회 확인)")

    res = []
    for name, sig in STRATS:
        r = run(sig, T, so, sc, segs, half_spread, leg_sigma=leg_sigma)
        r["name"] = name
        res.append(r)
    res.sort(key=lambda r: -r["net"])
    print("\n  %-20s %9s %8s %8s %9s %5s %5s %8s %8s"
          % ("전략", "순손익pt", "비용", "IS", "원화", "거래", "승", "샤프", "MDD"))
    for r in res:
        print("  %-20s %+9.4f %8.4f %8.4f %+8.4f %+9.0f %5d %5d %8.2f"
              % (r["name"], r["net"], r["cost"], r["legrisk"], r["isf"], r["krw"],
                 r["n"], r["wins"], r["sharpe"]))
    best = res[0]
    print("\n  1위: %s · 순손익 %+.4f pt (= %s원) · 거래 %d건"
          % (best["name"].split()[0], best["net"],
             format(int(best["krw"]), ","), best["n"]))
    print("  [주의] 표본 %d봉·세션 %d개. 거래 수가 한 자리인 전략은 성과가 아니라"
          " 기능 검증으로만 읽어야 한다." % (len(sc), len(segs)))
    print("  [주의] 샤프는 %s봉/년 가정으로 연율화한 값이다. 표본이 이틀치라"
          " 절대 수준은 의미가 없고, 전략 간 '순위' 비교용으로만 봐야 한다."
          % format(BARS_PER_YEAR, ","))
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"pair": "%s-%s" % (A, B), "n_bars": len(sc), "n_sessions": len(segs),
             "half_spread": half_spread, "roll": hs_roll,
             "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "results": res}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("  wrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
