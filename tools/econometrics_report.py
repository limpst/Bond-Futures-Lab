# -*- coding: utf-8 -*-
"""델타원 spread 계량 분석 — 기술 보고서용 (2026-08-24).

실측 1분봉(minbars.db)에서 KTB3–KTB10 spread 에 대해:
  ADF 정상성 · OU 적합(mean-reversion 속도·half-life) · ECM ·
  microstructure(Roll spread·Amihud·signed-volume OFI proxy) ·
  ACD 유형 duration 통계 · z-score 진입 threshold sweep 시뮬레이션.
출력: JSON (reports/econ_20260824.json) — 보고서 본문이 이 수치를 인용.
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd
import sqlite3
from statsmodels.tsa.stattools import adfuller
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "minbars.db")

bars = pd.read_sql("SELECT symbol, bar_time, open, high, low, close, volume FROM minbar", con)
inst = pd.read_sql("SELECT * FROM instrument", con)
scan = pd.read_sql("SELECT name, exch, symbol, volume, passed FROM universe_scan ORDER BY volume DESC", con)
print("symbols:", bars["symbol"].unique(), "| rows:", len(bars))

bars["bar_time"] = pd.to_datetime(bars["bar_time"])
PAIR = ("A6569000", "A6769000")   # KTB3, KTB10 — CME 심볼 유입과 무관하게 고정
bars = bars[bars["symbol"].isin(PAIR)]
px = bars.pivot_table(index="bar_time", columns="symbol", values="close").dropna()
vol = bars.pivot_table(index="bar_time", columns="symbol", values="volume").reindex(px.index)
syms = list(px.columns)
s3, s10 = syms[0], syms[1]  # KTB3, KTB10 (확인 출력)
print("aligned bars:", len(px), "| cols:", syms, "|", px.index.min(), "->", px.index.max())

spread = (px[s3] - px[s10]).rename("spread")
if len(px) < 100 or float(spread.std()) < 1e-9:
    print(f"[skip] insufficient/constant data (n={len(px)}) - no report")
    sys.exit(0)
out = {"n_bars_raw": int(len(bars)), "n_aligned": int(len(px)),
       "span": [str(px.index.min()), str(px.index.max())],
       "symbols": syms,
       "spread_now": round(float(spread.iloc[-1]), 3),
       "spread_mean": round(float(spread.mean()), 3),
       "spread_std": round(float(spread.std()), 4)}

# ── 1. ADF ───────────────────────────────────────────────────────────────
for name, series in [("level", spread), ("diff", spread.diff().dropna())]:
    stat, p, lags, nobs, crit, _ = adfuller(series, autolag="AIC")
    out[f"adf_{name}"] = {"stat": round(stat, 3), "p": round(p, 4), "lags": int(lags),
                          "nobs": int(nobs), "crit5": round(crit["5%"], 3)}
print("ADF:", out["adf_level"], out["adf_diff"])

# ── 2. OU 적합 (AR(1) → 연속시간 매핑, Δt = 1분봉) ──────────────────────
s_lag = spread.shift(1).dropna()
s_cur = spread.iloc[1:]
X = sm.add_constant(s_lag.values)
ar1 = sm.OLS(s_cur.values, X).fit()
a, b = ar1.params
kappa_bar = -np.log(b) if 0 < b < 1 else np.nan          # per bar
half_life_bar = np.log(2) / kappa_bar if kappa_bar and kappa_bar > 0 else np.nan
mu = a / (1 - b) if b != 1 else np.nan
resid_sd = float(np.std(ar1.resid, ddof=2))
sigma_ou = resid_sd * np.sqrt(2 * kappa_bar / (1 - b**2)) if kappa_bar and kappa_bar > 0 else np.nan
out["ou"] = {"b_ar1": round(float(b), 5), "b_se": round(float(ar1.bse[1]), 5),
             "kappa_per_bar": round(float(kappa_bar), 5),
             "half_life_bars": round(float(half_life_bar), 1),
             "half_life_hours": round(float(half_life_bar) / 60, 2),
             "mu": round(float(mu), 3), "sigma_ou_per_bar": round(float(sigma_ou), 5),
             "resid_sd": round(resid_sd, 5)}
print("OU:", out["ou"])

# ── 3. ECM: Δs = α + γ·s(t-1) ────────────────────────────────────────────
ds = spread.diff().dropna()
Xe = sm.add_constant(s_lag.values)
ecm = sm.OLS(ds.values, Xe).fit()
# HAC(Newey-West) t 도 병기
ecm_hac = sm.OLS(ds.values, Xe).fit(cov_type="HAC", cov_kwds={"maxlags": 10})
out["ecm"] = {"gamma": round(float(ecm.params[1]), 5),
              "t_ols": round(float(ecm.tvalues[1]), 2),
              "t_hac10": round(float(ecm_hac.tvalues[1]), 2)}
print("ECM:", out["ecm"])

# ── 4. Microstructure (1분봉으로 가능한 범위) ────────────────────────────
ms = {}
for s in syms:
    dp = px[s].diff().dropna()
    cov1 = float(np.cov(dp[1:], dp[:-1])[0, 1])
    roll = 2 * np.sqrt(-cov1) if cov1 < 0 else None       # Roll(1984) 유효 스프레드
    ret = px[s].pct_change().abs()
    v = vol[s].replace(0, np.nan)
    amihud = float((ret / v).mean() * 1e6)                 # |ret|/volume ×1e6
    sgn = np.sign(px[s].diff()).fillna(0)
    ofi = (sgn * vol[s]).fillna(0)                         # signed-volume OFI proxy
    ofi_ratio = float(ofi.sum() / vol[s].sum())            # 순매수 방향성 비율
    ofi_ac1 = float(ofi.autocorr(1))
    ms[s] = {"roll_spread_pts": round(roll, 4) if roll else None,
             "cov1": round(cov1, 6),
             "amihud_x1e6": round(amihud, 3),
             "ofi_ratio": round(ofi_ratio, 3), "ofi_ac1": round(ofi_ac1, 3),
             "vol_total": int(vol[s].sum()), "vol_bar_mean": round(float(vol[s].mean()), 1)}
out["micro"] = ms
print("micro:", ms)

# ── 5. z-score + duration(ACD 유형) + threshold sweep 시뮬레이션 ─────────
W = 120  # rolling window (2시간)
mu_r = spread.rolling(W).mean()
sd_r = spread.rolling(W).std()
z = ((spread - mu_r) / sd_r).dropna()
out["z_now"] = round(float(z.iloc[-1]), 2)
out["z_window"] = W

# duration: |z|>=1.0 진입 이벤트 간 간격 (분)
ev = z[np.abs(z) >= 1.0]
if len(ev) > 1:
    gaps = np.diff(ev.index.values).astype("timedelta64[m]").astype(float)
    gaps = gaps[gaps > 1]  # 연속봉 제거
    out["acd"] = {"n_events": int(len(ev)), "n_gaps": int(len(gaps)),
                  "dur_mean_min": round(float(np.mean(gaps)), 1) if len(gaps) else None,
                  "dur_median_min": round(float(np.median(gaps)), 1) if len(gaps) else None,
                  "dur_cv": round(float(np.std(gaps) / np.mean(gaps)), 2) if len(gaps) else None}
else:
    out["acd"] = {"n_events": int(len(ev))}
print("ACD:", out["acd"])

# threshold sweep: 진입 |z|>=th → 청산 |z|<=0.25, 반대 다리, 1 spread 단위
sweep = []
for th in (1.0, 1.5, 2.0):
    pos, entry_px, trades, pnl = 0, 0.0, 0, 0.0
    for t_i in z.index:
        zi = z.loc[t_i]
        si = spread.loc[t_i]
        if pos == 0 and abs(zi) >= th:
            pos = -1 if zi > 0 else 1
            entry_px = si
        elif pos != 0 and abs(zi) <= 0.25:
            pnl += pos * (si - entry_px)
            trades += 1
            pos = 0
    if pos != 0:  # 미청산 포지션 mark-to-market
        pnl += pos * (spread.iloc[-1] - entry_px)
    sweep.append({"th": th, "trades": trades, "open_pos": pos,
                  "pnl_pts": round(pnl, 3),
                  "pnl_krw_1lot": int(pnl * 1_000_000)})   # 1pt = 100만원 (액면 1억)
out["sweep"] = sweep
print("sweep:", sweep)

out["universe_scan"] = scan.head(12).to_dict("records")
out["instruments"] = inst[["market", "name", "symbol", "active"]].to_dict("records")

rep = ROOT / "reports"
rep.mkdir(exist_ok=True)
(rep / "econ_20260824.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("saved reports/econ_20260824.json")


# -- daily 모드: 장 마감 후 하루 1행 JSONL 추가 (reports/econ_daily.jsonl) --
def daily_append():
    import datetime as _dt
    logp = rep / "econ_daily.jsonl"
    today = _dt.date.today().isoformat()
    if logp.exists():
        last = [l for l in logp.read_text(encoding="utf-8").splitlines() if l.strip()]
        if last and json.loads(last[-1]).get("date") == today:
            print("[daily] already logged for", today)
            return
    if _dt.datetime.now().hour < 15:
        print("[daily] before close - skip")
        return
    row = {"date": today, "n_aligned": out["n_aligned"],
           "adf_p": out["adf_level"]["p"],
           "half_life_min": out["ou"]["half_life_bars"],
           "ecm_gamma": out["ecm"]["gamma"],
           "t_ols": out["ecm"]["t_ols"], "t_hac10": out["ecm"]["t_hac10"],
           "spread_now": out["spread_now"], "z_now": out["z_now"]}
    with open(logp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("[daily] appended:", row)


if "--daily" in sys.argv:
    daily_append()
