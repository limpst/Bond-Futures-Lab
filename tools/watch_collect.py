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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"


def in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """자정을 넘는 구간(예: 18:00~05:00)도 처리한다."""
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


# 저유동 종목별 허용 지연(분) — 체결이 드물어 기본 허용치로는 오경보가 난다
THIN = {"KTB30": 60}

# (라벨, 시작, 끝, 허용 지연 분, 해당 market)
WINDOWS = [
    ("KRX 주간", dt.time(9, 0), dt.time(15, 45), 5, "KRX"),
    ("KRX 야간", dt.time(18, 0), dt.time(5, 0), 12, "KRX"),
    ("CME", dt.time(7, 0), dt.time(6, 0), 10, "CME"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="문제 있을 때만 출력")
    a = ap.parse_args()

    if not DB.is_file():
        print("DB 없음: %s" % DB)
        return 1
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    now = dt.datetime.now()
    rows = {r["instr_id"]: (r["market"], r["last"]) for r in con.execute(
        "SELECT i.instr_id, i.market, (SELECT MAX(bar_time) FROM minbar m"
        "  WHERE m.instr_id = i.instr_id) AS last"
        " FROM instrument i WHERE i.active = 1")}

    problems = []
    lines = []
    for iid, (market, last) in sorted(rows.items()):
        win = [w for w in WINDOWS if w[4] == market and in_window(now.time(), w[1], w[2])]
        if not win:
            lines.append("  %-6s %-4s 휴장 시간대 — 건너뜀 (최신 %s)"
                         % (iid, market, last or "없음"))
            continue
        label, _, _, tol, _ = win[0]
        # 저유동 종목은 체결이 드물어 '정지' 와 '거래 없음' 이 구분되지 않는다.
        # KTB30 은 실측상 분당 체결이 거의 없어 허용치를 넉넉히 준다.
        if iid in THIN:
            tol = max(tol, THIN[iid])
        if not last:
            problems.append("%s: 봉이 하나도 없음 (%s)" % (iid, label))
            lines.append("  %-6s %-4s [%s] ✗ 봉 없음" % (iid, market, label))
            continue
        lag = (now - dt.datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() / 60
        ok = lag <= tol
        if not ok:
            problems.append("%s: %.0f분째 정지 (허용 %d분, %s)" % (iid, lag, tol, label))
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
