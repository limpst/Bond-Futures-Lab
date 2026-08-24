# -*- coding: utf-8 -*-
"""델타원 z-score spread 백테스트 + parameter sweep (채권 선물 전용).

전략 (README 스펙): spread = ln(P1) − β·ln(P2) (β = 롤링 OLS, t−1 까지만 사용)
  진입: |z| ≥ threshold  그리고  |s − median| ≥ f×IQR  그리고  ECM γ<0 유의
  청산: |z| ≤ exit_band · 손절: 진입 후 z 가 1σ 추가 역행
  PnL: 포지션 × Δspread (로그-스프레드 단위의 비용 미반영 proxy — 성과 주장 금지)

sweep 축: 봉 주기 {min, day, week, month} × 창 {3mo, 6mo}
        × threshold {1.5, 2.0, 2.5} × IQR fence {0, 1.5} × ECM 게이트 {off, on}

출력: data/sweep_results.csv (조합별 지표 전부 — 최적 1개만 보고하지 않는다)
      data/multicum_<pair>_<freq>.csv (조합별 누적 PnL 시계열 — multicum 차트용)

데이터: data/minbars.db 의 minbar. 데이터가 없으면 정직하게 '0건'으로 끝난다.
표준 라이브러리만 사용 (pandas 불필요).
"""
from __future__ import annotations

import csv
import io
import math
import sqlite3
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"

PAIRS = [("KTB10", "ZN"), ("KTB3", "ZT"),
         ("KTB3", "KTB10"), ("KTB10", "KTB30"),
         ("ZT", "ZN"), ("ZN", "ZB"), ("ZN", "TN")]
FREQS = ["min", "day", "week", "month"]
# 창(개월) → 주기별 봉 수 근사 (KRX 6.5h≈390분/일 · 21일/월 기준)
WIN_BARS = {"min": {3: 63 * 390, 6: 126 * 390},
            "day": {3: 63, 6: 126},
            "week": {3: 13, 6: 26},
            "month": {3: 3, 6: 6}}
THRESHOLDS = [1.5, 2.0, 2.5]
IQR_FENCES = [0.0, 1.5]
ECM_GATES = [False, True]
EXIT_BAND = 0.5
ECM_T_CRIT = -1.64            # 5% 단측


def load_series(con, instr: str) -> list[tuple[str, float]]:
    return [(t, c) for t, c in con.execute(
        "SELECT bar_time, close FROM minbar WHERE instr_id=? AND close>0 "
        "ORDER BY bar_time", (instr,))]


def resample(series: list[tuple[str, float]], freq: str) -> list[tuple[str, float]]:
    """기간 라벨별 마지막 종가. min 은 원본 그대로."""
    if freq == "min":
        return series
    out: dict[str, tuple[str, float]] = {}
    for t, c in series:                       # 't' = 'YYYY-MM-DD HH:MM'
        d = t[:10]
        if freq == "day":
            key = d
        elif freq == "month":
            key = d[:7]
        else:                                 # week: ISO 주
            import datetime as dt
            y, w, _ = dt.date.fromisoformat(d).isocalendar()
            key = f"{y}-W{w:02d}"
        out[key] = (key, c)                   # 시간순 순회라 마지막 값이 남는다
    return [out[k] for k in sorted(out)]


def align(a, b) -> tuple[list[str], list[float], list[float]]:
    """공통 라벨 inner join (cross-market 는 겹치는 시점만 — 정직한 교집합)."""
    db_ = dict(b)
    ts, xa, xb = [], [], []
    for t, c in a:
        if t in db_:
            ts.append(t)
            xa.append(math.log(c))
            xb.append(math.log(db_[t]))
    return ts, xa, xb


class Roll:
    """롤링 합계로 mean/var/cov/beta 를 O(1) 갱신."""
    def __init__(self, w: int):
        self.w = w
        self.qx, self.qy = deque(), deque()
        self.sx = self.sy = self.sxx = self.syy = self.sxy = 0.0

    def push(self, x: float, y: float):
        self.qx.append(x); self.qy.append(y)
        self.sx += x; self.sy += y
        self.sxx += x * x; self.syy += y * y; self.sxy += x * y
        if len(self.qx) > self.w:
            ox, oy = self.qx.popleft(), self.qy.popleft()
            self.sx -= ox; self.sy -= oy
            self.sxx -= ox * ox; self.syy -= oy * oy; self.sxy -= ox * oy

    def full(self) -> bool:
        return len(self.qx) == self.w

    def beta(self) -> float:                  # y ~ beta * x
        n = len(self.qx)
        vx = self.sxx - self.sx * self.sx / n
        return (self.sxy - self.sx * self.sy / n) / vx if vx > 1e-12 else 1.0


