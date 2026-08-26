# -*- coding: utf-8 -*-
"""Bond Futures Lab — 라이브/관리자 API + 대시보드 서빙 (로컬 전용).

왜 별도 서버인가: 대시보드 HTML 은 어디에나 복사되어 열릴 수 있지만(Render·
artifact·파일), **랩 데이터(minbars.db)는 이 PC 에만 있다.** 그래서 화면은
이 API 가 닿으면 라이브로, 못 닿으면 정적 스냅샷으로 동작한다(정직한 이중 모드).

  python tools/lab_api.py            → http://127.0.0.1:8010

엔드포인트
  GET  /                     대시보드(frontend/sim_dashboard.html)
  GET  /api/live             pair 별 지금 상태(spread·z·게이트) + 신선도
  GET  /api/health           데이터 건강(최근 reports/data_health.json)
  POST /api/health/refresh   점검만 다시 (API 호출 없음)
  POST /api/backfill         자동 backfill 실행 (백그라운드)
  GET  /api/jobs             최근 실행한 작업 상태
  GET  /api/collector        수집기·스케줄러 상태

읽기 전용 원칙: 주문·체결 관련 엔드포인트는 이 서버에 두지 않는다.
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import datetime as dt
import json
import math
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "tools"))
import econ_pair as ep  # noqa: E402

DB = ROOT / "data" / "minbars.db"
FRONT = ROOT / "frontend" / "sim_dashboard.html"
W = 120
MIN_W = 30
GAP_MIN = 60
GATE = -1.64
PY = _sys.executable

app = FastAPI(title="Bond Futures Lab API", version="1.0.0")
_JOBS: list[dict] = []
_LOCK = threading.Lock()

PAIR_DEFS = [
    ("ktb", "KTB3", "KTB10", "diff", "🇰🇷 KTB3 − KTB10", "pt"),
    ("kus", "KTB10", "ZN", "log", "🇰🇷🇺🇸 KTB10 − ZN", "×100"),
]


def _con():
    con = sqlite3.connect(DB, timeout=60)
    con.execute('PRAGMA busy_timeout=60000')
    return con


def _pair_live(con, key, a, b, mode, label, unit, tail=100000):
    """tail 은 전체 이력 기본 — ECM/half-life 를 페이지(빌더)와 같은 표본으로
    계산해 두 화면의 숫자가 어긋나지 않게 한다."""
    rows = list(con.execute(
        "SELECT x.bar_time, x.close, y.close FROM minbar x"
        " JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id=? AND y.instr_id=? ORDER BY x.bar_time DESC LIMIT ?",
        (a, b, tail)))[::-1]
    out = {"key": key, "label": label, "unit": unit, "legs": f"{a} − {b}",
           "n_bars": len(rows)}
    if len(rows) < MIN_W:
        out.update(status="insufficient", z_now=None, gate_pass=False)
        return out
    T = [dt.datetime.strptime(r[0], "%Y-%m-%d %H:%M") for r in rows]
    S = ([r[1] - r[2] for r in rows] if mode == "diff"
         else [100 * (math.log(r[1]) - math.log(r[2])) for r in rows])
    # 세션 분리
    segs, s0 = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            segs.append((s0, i)); s0 = i
    segs.append((s0, len(T)))
    # 마지막 세션 안에서 z
    a0, b0 = segs[-1]
    win = S[max(a0, len(S) - W):]
    z = None
    if len(win) >= MIN_W:
        m = sum(win) / len(win)
        sd = math.sqrt(sum((v - m) ** 2 for v in win) / len(win))
        if sd > 1e-12:
            z = (S[-1] - m) / sd
    # ECM (세션 내부 쌍만)
    lx, dif = [], []
    for a1, b1 in segs:
        for i in range(a1 + 1, b1):
            if (T[i] - T[i - 1]).total_seconds() <= 90:
                lx.append(S[i - 1]); dif.append(S[i] - S[i - 1])
    gamma = t_hac = half_life = None
    if len(lx) >= MIN_W:
        g = ep.ols(lx, dif)
        gamma, t_hac = g["b"], ep.hac_t(lx, dif, g, 10)
        ly = [lx[i] + dif[i] for i in range(len(lx))]
        f = ep.ols(lx, ly)
        if 0 < f["b"] < 1:
            half_life = math.log(2) / -math.log(f["b"])
    # IQR 게이트
    ss = sorted(S[-W:]) if len(S) >= MIN_W else sorted(S)
    q1, q3 = ss[len(ss) // 4], ss[(3 * len(ss)) // 4]
    iqr = q3 - q1
    gate_iqr = bool(iqr > 0 and not (q1 - 1.5 * iqr <= S[-1] <= q3 + 1.5 * iqr))
    gate_z = bool(z is not None and abs(z) >= 2.0)
    gate_ecm = bool(gamma is not None and gamma < 0 and t_hac is not None and t_hac <= GATE)
    action = "NONE"
    if gate_z and gate_iqr and gate_ecm:
        action = "SHORT spread" if z > 0 else "LONG spread"
    stale = int((dt.datetime.now() - T[-1]).total_seconds() // 60)
    out.update(status="ok", spread_now=round(S[-1], 4),
               z_now=(round(z, 3) if z is not None else None),
               gamma=(round(gamma, 5) if gamma is not None else None),
               t_hac=(round(t_hac, 2) if t_hac is not None else None),
               half_life_min=(round(half_life, 1) if half_life else None),
               gate_z=gate_z, gate_iqr=gate_iqr, gate_ecm=gate_ecm,
               gate_pass=gate_ecm, action=action,
               last_bar=T[-1].strftime("%Y-%m-%d %H:%M"), stale_min=stale,
               n_sessions=len(segs),
               spark=[round(v, 4) for v in S[-120:]],
               spark_z=([round(v, 2) for v in _z_series(S, a0)][-120:] if z is not None else []))
    return out


def _z_series(S, a0):
    out = []
    for i in range(len(S)):
        w = S[max(a0, i - W + 1):i + 1]
        if len(w) < MIN_W:
            out.append(0.0); continue
        m = sum(w) / len(w)
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / len(w))
        out.append((S[i] - m) / sd if sd > 1e-12 else 0.0)
    return out


@app.get("/")
def index():
    if FRONT.exists():
        return FileResponse(FRONT)
    return JSONResponse({"error": "sim_dashboard.html 없음 — build_lab_dashboard.py 실행"},
                        status_code=404)


@app.get("/api/live")
def api_live():
    con = _con()
    try:
        pairs = [_pair_live(con, *d) for d in PAIR_DEFS]
        last = con.execute("SELECT MAX(bar_time) FROM minbar").fetchone()[0]
    finally:
        con.close()
    fresh = None
    if last:
        fresh = int((dt.datetime.now()
                     - dt.datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() // 60)
    return {"asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_bar": last, "stale_min": fresh, "pairs": pairs}


@app.get("/api/health")
def api_health():
    p = ROOT / "reports" / "data_health.json"
    if not p.exists():
        return JSONResponse({"error": "data_health.json 없음 — /api/health/refresh 실행"},
                            status_code=404)
    return json.loads(p.read_text(encoding="utf-8"))


def _run_job(name: str, args: list[str]):
    job = {"name": name, "started": dt.datetime.now().strftime("%H:%M:%S"),
           "status": "running", "tail": ""}
    with _LOCK:
        _JOBS.insert(0, job)
        del _JOBS[8:]

    def work():
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900)
            job["status"] = "done" if r.returncode == 0 else "failed"
            job["tail"] = ((r.stdout or "") + (r.stderr or ""))[-1200:]
        except Exception as e:                                  # noqa: BLE001
            job["status"] = "failed"
            job["tail"] = str(e)
        job["ended"] = dt.datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=work, daemon=True).start()
    return job


@app.post("/api/backfill")
def api_backfill():
    return _run_job("backfill", [PY, str(ROOT / "tools" / "data_health.py"), "--os-pages", "4"])


@app.post("/api/health/refresh")
def api_health_refresh():
    return _run_job("health-check", [PY, str(ROOT / "tools" / "data_health.py"), "--check"])


@app.post("/api/rebuild")
def api_rebuild():
    return _run_job("dashboard-rebuild",
                    [PY, str(ROOT / "tools" / "build_lab_dashboard.py")])


@app.get("/api/jobs")
def api_jobs():
    with _LOCK:
        return {"jobs": list(_JOBS)}


@app.get("/api/collector")
def api_collector():
    con = _con()
    try:
        rows = [dict(ts_utc=r[0], instr_id=r[1], rows_in=r[2], rows_new=r[3], status=r[4])
                for r in con.execute(
                    "SELECT ts_utc,instr_id,rows_in,rows_new,status FROM collect_log "
                    "ORDER BY id DESC LIMIT 12")]
        last = con.execute("SELECT MAX(ts_utc) FROM collect_log").fetchone()[0]
        counts = {r[0]: r[1] for r in con.execute(
            "SELECT instr_id, COUNT(*) FROM minbar GROUP BY instr_id")}
        db_mb = DB.stat().st_size / 1e6 if DB.exists() else 0
    finally:
        con.close()
    age = None
    if last:
        age = int((dt.datetime.utcnow()
                   - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
    return {"last_run_utc": last, "last_run_age_min": age, "recent": rows,
            "bars_by_instrument": counts, "db_mb": round(db_mb, 2)}


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8010")))
