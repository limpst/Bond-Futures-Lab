# -*- coding: utf-8 -*-
"""KTB10-ZN 본 pair 준비도 — rolling β 추정과 FX 조정 게이트.

■ 왜 β 가 필요한가
  지금 화면의 spread 는 s = 100·(ln P_KTB10 − ln P_ZN) 로 **β=1 고정**이다.
  정식으로는 s = ln P¹ − β·ln P² 이고 β 는 "ZN 이 1% 움직일 때 KTB10 이 몇 %
  움직이나" 를 회귀로 추정한 헤지비율이다. β 를 1 로 두면 두 다리의 금리 민감도
  (DV01) 차이가 spread 에 그대로 남아, 평균회귀가 아니라 **추세**를 재게 된다.

■ 왜 지금까지 안 켰나 — 표본
  β 는 표본이 짧으면 노이즈다. 겹치는 봉이 BETA_MIN(기본 1,000) 을 넘을 때만
  켠다. 그 전에는 β=1 을 쓰되 화면에 "추정 대기 · 겹침 n/1,000" 이라고 적는다.
  이 문턱은 임의가 아니라 **β 의 rolling 표준편차가 진정되는지**를 같이 보고
  판단하라고 함께 출력한다.

■ FX (2026-08-26 갱신 — 소스 확보)
  KRW 투자자에게 ZN 다리의 손익은 USDKRW 에 노출된다. LS 로는 받을 수 없다:
  t8435(국내) gubun 10종에 통화선물 없음 · o3121(해외) 취급 12상품 중 FX 는
  CNH 뿐 · WebSocket 으로 6KU26·6KZ26·6EU26 직접 구독도 전부 무응답.
  그래서 **외부 소스**로 받는다 — tools/collect_fx.py (yfinance KRW=X 1분봉,
  실패 시 Alpha Vantage 스냅샷). 데이터는 instr_id='USDKRW' 로 따로 저장하고
  행마다 출처를 남긴다.
  ★ 남은 것은 '소스' 가 아니라 '적용' 이다 — 손익 환산식에 아직 넣지 않았다.

  python tools/beta_fx.py            준비도 계산 → reports/pair_readiness.json
  python tools/beta_fx.py --min 500  문턱 조정해서 보기
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
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
BETA_MIN = 1000          # 겹치는 봉이 이만큼 쌓여야 β 를 실제로 적용한다
BETA_WIN = 240           # rolling β 창 (분)
BETA_SD_MAX = 0.10       # 최근 rolling β 의 표준편차 상한 — 이보다 흔들리면 쓰지 않는다
GAP_MIN = 60             # 세션 경계


def _con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def load_pair(a="KTB10", b="ZN"):
    con = _con()
    try:
        rows = list(con.execute(
            "SELECT x.bar_time, x.close, y.close FROM minbar x"
            " JOIN minbar y ON x.bar_time=y.bar_time"
            " WHERE x.instr_id=? AND y.instr_id=? AND x.close>0 AND y.close>0"
            " ORDER BY x.bar_time", (a, b)))
    finally:
        con.close()
    T = [dt.datetime.strptime(r[0], "%Y-%m-%d %H:%M") for r in rows]
    return T, [math.log(r[1]) for r in rows], [math.log(r[2]) for r in rows]


def sessions(T):
    if not T:
        return []
    out, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            out.append((s, i)); s = i
    out.append((s, len(T)))
    return out


def ols_beta(x, y):
    """y = a + b·x — 여기서 x=ln P_ZN, y=ln P_KTB10 이므로 b 가 헤지비율 β."""
    n = len(x)
    if n < 10:
        return None
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    if vx < 1e-15:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / vx


def rolling_betas(T, lk, lz, win=BETA_WIN):
    """세션 안에서만 창을 잡아 β 를 굴린다 — 공백 너머를 '최근' 으로 보면 안 된다."""
    segs = sessions(T)
    out = []
    for a, b in segs:
        for i in range(a, b):
            lo = max(a, i - win + 1)
            if i - lo + 1 < 30:
                continue
            beta = ols_beta(lz[lo:i + 1], lk[lo:i + 1])
            if beta is not None:
                out.append((T[i], beta))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=BETA_MIN, help="β 적용 문턱(겹침 봉)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    T, lk, lz = load_pair()
    n = len(T)
    segs = sessions(T)
    rb = rolling_betas(T, lk, lz)
    betas = [b for _, b in rb]
    beta_full = ols_beta(lz, lk)
    beta_last = betas[-1] if betas else None
    beta_sd = None
    if len(betas) >= 10:
        m = sum(betas) / len(betas)
        beta_sd = math.sqrt(sum((v - m) ** 2 for v in betas) / len(betas))
    # 최근 60개 β 의 표준편차 — 값이 진정되고 있는지
    beta_sd_recent = None
    if len(betas) >= 60:
        w = betas[-60:]
        m = sum(w) / len(w)
        beta_sd_recent = math.sqrt(sum((v - m) ** 2 for v in w) / len(w))

    # 문턱은 두 개다 — 표본 수 **그리고** 추정치의 안정성.
    # 표본만 보고 켜면 β 가 0.9~1.9 를 오가는 상태에서 그중 한 값을 집어
    # 쓰게 된다(2026-08-26 실측 sd 0.955). 그건 헤지가 아니라 도박이다.
    enough = n >= a.min
    stable = (beta_sd_recent is not None and beta_sd_recent <= BETA_SD_MAX)
    ready = bool(enough and stable)
    rate = None
    if len(T) >= 2:
        span_min = (T[-1] - T[0]).total_seconds() / 60
        # 세션 공백을 뺀 실제 수집 분
        active = sum((T[b - 1] - T[a0]).total_seconds() / 60 for a0, b in segs) or 1
        rate = round(n / active, 2)          # 활성 1분당 겹침 봉
    eta_min = None
    if rate and rate > 0 and not enough:          # 표본이 이미 찼으면 ETA 는 뜻이 없다
        eta_min = int((a.min - n) / rate)

    # FX 실측 — 소스가 실제로 DB 에 있나, pair 와 얼마나 겹치나
    _c = _con()
    try:
        fx_n, fx_lo, fx_hi = _c.execute(
            "SELECT COUNT(*), MIN(bar_time), MAX(bar_time) FROM minbar WHERE instr_id='USDKRW'"
        ).fetchone()
        fx_ov = _c.execute(
            "SELECT COUNT(*) FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
            " JOIN minbar z ON z.bar_time=x.bar_time"
            " WHERE x.instr_id='KTB10' AND y.instr_id='ZN' AND z.instr_id='USDKRW'"
        ).fetchone()[0]
        fx_src = [r[0] for r in _c.execute(
            "SELECT DISTINCT symbol FROM minbar WHERE instr_id='USDKRW' LIMIT 3")]
    finally:
        _c.close()
    fx_status = {
        "have_data": bool(fx_n),
        "applied": False,
        "bars": fx_n, "span": [fx_lo, fx_hi], "sources": fx_src,
        "triple_overlap": fx_ov,
        "data_detail": (f"{fx_n:,}봉 ({fx_lo} ~ {fx_hi}) · pair 와 3중 겹침 {fx_ov:,}분 · "
                        f"출처 {', '.join(fx_src)}" if fx_n else
                        "USDKRW 봉 없음 — tools/collect_fx.py 실행 필요"),
        "reason": ("데이터는 있으나 손익 환산식에 아직 적용하지 않음 — "
                   "신호(금리 상대가치)는 FX 없이도 성립하지만 손익은 FX 없이 틀린다"
                   if fx_n else "소스 미확보"),
    }

    rep = {
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pair": "KTB10-ZN",
        "overlap_bars": n, "threshold": a.min, "ready": ready,
        "enough_bars": enough, "beta_stable": stable, "beta_sd_max": BETA_SD_MAX,
        "sessions": len(segs),
        "span": [T[0].strftime("%Y-%m-%d %H:%M"), T[-1].strftime("%Y-%m-%d %H:%M")] if T else None,
        "bars_per_active_min": rate,
        "eta_min_to_threshold": eta_min,
        "beta_applied": (round(beta_last, 4) if (ready and beta_last) else 1.0),
        "beta_mode": ("rolling OLS" if ready else
                      ("고정 1.0 (β 불안정 — sd %.3f > %.2f)" % (beta_sd_recent, BETA_SD_MAX)
                       if (enough and beta_sd_recent) else "고정 1.0 (표본 부족)")),
        "beta_full_sample": (round(beta_full, 4) if beta_full else None),
        "beta_rolling_last": (round(beta_last, 4) if beta_last else None),
        "beta_rolling_sd": (round(beta_sd, 4) if beta_sd else None),
        "beta_rolling_sd_recent60": (round(beta_sd_recent, 4) if beta_sd_recent else None),
        "beta_window_min": BETA_WIN,
        "fx": fx_status,
        "checklist": [
            {"item": "겹치는 봉 ≥ %d" % a.min, "ok": enough,
             "detail": f"{n} / {a.min}" + (f" · 예상 {eta_min}분 남음" if eta_min else "")},
            {"item": "rolling β 안정성 (최근60 sd ≤ %.2f)" % BETA_SD_MAX, "ok": stable,
             "detail": (("sd %.3f — %s" % (beta_sd_recent,
                         "안정, β 적용" if stable else "아직 노이즈, β=1 유지"))
                        if beta_sd_recent is not None else "창이 덜 참")},
            {"item": "rolling β 값", "ok": bool(ready and beta_last),
             "detail": ("β=%.3f (창 %d분)" % (beta_last, BETA_WIN) if beta_last
                        else "산출 불가") + (" · 전체표본 β=%.3f" % beta_full if beta_full else "")},
            {"item": "FX(USDKRW) 데이터", "ok": fx_status["have_data"],
             "detail": fx_status["data_detail"]},
            {"item": "FX 손익 환산 적용", "ok": fx_status["applied"],
             "detail": fx_status["reason"]},
            {"item": "duration(DV01) 매칭", "ok": False,
             "detail": "β 가 대리하지만 정식 DV01 매칭은 미구현"},
        ],
    }
    out = ROOT / "reports" / "pair_readiness.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    print(f"[readiness] {rep['pair']} 겹침 {n}/{a.min} · 세션 {len(segs)} · "
          f"{'🟢 준비됨' if ready else '🟠 축적 중'}")
    if eta_min:
        print(f"  현재 속도 {rate}봉/활성분 → 문턱까지 약 {eta_min}분")
    print(f"  β  전체표본 {rep['beta_full_sample']} · rolling 최신 {rep['beta_rolling_last']} "
          f"(sd {rep['beta_rolling_sd']}, 최근60 sd {rep['beta_rolling_sd_recent60']})")
    print(f"  적용 중: β={rep['beta_applied']} ({rep['beta_mode']})")
    for c in rep["checklist"]:
        print(f"   {'🟢' if c['ok'] else '🟠'} {c['item']}: {c['detail']}")
    print(f"  → {out.name} 저장")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
