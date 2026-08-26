# -*- coding: utf-8 -*-
"""pair 계량 분석 — 세션 경계를 아는 버전.

왜 새로 쓰나: 기존 econometrics_report.py 는 `spread.diff()` 를 전 구간에
그대로 걸었다. KTB 는 주간(09:00~15:45)과 야간(18:00~05:00) 사이에 12시간
공백이 있고, 그 공백을 사이에 둔 두 봉의 차이가 '1분 변화' 로 섞여 들어간다.
2026-08-24 실측에서 그 한 건이 표본 전체 최대 차분이었다 — AR(1)·ADF·ECM 이
직접 오염된다.

여기서는 60분 넘는 공백을 세션 경계로 보고, **차분·lag 쌍을 세션 안에서만**
만든다. ADF 는 연속성이 필요하므로 가장 긴 세션에서 돌린다(표본 수 함께 보고).

  python tools/econ_pair.py                     KTB3-KTB10 (기본)
  python tools/econ_pair.py --pair KTB10 ZN     한·미 10년물
  python tools/econ_pair.py --json out.json
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
GAP_MIN = 60          # 이보다 긴 공백은 세션 경계로 본다


def load_pair(a: str, b: str):
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT x.bar_time AS t, x.close AS pa, y.close AS pb"
        " FROM minbar x JOIN minbar y ON x.bar_time = y.bar_time"
        " WHERE x.instr_id = ? AND y.instr_id = ? ORDER BY x.bar_time", (a, b)))
    T = [dt.datetime.strptime(r["t"], "%Y-%m-%d %H:%M") for r in rows]
    S = [r["pa"] - r["pb"] for r in rows]
    return T, S


def sessions(T, gap_min=GAP_MIN):
    """[(start_idx, end_idx_exclusive)] — 공백으로 끊은 구간들."""
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > gap_min * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def ols(x, y):
    """단순 회귀 y = a + b x — 계수·t값·잔차 반환."""
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n; my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return None
    b = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    a = my - b * mx
    res = [y[i] - (a + b * x[i]) for i in range(n)]
    s2 = sum(r * r for r in res) / max(1, n - 2)
    se = math.sqrt(s2 / sxx) if sxx > 0 else float("nan")
    return dict(a=a, b=b, se=se, t=(b / se if se and se == se and se > 0 else float("nan")),
                n=n, res=res)


def hac_t(x, y, fit, maxlags=10):
    """Newey-West 보정 t — 1분봉은 자기상관이 강해 OLS t 가 부풀려진다."""
    n = fit["n"]; res = fit["res"]
    mx = sum(x) / n
    xc = [v - mx for v in x]
    sxx = sum(v * v for v in xc)
    if sxx <= 0:
        return float("nan")
    u = [xc[i] * res[i] for i in range(n)]
    S = sum(v * v for v in u)
    for L in range(1, min(maxlags, n - 1) + 1):
        w = 1.0 - L / (maxlags + 1.0)
        S += 2.0 * w * sum(u[i] * u[i - L] for i in range(L, n))
    var = S / (sxx ** 2)
    se = math.sqrt(var) if var > 0 else float("nan")
    return fit["b"] / se if se == se and se > 0 else float("nan")


def analyse(a: str, b: str):
    T, S = load_pair(a, b)
    res = {"pair": "%s-%s" % (a, b), "n_bars": len(S),
           "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if len(S) < 30:
        res["status"] = "표본 부족"
        return res
    segs = sessions(T)
    res["span"] = [T[0].strftime("%Y-%m-%d %H:%M"), T[-1].strftime("%Y-%m-%d %H:%M")]
    res["n_sessions"] = len(segs)
    res["sessions"] = [{"n": e - s,
                        "from": T[s].strftime("%m-%d %H:%M"),
                        "to": T[e - 1].strftime("%m-%d %H:%M")} for s, e in segs]
    mu = sum(S) / len(S)
    sd = math.sqrt(sum((v - mu) ** 2 for v in S) / len(S))
    res.update(spread_now=S[-1], spread_mean=mu, spread_std=sd,
               z_full=(S[-1] - mu) / sd if sd else 0.0)

    # 세션 안에서만 만든 (t-1, t) 쌍
    lag_x, lag_y, diffs = [], [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:      # 1분봉 연속만
                lag_x.append(S[i - 1]); lag_y.append(S[i]); diffs.append(S[i] - S[i - 1])
    res["n_pairs"] = len(lag_x)
    if len(lag_x) < 30:
        res["status"] = "연속 쌍 부족"
        return res

    f = ols(lag_x, lag_y)
    bb = f["b"]
    res["ar1_b"] = bb
    res["half_life_min"] = (math.log(2) / -math.log(bb)) if 0 < bb < 1 else None

    # ECM: Δs(t) = α + γ·s(t-1)   γ<0 유의 = 벌어지면 되돌아온다
    g = ols(lag_x, diffs)
    res["ecm_gamma"] = g["b"]
    res["ecm_t_ols"] = g["t"]
    res["ecm_t_hac10"] = hac_t(lag_x, diffs, g, 10)

    # ADF 는 연속 구간이 필요 — 가장 긴 세션에서만
    longest = max(segs, key=lambda se: se[1] - se[0])
    seg = S[longest[0]:longest[1]]
    res["adf_n"] = len(seg)
    res["adf_session"] = "%s ~ %s" % (T[longest[0]].strftime("%m-%d %H:%M"),
                                      T[longest[1] - 1].strftime("%m-%d %H:%M"))
    try:
        from statsmodels.tsa.stattools import adfuller
        st, p, lags, nobs, crit, _ = adfuller(seg, autolag="AIC")
        res["adf_stat"], res["adf_p"] = float(st), float(p)
        st2, p2, *_ = adfuller([seg[i] - seg[i - 1] for i in range(1, len(seg))], autolag="AIC")
        res["adf_diff_p"] = float(p2)
    except ImportError:
        res["adf_note"] = "statsmodels 없음 — anaconda python 으로 실행하십시오"
    res["status"] = "ok"
    return res


def show(r):
    print("\n=== %s ===" % r["pair"])
    print("  봉 %d · 세션 %s · 연속쌍 %s" % (r["n_bars"], r.get("n_sessions", "-"), r.get("n_pairs", "-")))
    if r.get("span"):
        print("  구간 %s ~ %s" % tuple(r["span"]))
    for s in r.get("sessions", []):
        print("     세션 %4d봉  %s ~ %s" % (s["n"], s["from"], s["to"]))
    if r["status"] != "ok":
        print("  판정: %s" % r["status"]); return
    print("  spread  현재 %.4f · 평균 %.4f · 표준편차 %.4f · z %.2f"
          % (r["spread_now"], r["spread_mean"], r["spread_std"], r["z_full"]))
    hl = r.get("half_life_min")
    print("  AR(1) b=%.4f · half-life %s"
          % (r["ar1_b"], ("%.1f분" % hl) if hl else "산출 불가"))
    print("  ECM  γ=%.5f · t(OLS) %.2f · t(HAC10) %.2f  → %s"
          % (r["ecm_gamma"], r["ecm_t_ols"], r["ecm_t_hac10"],
             "유의" if abs(r["ecm_t_hac10"]) > 1.96 else "유의하지 않음"))
    if "adf_p" in r:
        print("  ADF(최장세션 %d봉, %s)  stat %.3f · p %.4f → %s"
              % (r["adf_n"], r["adf_session"], r["adf_stat"], r["adf_p"],
                 "정상성 채택" if r["adf_p"] < 0.05 else "기각 실패(아직 증명 못함)"))
        print("       1차 차분 p %.4g" % r["adf_diff_p"])
    else:
        print("  ADF: %s" % r.get("adf_note", "미산출"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, default=["KTB3", "KTB10"])
    ap.add_argument("--json", default="")
    ap.add_argument("--all", action="store_true", help="KTB3-KTB10 과 KTB10-ZN 둘 다")
    a = ap.parse_args()
    pairs = [("KTB3", "KTB10"), ("KTB10", "ZN")] if a.all else [tuple(a.pair)]
    out = []
    for x, y in pairs:
        r = analyse(x, y)
        show(r)
        out.append(r)
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
