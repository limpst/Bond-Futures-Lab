# -*- coding: utf-8 -*-
"""리서치·시그널 엔진 — pair 별 spread 상태를 계산해 signals.json 으로 내보낸다.

계산 (데이터를 '비교 가능한 수치'로 가공):
  · 정규화 지수  각 상품 로그가격을 공통 구간 시작=100 으로 — multicum 식 비교
  · spread      ln(P1) − β·ln(P2) (β = 창 내 OLS, 직전 창)
  · z-score     (s − mean)/std   · IQR 위치  (s − median)/IQR
  · ECM         Δs = α + γ·s(t−1) → γ, Newey-West t (되돌림 자격 — OLS t 는 부풀려짐)
  · lead-lag    cross-correlation argmax (±L 봉) — '시차를 두고 같은 방향'의 시차 추정
  · 마이크로     최근 봉 range·거래량, 세션 누적 거래량 (1분봉 기반 v0)
  · 시그널      |z| ≥ 2 이고 IQR 게이트·ECM 게이트 통과 → SHORT/LONG spread 후보
  · 비중        strategy.json 의 weights 스킴 (equal · invvol · kelly) — pair 간 배분
                selector 는 qubo_sa (후보 pair 가 늘어나면 QUBO+SA 로 부분선택 — v0 은 전체)

출력: data/signals.json · data/bars_recent.json (frontend 차트용)
사용: python tools/signal_monitor.py [--window 240] [--emit-order]
      --emit-order 는 시그널 발생 시 execution.place_pair_order 로 **dry 주문 기록**
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from deltaone_backtest import PAIRS, ecm_gamma_hac, stats_window  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
STRAT_FILE = ROOT / "strategy.json"
DEFAULT_STRAT = {"selector": "qubo_sa", "weights": "invvol",
                 "threshold": 2.0, "iqr_fence": 1.5, "ecm_gate": True}


def strat() -> dict:
    if STRAT_FILE.is_file():
        try:
            return {**DEFAULT_STRAT, **json.loads(STRAT_FILE.read_text(encoding="utf-8"))}
        except ValueError:
            pass
    STRAT_FILE.write_text(json.dumps(DEFAULT_STRAT, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return dict(DEFAULT_STRAT)


def series(con, iid: str, n: int) -> list[tuple[str, float]]:
    rows = con.execute("SELECT bar_time, close FROM minbar WHERE instr_id=? AND close>0 "
                       "ORDER BY bar_time DESC LIMIT ?", (iid, n)).fetchall()
    return rows[::-1]


def xcorr_lag(a: list[float], b: list[float], max_lag: int = 30) -> tuple[int, float]:
    """Δlog 수익률 교차상관 argmax — 양수 lag = a 가 b 를 lag 봉 선행."""
    ra = [a[i + 1] - a[i] for i in range(len(a) - 1)]
    rb = [b[i + 1] - b[i] for i in range(len(b) - 1)]
    n = min(len(ra), len(rb))
    ra, rb = ra[-n:], rb[-n:]
    if n < max_lag * 3:
        return 0, 0.0

    def corr(x, y):
        m = len(x)
        mx, my = sum(x) / m, sum(y) / m
        vx = math.sqrt(sum((v - mx) ** 2 for v in x))
        vy = math.sqrt(sum((v - my) ** 2 for v in y))
        if vx < 1e-12 or vy < 1e-12:
            return 0.0
        return sum((x[i] - mx) * (y[i] - my) for i in range(m)) / vx / vy

    best = (0, corr(ra, rb))
    for k in range(1, max_lag + 1):
        c1 = corr(ra[:-k], rb[k:])      # a 선행
        c2 = corr(ra[k:], rb[:-k])      # b 선행
        if abs(c1) > abs(best[1]):
            best = (k, c1)
        if abs(c2) > abs(best[1]):
            best = (-k, c2)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=240, help="창 봉수 (기본 240)")
    ap.add_argument("--emit-order", action="store_true", help="시그널 시 dry 주문 기록")
    a = ap.parse_args()
    cfg = strat()

    con = sqlite3.connect(DB, timeout=60)
    out = {"generated_utc": f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}",
           "strategy": cfg, "window_bars": a.window, "pairs": []}
    bars_export: dict[str, list] = {}
    zseries: dict[str, list] = {}
    sig_pairs = []

    for p1, p2 in PAIRS:
        s1 = series(con, p1, a.window * 2)
        s2 = series(con, p2, a.window * 2)
        d2 = dict(s2)
        ts = [t for t, _ in s1 if t in d2]
        if len(ts) < 40:
            out["pairs"].append({"pair": f"{p1}-{p2}", "status": "insufficient_data",
                                 "bars": len(ts)})
            continue
        la = [math.log(dict(s1)[t]) for t in ts]
        lb = [math.log(d2[t]) for t in ts]
        w = min(a.window, len(ts) - 1)
        xa, xb = la[-w:], lb[-w:]
        # β (직전 창 OLS)
        n = len(xa)
        mb, ma = sum(xb) / n, sum(xa) / n
        vb = sum((v - mb) ** 2 for v in xb)
        beta = (sum((xb[i] - mb) * (xa[i] - ma) for i in range(n)) / vb) if vb > 1e-12 else 1.0
        spread = [xa[i] - beta * xb[i] for i in range(n)]
        m, sd, med, iqr = stats_window(spread)
        z = (spread[-1] - m) / sd
        iqr_pos = (spread[-1] - med) / iqr if iqr > 1e-12 else 0.0
        g, tstat = ecm_gamma_hac(spread)   # Newey-West t (2026-08-24 보고서 §5)
        lag, lc = xcorr_lag(la, lb)
        gate_iqr = cfg["iqr_fence"] <= 0 or abs(iqr_pos) >= cfg["iqr_fence"] * 0.5
        gate_ecm = (not cfg["ecm_gate"]) or (g < 0 and tstat <= -1.64)
        signal = "NONE"
        if abs(z) >= cfg["threshold"] and gate_iqr and gate_ecm:
            signal = "SHORT_SPREAD" if z > 0 else "LONG_SPREAD"
        info = {
            "pair": f"{p1}-{p2}", "bars": n, "beta": round(beta, 4),
            "z": round(z, 3), "iqr_pos": round(iqr_pos, 3),
            "ecm_gamma": round(g, 5), "ecm_t": round(tstat, 2),
            "leadlag_bars": lag, "leadlag_corr": round(lc, 3),
            "gate_iqr": gate_iqr, "gate_ecm": gate_ecm, "signal": signal,
            "last_time": ts[-1],
            "micro": {
                "last_range_ticks": None,
                "session_vol": {p1: sum(v for _, v in con.execute(
                    "SELECT bar_time, volume FROM minbar WHERE instr_id=? "
                    "ORDER BY bar_time DESC LIMIT ?", (p1, w)))},
            },
        }
        out["pairs"].append(info)
        if signal != "NONE":
            sig_pairs.append(info)

        # z-score 시계열 (multicum 차트용) — 현재 β 고정 근사 + 롤링 창 w
        full = [la[i] - beta * lb[i] for i in range(len(la))]
        zs = []
        for i in range(w, len(full)):
            win = full[i - w:i]
            mm = sum(win) / w
            sdd = math.sqrt(max(1e-18, sum((v - mm) ** 2 for v in win) / w))
            zs.append({"t": ts[i], "z": round((full[i] - mm) / sdd, 3)})
        zseries[f"{p1}-{p2}"] = zs[-480:]
        for iid, ser in ((p1, s1), (p2, s2)):
            if iid not in bars_export:
                base = ser[0][1]
                bars_export[iid] = [{"t": t, "c": c, "norm": round(100 * c / base, 4)}
                                    for t, c in ser[-a.window:]]

    # pair 간 비중 (weights 스킴) — invvol = spread 변동성 역수
    if sig_pairs:
        scheme = cfg["weights"]
        if scheme == "equal":
            wts = [1 / len(sig_pairs)] * len(sig_pairs)
        else:                                  # invvol (kelly 는 수익모형 연결 전 = invvol 대체)
            ivs = [1 / max(1e-9, abs(p["z"])) for p in sig_pairs]
            tot = sum(ivs)
            wts = [v / tot for v in ivs]
        for p, wv in zip(sig_pairs, wts):
            p["weight"] = round(wv, 3)
        if scheme == "kelly":
            out["note"] = "kelly 는 기대수익 모형 연결 전 — invvol 로 대체 계산됨 (정직 표기)"

    (ROOT / "data" / "signals.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "data" / "bars_recent.json").write_text(
        json.dumps(bars_export, ensure_ascii=False), encoding="utf-8")
    (ROOT / "data" / "zseries.json").write_text(
        json.dumps(zseries, ensure_ascii=False), encoding="utf-8")
    print(f"[signal] pairs={len(out['pairs'])} 시그널={len(sig_pairs)} "
          f"-> data/signals.json · bars_recent.json")
    for p in out["pairs"]:
        if "z" in p:
            print(f"  {p['pair']:12s} bars={p['bars']} z={p['z']:+.2f} "
                  f"iqr={p['iqr_pos']:+.2f} ecm(γ={p['ecm_gamma']}, t={p['ecm_t']}) "
                  f"lag={p['leadlag_bars']} -> {p['signal']}")
        else:
            print(f"  {p['pair']:12s} {p['status']} (bars={p['bars']})")

    if a.emit_order and sig_pairs:
        from execution import place_pair_order
        for p in sig_pairs:
            i1, i2 = p["pair"].split("-")
            side1 = "SELL" if p["signal"] == "SHORT_SPREAD" else "BUY"
            side2 = "BUY" if side1 == "SELL" else "SELL"
            st = place_pair_order(p["pair"], p["signal"].lower(),
                                  [{"instr": i1, "side": side1, "qty": 1},
                                   {"instr": i2, "side": side2, "qty": 1}],
                                  reason=f"z={p['z']} iqr={p['iqr_pos']} ecm_t={p['ecm_t']}")
            print(f"  [order:{st}] {p['pair']} {p['signal']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
