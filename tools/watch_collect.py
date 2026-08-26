# -*- coding: utf-8 -*-
"""수집 정지 감시 — 최신 봉이 안 늘면 경고를 남긴다.

어제(2026-08-24) 밤 수집기가 19:41 에 죽었는데 다음 날 아침에야 알았다.
그런 일을 다시 겪지 않으려고 만든다. 고치지는 않고 **알리기만** 한다 —
자동 재시작은 작업 스케줄러의 RestartCount 가 맡는다.

판정: 지금이 그 종목의 '거래 시간대' 인데 최신 봉이 허용 지연을 넘으면 stale.
      거래 시간이 아니면 조용히 넘어간다 (휴장에 경고를 울리면 아무도 안 본다).

시간대 (KST):
  KRX 주간  09:00~15:45   WebSocket FC9   허용 5분
  KRX 야간  18:00~05:00   REST t8461 5분 폴링  허용 12분
  CME       07:00~06:00(익일), 06:00~07:00 휴식  허용 10분

결과는 화면과 collect_log(status='stale'|'ok') 양쪽에 남긴다.

  python tools/watch_collect.py            한 번 점검
  python tools/watch_collect.py --quiet    문제 있을 때만 출력
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
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"


def _sqlite_connect_safe(*args, **kwargs):
    """랩 공통 커넥션 — 락을 만나면 죽지 않고 기다린다(2026-08-26 사고 대응)."""
    import sqlite3 as _s3
    kwargs.setdefault("timeout", 60)
    _c = _s3.connect(*args, **kwargs)
    try:
        _c.execute("PRAGMA busy_timeout=60000")
    except Exception:
        pass
    return _c


def in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """자정을 넘는 구간(예: 18:00~05:00)도 처리한다."""
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


# 저유동 종목별 허용 지연(분) — 체결이 드물어 기본 허용치로는 오경보가 난다
THIN = {"KTB30": 60}

# 그 시간대에 **데이터 소스 자체가 없는** 조합. 감시에서 제외한다.
# KTB30 은 주간 WebSocket(FC9) 으로만 잡힌다 — 야간 REST(t8461) 는 계속
# rows_in=0 을 돌려준다(2026-08-26 실측). 재시작해도 안 고쳐지므로
# 경보를 울리면 자동 재시작만 헛돈다.
NO_SOURCE = {("KTB30", "KRX 야간")}

# (라벨, 시작, 끝, 허용 지연 분, 해당 market, 되살릴 작업 이름)
WINDOWS = [
    ("KRX 주간", dt.time(9, 0), dt.time(15, 45), 5, "KRX", "KTB day ticks"),
    ("KRX 야간", dt.time(18, 0), dt.time(5, 0), 12, "KRX", "KTB night bars"),
    ("CME", dt.time(7, 0), dt.time(6, 0), 10, "CME", "CME bars"),
]

# 재시작 쿨다운(분) — 같은 작업을 계속 두드리지 않는다. 원인이 전원·네트워크면
# 재시작해도 안 되므로, 반복은 로그만 남기고 사람이 보게 한다.
RESTART_COOLDOWN_MIN = 20


def last_restart(con, task):
    r = con.execute(
        "SELECT ts_utc FROM collect_log WHERE instr_id='RESTART' AND detail LIKE ?"
        " ORDER BY id DESC LIMIT 1", ("%" + task + "%",)).fetchone()
    if not r:
        return None
    try:
        return dt.datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def restart(con, task, reason):
    """멈춘 수집 작업을 되살린다. 실패해도 감시는 계속된다."""
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    prev = last_restart(con, task)
    if prev and (now_utc - prev).total_seconds() < RESTART_COOLDOWN_MIN * 60:
        print("     [재시작 보류] %s — 직전 시도가 %.0f분 전 (쿨다운 %d분)"
              % (task, (now_utc - prev).total_seconds() / 60, RESTART_COOLDOWN_MIN))
        return False
    ok, msg = False, ""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", task],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        ok = (r.returncode == 0)
        msg = (r.stdout or r.stderr or "").strip().splitlines()[-1][:120] if (r.stdout or r.stderr) else ""
    except Exception as e:
        msg = str(e)[:120]
    print("     [재시작] %s → %s  %s" % (task, "성공" if ok else "실패", msg))
    con.execute(
        "INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail)"
        " VALUES(?,?,?,?,?,?,?)",
        (now_utc.strftime("%Y-%m-%d %H:%M:%S"), "RESTART", "-", 0, 0,
         "ok" if ok else "error", "%s | %s | %s" % (task, reason[:120], msg)))
    con.commit()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="문제 있을 때만 출력")
    ap.add_argument("--no-restart", action="store_true", help="감시만 하고 되살리지 않는다")
    a = ap.parse_args()

    if not DB.is_file():
        print("DB 없음: %s" % DB)
        return 1
    con = _sqlite_connect_safe(DB, timeout=60)
    con.row_factory = sqlite3.Row
    now = dt.datetime.now()
    rows = {r["instr_id"]: (r["market"], r["last"]) for r in con.execute(
        "SELECT i.instr_id, i.market, (SELECT MAX(bar_time) FROM minbar m"
        "  WHERE m.instr_id = i.instr_id) AS last"
        " FROM instrument i WHERE i.active = 1")}

    problems = []
    lines = []
    stalled_tasks: dict[str, list] = {}
    for iid, (market, last) in sorted(rows.items()):
        win = [w for w in WINDOWS if w[4] == market and in_window(now.time(), w[1], w[2])]
        if not win:
            lines.append("  %-6s %-4s 휴장 시간대 — 건너뜀 (최신 %s)"
                         % (iid, market, last or "없음"))
            continue
        label, _, _, tol, _, task = win[0]
        if (iid, label) in NO_SOURCE:
            lines.append("  %-6s %-4s [%s] — 이 시간대엔 데이터 소스 없음, 감시 제외"
                         % (iid, market, label))
            continue
        # 저유동 종목은 체결이 드물어 '정지' 와 '거래 없음' 이 구분되지 않는다.
        # KTB30 은 실측상 분당 체결이 거의 없어 허용치를 넉넉히 준다.
        if iid in THIN:
            tol = max(tol, THIN[iid])
        if not last:
            problems.append("%s: 봉이 하나도 없음 (%s)" % (iid, label))
            stalled_tasks.setdefault(task, []).append(iid)
            lines.append("  %-6s %-4s [%s] ✗ 봉 없음" % (iid, market, label))
            continue
        lag = (now - dt.datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() / 60
        ok = lag <= tol
        if not ok:
            problems.append("%s: %.0f분째 정지 (허용 %d분, %s)" % (iid, lag, tol, label))
            stalled_tasks.setdefault(task, []).append(iid)
        lines.append("  %-6s %-4s [%s] %s 최신 %s · 지연 %.0f분 (허용 %d)"
                     % (iid, market, label, "✓" if ok else "✗", last, lag, tol))

    if problems or not a.quiet:
        print("수집 감시 · %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
        for ln in lines:
            print(ln)
    if problems:
        print("\n[경고] 정지 의심 %d건" % len(problems))
        for p in problems:
            print("   · " + p)

    con.execute(
        "INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail)"
        " VALUES(?,?,?,?,?,?,?)",
        (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         "WATCH", "-", len(rows), 0, "stale" if problems else "ok",
         " | ".join(problems)[:500]))
    con.commit()
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
