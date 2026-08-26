# -*- coding: utf-8 -*-
"""Bond Futures Lab — 단일 진입점.

데스크톱에서 `git clone` 후 `python main.py` 하나로 돌아가게 만든 것이다.
Windows 작업 스케줄러를 5개씩 걸 필요가 없다 — 이 프로세스가 수집기를
스레드로 띄우고, 죽으면 스스로 되살린다.

  python main.py                 수집기 전부 + 웹 대시보드 (기본)
  python main.py collect         수집기만
  python main.py serve           웹 대시보드만
  python main.py report          분석 리포트 한 번 내고 종료
  python main.py status          지금 무엇이 얼마나 쌓였는지

세션별 경로 (2026-08-25 실측 — 이 갈림이 이 프로그램 구조의 근거다):
  KRX 주간 09:00~15:45  WebSocket FC9/FH9   (REST 는 0봉)
  KRX 야간 18:00~05:00  REST t8461 폴링      (WebSocket 은 0건)
  CME      07:05~06:00  WebSocket OVC        (REST 는 아무것도 안 준다)

준비물:
  .env.ls            LS Open API 자격증명 (gitignore 대상 — 저장소에 없다)
  pip install websockets requests
  (선택) statsmodels — ADF 검정에 필요. 없으면 그 항목만 건너뛴다.
"""
from __future__ import annotations

import sys as _sys
# 작업 스케줄러·서비스 콘솔은 cp949 라 '—' 같은 문자에서 죽는다.
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import datetime as dt
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
DB = ROOT / "data" / "minbars.db"
PY = _sys.executable

# (이름, 인자, 언제 도는가) — when 은 지금 돌아야 하는지 판정하는 함수
KST = dt.timezone(dt.timedelta(hours=9))


def now_kst() -> dt.datetime:
    return dt.datetime.now()


def in_window(t: dt.time, a: dt.time, b: dt.time) -> bool:
    return (a <= t <= b) if a <= b else (t >= a or t <= b)


JOBS = [
    dict(name="KTB 주간 실시간", script="collect_kr_ws.py", args=["--minutes", "20"],
         when=lambda n: n.weekday() < 5 and in_window(n.time(), dt.time(8, 55), dt.time(15, 50))),
    dict(name="KTB 야간 REST", script="collect_minbars.py", args=["--live", "--count", "900"],
         when=lambda n: in_window(n.time(), dt.time(18, 0), dt.time(5, 5)), every=300),
    dict(name="CME 실시간", script="collect_cme_ws.py", args=["--minutes", "20"],
         when=lambda n: not in_window(n.time(), dt.time(6, 0), dt.time(7, 5))),
    dict(name="pair 모니터", script="monitor_pairs.py", args=[], when=lambda n: True, every=1800),
    dict(name="수집 감시", script="watch_collect.py", args=["--quiet"], when=lambda n: True, every=900),
]


def ensure_wal():
    if not DB.is_file():
        print("[경고] %s 가 없습니다. collect_minbars.py --init-db 로 먼저 만드십시오." % DB)
        return
    try:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.close()
    except Exception as e:
        print("[경고] WAL 전환 실패: %s" % str(e)[:80])


def run_job(job, stop: threading.Event):
    """한 수집기를 창(window) 안에서만, 죽으면 되살리며 돌린다."""
    every = job.get("every")
    last = 0.0
    while not stop.is_set():
        n = now_kst()
        if not job["when"](n):
            stop.wait(60); continue
        if every and time.time() - last < every:
            stop.wait(min(30, every)); continue
        last = time.time()
        cmd = [PY, "-u", str(TOOLS / job["script"])] + job["args"]
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3600)
            tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
            print("[%s] %s · rc=%d · %s"
                  % (n.strftime("%H:%M:%S"), job["name"], p.returncode, tail[0][:90]))
            if p.returncode != 0 and p.stderr:
                print("        stderr: %s" % p.stderr.strip().splitlines()[-1][:120])
        except subprocess.TimeoutExpired:
            print("[%s] %s · 시간 초과 — 다음 주기에 재시도" % (n.strftime("%H:%M:%S"), job["name"]))
        except Exception as e:
            print("[%s] %s · 실행 실패: %s" % (n.strftime("%H:%M:%S"), job["name"], str(e)[:100]))
        if not every:
            stop.wait(2)          # 창 안이면 바로 이어서 다시 띄운다


def cmd_collect(stop: threading.Event):
    ensure_wal()
    print("수집 시작 · %s · 작업 %d개 (Ctrl+C 로 중단)"
          % (now_kst().strftime("%Y-%m-%d %H:%M"), len(JOBS)))
    for j in JOBS:
        print("   · %-14s %s" % (j["name"], j["script"]))
    ts = [threading.Thread(target=run_job, args=(j, stop), daemon=True, name=j["name"])
          for j in JOBS]
    for t in ts:
        t.start()
    return ts