def stats_window(vals: list[float]) -> tuple[float, float, float, float]:
    """(mean, std, median, IQR)"""
    n = len(vals)
    m = sum(vals) / n
    sd = math.sqrt(max(1e-18, sum((v - m) ** 2 for v in vals) / n))
    s = sorted(vals)
    med = s[n // 2]
    iqr = s[(3 * n) // 4] - s[n // 4]
    return m, sd, med, iqr


def ecm_gamma(spread: list[float]) -> tuple[float, float]:
    """Δs(t) = α + γ·s(t−1) — (γ, t-stat)."""
    x = spread[:-1]
    y = [spread[i + 1] - spread[i] for i in range(len(spread) - 1)]
    n = len(x)
    if n < 8:
        return 0.0, 0.0
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    if vx < 1e-14:
        return 0.0, 0.0
    g = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / vx
    a = my - g * mx
    sse = sum((y[i] - a - g * x[i]) ** 2 for i in range(n))
    se = math.sqrt(max(1e-18, sse / max(1, n - 2) / vx))
    return g, g / se


def ecm_gamma_hac(spread: list[float], maxlags: int | None = None) -> tuple[float, float]:
    """Δs(t) = α + γ·s(t−1) — (γ, Newey-West t).

    1분봉 spread 는 자기상관이 강해 OLS t 가 부풀려진다 (2026-08-24 기술
    보고서: t_OLS −3.28 vs t_HAC −1.38). 게이트는 이 HAC t 를 쓴다.
    maxlags 기본값: NW rule of thumb floor(4·(n/100)^(2/9)).
    """
    x = spread[:-1]
    y = [spread[i + 1] - spread[i] for i in range(len(spread) - 1)]
    n = len(x)
    if n < 12:
        return 0.0, 0.0
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    if vx < 1e-14:
        return 0.0, 0.0
    g = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / vx
    a = my - g * mx
    u = [x[i] - mx for i in range(n)]
    e = [y[i] - a - g * x[i] for i in range(n)]
    sc = [u[i] * e[i] for i in range(n)]           # score
    L = maxlags if maxlags is not None else max(1, int(4 * (n / 100.0) ** (2.0 / 9.0)))
    S = sum(v * v for v in sc)
    for lag in range(1, L + 1):
        w = 1.0 - lag / (L + 1.0)
        S += 2.0 * w * sum(sc[i] * sc[i - lag] for i in range(lag, n))
    se = math.sqrt(max(1e-18, S)) / vx
    return g, g / se


def backtest(ts, la, lb, w, thr, fence, use_ecm):
    """(trades, wins, pnl_series) — 모든 통계는 t−1 창으로 계산 (lookahead 금지)."""
    roll = Roll(w)
    hist: deque[float] = deque(maxlen=w)      # spread 이력 (t−1 beta 로 계산된 값)
    pos = 0
    entry_z = 0.0
    trades = wins = 0
    entry_pnl_base = 0.0
    cum = 0.0
    pnl_ts: list[tuple[str, float]] = []
    prev_s = None

    for i in range(len(ts)):
        if roll.full():
            b = roll.beta()                   # t−1 까지의 창
            s = la[i] - b * lb[i]
            if prev_s is not None and pos != 0:
                cum += pos * (s - prev_s)
            if len(hist) == w:
                m, sd, med, iqr = stats_window(list(hist))
                z = (s - m) / sd
                if pos == 0:
                    gate_iqr = fence <= 0 or abs(s - med) >= fence * iqr
                    if abs(z) >= thr and gate_iqr:
                        ok = True
                        if use_ecm:
                            g, t = ecm_gamma_hac(list(hist))
                            ok = g < 0 and t <= ECM_T_CRIT
                        if ok:
                            pos = -1 if z > 0 else 1
                            entry_z, entry_pnl_base = z, cum
                            trades += 1
                else:
                    stop = (pos == -1 and z >= entry_z + 1.0) or \
                           (pos == 1 and z <= entry_z - 1.0)
                    if abs(z) <= EXIT_BAND or stop:
                        if cum > entry_pnl_base:
                            wins += 1
                        pos = 0
            hist.append(s)
            prev_s = s
            pnl_ts.append((ts[i], cum))
        roll.push(lb[i], la[i])
    return trades, wins, pnl_ts


def metrics(pnl_ts):
    if len(pnl_ts) < 3:
        return 0.0, 0.0, 0.0
    vals = [v for _, v in pnl_ts]
    rets = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    m = sum(rets) / len(rets)
    sd = math.sqrt(max(1e-18, sum((r - m) ** 2 for r in rets) / len(rets)))
    sharpe = m / sd * math.sqrt(252) if sd > 0 else 0.0   # 주기 무시한 근사 — 참고용
    peak, mdd = -1e18, 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return vals[-1], sharpe, mdd


def main() -> int:
    if not DB.is_file():
        print("[sweep] data/minbars.db 없음 — 먼저 수집하십시오 (collect_minbars.py --live)")
        return 1
    con = sqlite3.connect(DB)
    counts = dict(con.execute("SELECT instr_id, COUNT(*) FROM minbar GROUP BY instr_id"))
    print("[sweep] 보유 봉수:", counts or "0건")
    if not counts:
        print("[sweep] 데이터 0건 — 정직하게 종료. 수집 후 다시 실행.")
        return 0

    results = []
    for p1, p2 in PAIRS:
        s1, s2 = load_series(con, p1), load_series(con, p2)
        if not s1 or not s2:
            continue
        for freq in FREQS:
            r1, r2 = resample(s1, freq), resample(s2, freq)
            ts, la, lb = align(r1, r2)
            mc_rows: dict[str, dict[str, float]] = {}
            mc_cols = []
            for mo, w in WIN_BARS[freq].items():
                if len(ts) < w * 2:
                    continue
                for thr in THRESHOLDS:
                    for fence in IQR_FENCES:
                        for ecm in ECM_GATES:
                            tr, wn, pnl = backtest(ts, la, lb, w, thr, fence, ecm)
                            tot, sh, mdd = metrics(pnl)
                            tag = f"{mo}mo_z{thr}_iqr{fence}_ecm{int(ecm)}"
                            results.append({
                                "pair": f"{p1}-{p2}", "freq": freq, "window_mo": mo,
                                "bars": len(ts), "threshold": thr, "iqr_fence": fence,
                                "ecm_gate": int(ecm), "trades": tr, "wins": wn,
                                "hit": round(wn / tr, 3) if tr else "",
                                "total_pnl": round(tot, 6), "sharpe_approx": round(sh, 3),
                                "maxdd": round(mdd, 6)})
                            mc_cols.append(tag)
                            for t, v in pnl:
                                mc_rows.setdefault(t, {})[tag] = v
            if mc_cols:
                out = ROOT / "data" / f"multicum_{p1}-{p2}_{freq}.csv"
                with out.open("w", newline="", encoding="utf-8") as f:
                    wtr = csv.writer(f)
                    wtr.writerow(["time"] + mc_cols)
                    for t in sorted(mc_rows):
                        wtr.writerow([t] + [mc_rows[t].get(c, "") for c in mc_cols])
                print(f"[sweep] multicum -> {out.name} ({len(mc_rows)}행 × {len(mc_cols)}조합)")

    if results:
        out = ROOT / "data" / "sweep_results.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            wtr = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            wtr.writeheader()
            wtr.writerows(results)
        print(f"[sweep] {len(results)}조합 -> {out}")
        results.sort(key=lambda r: -(r["sharpe_approx"] or 0))
        print("[sweep] 상위 5 (참고용 — 비용 미반영, 성과 주장 금지):")
        for r in results[:5]:
            print(f"  {r['pair']:11s} {r['freq']:5s} {r['window_mo']}mo z{r['threshold']} "
                  f"iqr{r['iqr_fence']} ecm{r['ecm_gate']} | trades={r['trades']} "
                  f"hit={r['hit']} pnl={r['total_pnl']} sh~{r['sharpe_approx']} mdd={r['maxdd']}")
    else:
        print("[sweep] 유효 조합 없음 — 봉 수가 창 길이의 2배 미만입니다. 수집을 더 쌓으십시오.")
    con.close()
    return 0


if __name__ == "__main__":
    # cp949 콘솔 대비 — import 시가 아니라 단독 실행 시에만 감싼다
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
