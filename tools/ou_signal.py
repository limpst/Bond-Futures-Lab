# -*- coding: utf-8 -*-
"""실시간 델타원 시그널 엔진 — unit 을 맞춘 pair · OU 적합 · 5~15분 주기.

■ 왜 '단순 차이' 가 아니라 unit 을 맞추나 (이 파일의 존재 이유)

  KTB10 106.04 − ZN 108.73 = −2.69.  이 −2.69 는 **아무 뜻이 없는 숫자**다.
  KTB10 의 1pt 는 1,000,000원이고 ZN 의 1pt 는 1,000달러다. 단위가 다른 두
  숫자를 뺀 것이라, 이 값이 0.1 움직였을 때 통장에서 얼마가 오가는지 말할 수
  없다. 말할 수 없으면 **비용과 비교할 수 없고**, 비교할 수 없으면 진입
  임계(몇 σ에서 들어갈까)를 정할 근거가 없다.

  그래서 세 단계로 맞춘다.

    1) 계약 가치   가격 × 1pt 가치 → 그 다리 1계약이 지금 얼마짜리인가
    2) 통화        USD 다리는 USDKRW 로 환산 → 두 다리를 같은 화폐로
    3) 위험(헤지비) ΔA = a + b·ΔB 회귀로 b 를 구해 B 다리를 b 배 잡는다.
                   만기가 다르면 금리 1bp 에 움직이는 금액(DV01)이 다르다.
                   1:1 로 빼면 남는 건 스프레드가 아니라 **금리 방향 노출**이다.
                   듀레이션 가정 없이 데이터가 b 를 정하게 한다(세션 내 차분).

  결과 spread = A_KRW − b·B_KRW 는 **원화 손익 단위**다. 이 값이 50,000 움직이면
  실제로 5만원이 움직인다. 그제야 z-score 도, 비용 대비 기대수익도 뜻이 생긴다.

  ★ 정직: 환헤지는 하지 않는다. 그래서 USDKRW 변동이 스프레드에 섞인다.
    그 크기(fx_share)를 같이 보고한다.

■ 무엇을 계산하나
    OU 적합    세션 안 AR(1) → κ · half-life · μ · σ_eq
    ECM        Δs = α + γ·s(t−1) → γ, Newey-West t (게이트)
    z-score    (s − μ_W)/σ_W, 창 W 봉, **세션 내부에서만**
    자물쇠 3개 ① |z| ≥ 2  ② IQR 밖  ③ t_HAC ≤ −1.64
    기대손익    되돌림 시 기대 이익(원) vs 왕복 비용(원) — 넘어야 진입 후보

■ 왜 5~15분 주기인가
    1분마다 다시 적합하면 추정치가 tick noise 로 떨린다. 반대로 하루 한 번이면
    장중 상태 변화를 놓친다. half-life 가 수십 분~수 시간대로 나오므로 그보다
    한 단계 짧은 5~15분이 맞다. 기본 10분.

  python tools/ou_signal.py                  1회 계산 후 JSON 저장
  python tools/ou_signal.py --every 10       10분 주기로 계속
  python tools/ou_signal.py --window 240     z 창 바꾸기
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
import itertools
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ls_openapi import ROOT as _R  # noqa: F401,E402  (경로 일관성 확인용)

DB = ROOT / "data" / "minbars.db"
OUT = ROOT / "reports" / "ou_signal.json"
HIST = ROOT / "reports" / "ou_signal_history.jsonl"

GAP_MIN = 60            # 이보다 긴 공백은 세션 경계
W_DEFAULT = 120         # z 창 (분)
MIN_W = 30              # 창이 이만큼 차야 z 를 낸다
MIN_PAIRS = 60          # 연속쌍이 이만큼은 있어야 적합
GATE_T = -1.64
Z_ENTRY = 2.0
Z_EXIT = 0.25

# 1 포인트가 얼마인가 (계약 통화 기준) — pair_hedged.py 와 같은 표
PT = {"ZT": 2000.0, "ZF": 1000.0, "ZN": 1000.0, "TN": 1000.0, "ZB": 1000.0,
      "KTB3": 1_000_000.0, "KTB10": 1_000_000.0, "KTB30": 1_000_000.0}
CCY = {"ZT": "USD", "ZF": "USD", "ZN": "USD", "TN": "USD", "ZB": "USD",
       "KTB3": "KRW", "KTB10": "KRW", "KTB30": "KRW"}
COMMISSION_KRW = 1_000.0        # 계약당 편도 수수료 가정 (사용자 확인 2026-08-25)
LABEL = {"KTB3": "한국 3년", "KTB10": "한국 10년", "KTB30": "한국 30년",
         "ZT": "미국 2년", "ZF": "미국 5년", "ZN": "미국 10년",
         "TN": "미국 Ultra10", "ZB": "미국 30년"}


# ── 자료 ─────────────────────────────────────────────────────────────────
def _ro_connect() -> sqlite3.Connection:
    """읽기 전용 커넥션 — 수집기의 쓰기 락에 걸리지도, 걸리게 하지도 않는다."""
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def load_fx(con) -> dict[str, float]:
    return {r[0]: r[1] for r in con.execute(
        "SELECT bar_time, close FROM minbar WHERE instr_id='USDKRW'")}


def load_pair(con, a: str, b: str, fx: dict[str, float]):
    """같은 분의 두 다리를 **원화 계약가치**로 바꿔 읽는다. FX 는 캐리포워드."""
    rows = list(con.execute(
        "SELECT x.bar_time, x.close, y.close FROM minbar x"
        " JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id=? AND y.instr_id=? ORDER BY x.bar_time", (a, b)))
    T, VA, VB, used_fx = [], [], [], []
    last_fx = None
    for bt, pa, pb in rows:
        f = fx.get(bt, last_fx)
        if f:
            last_fx = f
        need_fx = CCY[a] == "USD" or CCY[b] == "USD"
        if need_fx and not last_fx:
            continue                        # 환율을 모르면 그 봉은 버린다
        ka = pa * PT[a] * (last_fx if CCY[a] == "USD" else 1.0)
        kb = pb * PT[b] * (last_fx if CCY[b] == "USD" else 1.0)
        T.append(dt.datetime.strptime(bt, "%Y-%m-%d %H:%M"))
        VA.append(ka); VB.append(kb); used_fx.append(last_fx or 0.0)
    return T, VA, VB, used_fx


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


# ── 통계 ─────────────────────────────────────────────────────────────────
def ols(x, y):
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
    se = math.sqrt(s2 / sxx)
    return dict(a=a, b=b, se=se, t=(b / se if se > 0 else float("nan")), n=n, res=res)


def hac_t(x, fit, maxlags=10):
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
    return fit["b"] / math.sqrt(var) if var > 0 else float("nan")


def quantile(v, q):
    if not v:
        return 0.0
    s = sorted(v); i = q * (len(s) - 1)
    lo = int(math.floor(i)); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def roll_spread(diffs):
    """Roll(1984) 유효 스프레드 — 체결가만으로 추정한 왕복 호가차(원)."""
    if len(diffs) < 30:
        return None
    n = len(diffs) - 1
    m = sum(diffs) / len(diffs)
    cov = sum((diffs[i] - m) * (diffs[i - 1] - m) for i in range(1, len(diffs))) / n
    return 2 * math.sqrt(-cov) if cov < 0 else None


def rolling_z(S, segs, W):
    Z = [None] * len(S)
    for s, e in segs:
        for i in range(s, e):
            v = S[max(s, i - W + 1):i + 1]
            if len(v) < MIN_W:
                continue
            m = sum(v) / len(v)
            sd = math.sqrt(sum((q - m) ** 2 for q in v) / len(v))
            if sd > 1e-9:
                Z[i] = (S[i] - m) / sd
    return Z


# ── pair 분석 ────────────────────────────────────────────────────────────
def analyse(con, a, b, fx, W):
    T, VA, VB, used_fx = load_pair(con, a, b, fx)
    P = {"pair": "%s-%s" % (a, b), "legs": [a, b],
         "label": "%s − %s" % (LABEL.get(a, a), LABEL.get(b, b)),
         "n_bars": len(T), "ccy": [CCY[a], CCY[b]],
         "cross_ccy": CCY[a] != CCY[b]}
    if len(T) < MIN_W * 2:
        P["status"] = "표본 부족"
        return P
    segs = sessions(T)

    # ① 헤지비 b — 세션 안 차분 회귀 (듀레이션 가정 없이 데이터가 정한다)
    dA, dB = [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                dA.append(VA[i] - VA[i - 1]); dB.append(VB[i] - VB[i - 1])
    hedge = ols(dB, dA) if len(dA) >= MIN_PAIRS else None
    if not hedge or not (0.01 < abs(hedge["b"]) < 100):
        P["status"] = "헤지비 추정 실패"
        return P
    hb = hedge["b"]
    P["hedge_b"] = hb
    P["hedge_b_se"] = hedge["se"]
    P["hedge_r2_note"] = "ΔA = a + b·ΔB (세션 내 차분, %d쌍)" % hedge["n"]

    # ② 원화 손익 단위 spread
    S = [VA[i] - hb * VB[i] for i in range(len(T))]

    # ③ OU / ECM — 세션 안 쌍만
    lx, ly, dif = [], [], []
    for s, e in segs:
        for i in range(s + 1, e):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                lx.append(S[i - 1]); ly.append(S[i]); dif.append(S[i] - S[i - 1])
    if len(lx) < MIN_PAIRS:
        P["status"] = "연속쌍 부족"
        return P
    f = ols(lx, ly); g = ols(lx, dif)
    ar1 = f["b"]
    hl = (math.log(2) / -math.log(ar1)) if 0 < ar1 < 1 else None
    kappa = (-math.log(ar1)) if 0 < ar1 < 1 else None
    resid_sd = math.sqrt(sum(r * r for r in f["res"]) / max(1, len(f["res"]) - 2))
    sigma_eq = (resid_sd / math.sqrt(1 - ar1 ** 2)) if 0 < ar1 < 1 else None
    thac = hac_t(lx, g, 10)

    # ④ z / IQR
    Z = rolling_z(S, segs, W)
    z_now = next((Z[i] for i in range(len(Z) - 1, -1, -1) if Z[i] is not None), None)
    q1, q3 = quantile(S, .25), quantile(S, .75)
    iqr = q3 - q1
    out_iqr = bool(iqr > 0 and (S[-1] < q1 - 1.5 * iqr or S[-1] > q3 + 1.5 * iqr))

    # ⑤ 비용 (원) — Roll 유효 스프레드 + 양다리 왕복 수수료
    rs = roll_spread(dif)
    half_spread = (rs / 2) if rs else (abs(S[-1]) * 1e-4)
    legs_round_trip = COMMISSION_KRW * 2 * 2          # 다리2 × (진입+청산)
    cost_krw = half_spread * 2 + legs_round_trip      # 진입·청산 각 half-spread

    # ⑥ 자물쇠 3개 + 기대손익
    mu_w = sum(S[-W:]) / len(S[-W:])
    edge_krw = abs(S[-1] - mu_w)                      # 되돌림 시 기대 이익(원)
    locks = {
        "z": bool(z_now is not None and abs(z_now) >= Z_ENTRY),
        "iqr": out_iqr,
        "ecm": bool(g["b"] < 0 and thac <= GATE_T),
    }
    locks["edge"] = bool(edge_krw > cost_krw)
    ready = all(locks.values())
    side = None
    if z_now is not None and abs(z_now) >= Z_ENTRY:
        side = "SHORT spread" if z_now > 0 else "LONG spread"

    P.update(status="ok",
             n_sessions=len(segs), n_pairs=len(lx),
             span=[T[0].strftime("%m-%d %H:%M"), T[-1].strftime("%m-%d %H:%M")],
             spread_now=S[-1], spread_mean=sum(S) / len(S),
             spread_std=math.sqrt(sum((v - sum(S) / len(S)) ** 2 for v in S) / len(S)),
             z_now=z_now, ar1=ar1, kappa=kappa, half_life_min=hl,
             sigma_eq=sigma_eq, mu=(f["a"] / (1 - ar1)) if 0 < ar1 < 1 else None,
             ecm_gamma=g["b"], ecm_t_ols=g["t"], ecm_t_hac=thac,
             iqr_lo=q1 - 1.5 * iqr, iqr_hi=q3 + 1.5 * iqr,
             roll_spread_krw=rs, cost_krw=cost_krw, edge_krw=edge_krw,
             locks=locks, ready=ready, side=side,
             fx_used=(used_fx[-1] if used_fx else None),
             # 마지막 봉이 얼마나 오래됐나 — 멈춘 수집기의 옛 봉으로 z 가 튀는 것을
             # 화면에서 걸러내기 위해 같이 내보낸다 (2026-08-27: TN 이 4시간 정지 중
             # 이었는데 z=+8 로 1위에 올라왔다).
             stale_min=(dt.datetime.now() - T[-1]).total_seconds() / 60)

    # 차트용 — z 가 나온 구간만, 최근 600점
    idx = [i for i in range(len(S)) if Z[i] is not None][-600:]
    if len(idx) >= 2:
        P["chart"] = {"t": [T[i].strftime("%m-%d %H:%M") for i in idx],
                      "z": [round(Z[i], 2) for i in idx],
                      "s": [round(S[i]) for i in idx]}
    return P


def discover(con, min_overlap=200):
    """겹치는 봉이 충분한 pair 만 자동으로 고른다."""
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT instr_id FROM minbar WHERE instr_id IN (%s)"
        % ",".join("'%s'" % k for k in PT))]
    order = [k for k in ("KTB3", "KTB10", "KTB30", "ZT", "ZF", "ZN", "TN", "ZB")
             if k in ids]
    out = []
    for a, b in itertools.combinations(order, 2):
        n = con.execute("SELECT COUNT(*) FROM minbar x JOIN minbar y"
                        " ON x.bar_time=y.bar_time WHERE x.instr_id=? AND y.instr_id=?",
                        (a, b)).fetchone()[0]
        if n >= min_overlap:
            out.append((a, b, n))
    return out


def run_once(W, min_overlap, quiet=False):
    con = _ro_connect()
    fx = load_fx(con)
    pairs = discover(con, min_overlap)
    res = []
    for a, b, _n in pairs:
        try:
            res.append(analyse(con, a, b, fx, W))
        except Exception as e:                       # 한 pair 가 죽어도 나머지는 간다
            res.append({"pair": "%s-%s" % (a, b), "status": "오류: %s" % str(e)[:80]})
    con.close()
    ok = [r for r in res if r.get("status") == "ok"]
    ok.sort(key=lambda r: -abs(r.get("z_now") or 0))
    rest = [r for r in res if r.get("status") != "ok"]
    payload = {"asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "window": W, "gate_t": GATE_T, "z_entry": Z_ENTRY, "z_exit": Z_EXIT,
               "n_pairs": len(res), "pairs": ok + rest}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as fh:
        for r in ok:
            fh.write(json.dumps({k: r.get(k) for k in
                                 ("pair", "z_now", "half_life_min", "ecm_t_hac",
                                  "hedge_b", "spread_now", "ready")}
                                | {"ts": payload["asof"]}, ensure_ascii=False) + "\n")
    if not quiet:
        print("[%s] pair %d개 · 창 %d봉" % (payload["asof"], len(res), W))
        print("  %-14s %-22s %11s %8s %10s %8s %7s %s"
              % ("pair", "구성", "편차(원)", "z", "half-life", "t_HAC", "지연", "자물쇠"))
        for r in ok:
            lk = "".join("●" if r["locks"][k] else "○" for k in ("z", "iqr", "ecm", "edge"))
            st = r.get("stale_min") or 0
            print("  %-14s %-22s %11s %8s %10s %8s %7s %s %s"
                  % (r["pair"], r["label"], format(int(r["edge_krw"]), ","),
                     "%+.2f" % (r["z_now"] or 0),
                     ("%.0f분" % r["half_life_min"]) if r["half_life_min"] else "—",
                     "%.2f" % r["ecm_t_hac"],
                     ("%.0f분" % st) + ("!" if st > 15 else ""), lk,
                     ("← " + r["side"]) if r["ready"] else ""))
        for r in rest:
            print("  %-14s %s" % (r["pair"], r["status"]))
        print("  자물쇠 순서: z · IQR · ECM · 비용초과   (● 열림 / ○ 닫힘)")
        print("  wrote %s" % OUT)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=W_DEFAULT, help="z 창 (봉)")
    ap.add_argument("--every", type=int, default=0,
                    help="분 주기로 반복 (5~15 권장, 0=1회)")
    ap.add_argument("--min-overlap", type=int, default=200)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if not a.every:
        return run_once(a.window, a.min_overlap, a.quiet)
    every = max(1, min(60, a.every))
    print("주기 실행 %d분 — Ctrl+C 로 중단" % every)
    while True:
        try:
            run_once(a.window, a.min_overlap, a.quiet)
        except Exception as e:
            print("  회차 실패: %s" % str(e)[:150])
        time.sleep(every * 60)


if __name__ == "__main__":
    sys.exit(main())