def cmd_status():
    if not DB.is_file():
        print("DB 없음: %s" % DB); return 1
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    print("Bond Futures Lab · %s" % now_kst().strftime("%Y-%m-%d %H:%M:%S"))
    print("\n[봉]")
    for r in c.execute("SELECT instr_id,COUNT(*) n,MIN(bar_time) a,MAX(bar_time) b"
                       " FROM minbar GROUP BY instr_id ORDER BY instr_id"):
        lag = (now_kst() - dt.datetime.strptime(r["b"], "%Y-%m-%d %H:%M")).total_seconds() / 60
        print("  %-6s %6d봉  %s ~ %s  (%.0f분 전)" % (r["instr_id"], r["n"], r["a"], r["b"], lag))
    try:
        print("\n[호가]")
        for r in c.execute("SELECT instr_id,COUNT(*) n,MAX(ts) b FROM quote GROUP BY instr_id"):
            print("  %-6s %6d건  ~ %s" % (r["instr_id"], r["n"], r["b"]))
    except sqlite3.OperationalError:
        print("  (quote 테이블 없음)")
    try:
        print("\n[pair 추정치 최근]")
        for r in c.execute("SELECT ts,pair,n_bars,half_life,ecm_t_hac,adf_p FROM pair_history"
                           " ORDER BY ts DESC LIMIT 4"):
            print("  %s %-12s %5d봉 · half-life %s · ECM t %s · ADF p %s"
                  % (r["ts"], r["pair"], r["n_bars"],
                     ("%.1f분" % r["half_life"]) if r["half_life"] else "—",
                     ("%.2f" % r["ecm_t_hac"]) if r["ecm_t_hac"] is not None else "—",
                     ("%.4f" % r["adf_p"]) if r["adf_p"] is not None else "—"))
    except sqlite3.OperationalError:
        print("  (pair_history 없음 — monitor_pairs.py 를 한 번 돌리십시오)")
    return 0


def cmd_report():
    for script, args in (("econ_pair.py", ["--all"]), ("strategy_lab.py", [])):
        print("\n" + "=" * 70)
        subprocess.run([PY, "-u", str(TOOLS / script)] + args, cwd=str(ROOT))
    return 0


def cmd_serve(port: int):
    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        print("웹 서버에는 fastapi·uvicorn 이 필요합니다: pip install fastapi uvicorn")
        return 1
    app = FastAPI(title="Bond Futures Lab")

    def q(sql, args=()):
        c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    @app.get("/api/status")
    def api_status():
        return JSONResponse({
            "asof": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
            "bars": q("SELECT instr_id,COUNT(*) n,MAX(bar_time) last FROM minbar GROUP BY instr_id"),
            "pairs": q("SELECT * FROM pair_history ORDER BY ts DESC LIMIT 10"),
        })

    @app.get("/api/spread/{a}/{b}")
    def api_spread(a: str, b: str, limit: int = 300):
        return JSONResponse(q(
            "SELECT x.bar_time t, x.close-y.close s FROM minbar x JOIN minbar y"
            " ON x.bar_time=y.bar_time WHERE x.instr_id=? AND y.instr_id=?"
            " ORDER BY x.bar_time DESC LIMIT ?", (a, b, limit)))

    @app.get("/", response_class=HTMLResponse)
    def index():
        rows = q("SELECT instr_id,COUNT(*) n,MAX(bar_time) last FROM minbar GROUP BY instr_id")
        ph = q("SELECT * FROM pair_history ORDER BY ts DESC LIMIT 6")
        tr = "".join("<tr><td>%s</td><td style='text-align:right'>%d</td><td>%s</td></tr>"
                     % (r["instr_id"], r["n"], r["last"]) for r in rows)
        pr = "".join("<tr><td>%s</td><td>%s</td><td style='text-align:right'>%d</td>"
                     "<td style='text-align:right'>%s</td><td style='text-align:right'>%s</td></tr>"
                     % (r["ts"], r["pair"], r["n_bars"],
                        ("%.1f" % r["half_life"]) if r["half_life"] else "-",
                        ("%.2f" % r["ecm_t_hac"]) if r["ecm_t_hac"] is not None else "-")
                     for r in ph)
        return ("<meta charset=utf-8><title>Bond Futures Lab</title>"
                "<style>body{font:14px system-ui;background:#0b0e14;color:#e8ecf4;padding:24px}"
                "table{border-collapse:collapse;margin:12px 0}td,th{padding:6px 12px;"
                "border-bottom:1px solid #232b3a}h2{font-size:16px;margin-top:24px}</style>"
                "<h1>Bond Futures Lab</h1><p>%s</p>"
                "<h2>적재 현황</h2><table><tr><th>종목</th><th>봉</th><th>최신</th></tr>%s</table>"
                "<h2>pair 추정치 추이</h2><table><tr><th>시각</th><th>pair</th><th>봉</th>"
                "<th>half-life(분)</th><th>ECM t(HAC)</th></tr>%s</table>"
                "<p style='color:#8b95a7'>API: <code>/api/status</code> · "
                "<code>/api/spread/KTB10/ZN</code></p>"
                % (now_kst().strftime("%Y-%m-%d %H:%M:%S"), tr, pr))

    print("웹 대시보드 → http://127.0.0.1:%d" % port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Bond Futures Lab")
    ap.add_argument("mode", nargs="?", default="all",
                    choices=["all", "collect", "serve", "report", "status"])
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()

    if a.mode == "status":
        return cmd_status()
    if a.mode == "report":
        return cmd_report()

    stop = threading.Event()
    if a.mode in ("all", "collect"):
        cmd_collect(stop)
    try:
        if a.mode in ("all", "serve"):
            return cmd_serve(a.port)
        while True:                       # collect 전용 — 스레드가 일한다
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n중단 요청 — 수집기를 정리합니다")
        stop.set()
        time.sleep(2)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
