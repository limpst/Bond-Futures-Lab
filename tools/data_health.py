# -*- coding: utf-8 -*-
"""데이터 상태 점검 + 자동 backfill — PC 가 꺼졌거나 인터넷이 끊긴 구간을 메운다.

■ 무엇이 가능하고 무엇이 불가능한가 (2026-08-26 API 실측)

  국내선옵 t8461  날짜 파라미터가 없다. cnt(요청 봉 수)만 지정 가능하고
                  **cnt 상한이 900** 이다(2000 요청 시 IGW40011 거부).
                  → 되받을 수 있는 과거는 **최근 900분(약 15시간)** 뿐.
                  t8415/t8414/t2209/t8416~t8419 는 REST 게이트웨이가
                  '유효하지 않은 TR' 로 거부한다(실측).
  CME(해외)      LS 로는 못 받는다(o3121 마스터에 CME 없음 · o3106 빈 응답 ·
                  o3103 차트 0봉). 그러나 **밖에서 받을 수 있다** —
                  tools/backfill_yf.py 가 yfinance 1분봉으로 최근 7일의
                  빠진 분만 채운다. 겹치는 구간 가격차 중앙 0.000000·최대
                  반 틱으로 동일함을 확인했고(2026-08-26), 월물이 어긋나면
                  (ZB·TN 처럼 차가 크면) 자동으로 채우지 않는다.

  결론: **국내는 최근 15시간(LS) · CME 는 최근 7일(yfinance) 까지 복구된다.**
  그보다 오래된 구멍만 영구 결측
  이다. 영구 결측은 지우지 않고 data_gap 테이블에 기록해 화면에 그대로
  노출한다 — 없는 데이터를 있는 척하지 않는 것이 이 랩의 규칙이다.

■ 하는 일
  1) instrument 별로 세션(60분 초과 공백으로 분리)을 나누고, 세션 안의
     빠진 분(hole)과 세션 사이의 공백을 계산한다.
  2) 최근 900분 안에 구멍이 있으면 collect_minbars 를 cnt=900 으로 호출해
     즉시 메운다(국내). 해외는 cts 페이징으로 과거를 훑는다.
  3) backfill 후 다시 계산해 '메워진 봉 수' 를 남기고, 남은 구멍은
     복구 가능/불가로 갈라 data_gap 에 기록한다.
  4) reports/data_health.json 로 저장 — 대시보드가 이 파일을 읽어 표시한다.

  python tools/data_health.py              점검 + 자동 backfill
  python tools/data_health.py --check      점검만 (호출 없음)
  python tools/data_health.py --os-pages 6 해외 과거 페이징 깊이
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
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "tools"))

DB = ROOT / "data" / "minbars.db"
GAP_MIN = 60                 # 이보다 크게 비면 다른 세션
KR_REACH_MIN = 900           # t8461 cnt 상한 = 되받을 수 있는 과거(분)
PY = _sys.executable

GAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_gap(
  instr_id    TEXT NOT NULL,
  gap_from    TEXT NOT NULL,      -- 빠진 첫 분
  gap_to      TEXT NOT NULL,      -- 빠진 마지막 분
  minutes     INTEGER NOT NULL,
  kind        TEXT NOT NULL,      -- hole(세션 내부) · session(세션 사이)
  recoverable INTEGER NOT NULL,   -- 1 = API 사거리 안 · 0 = 영구 결측
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  filled      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instr_id, gap_from)
);
"""


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute('PRAGMA busy_timeout=60000')
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(GAP_SCHEMA)
    return con


_UPCACHE = {}


def uptime_windows(con, slack_min=6):
    """수집기가 살아 있던 구간(KST) 목록. collect_log 의 ts_utc(UTC)를 KST 로 옮기고
    각 실행이 앞뒤 slack_min 분을 덮는다고 본다(스케줄러 5분 간격 + 여유 1분).

    왜 필요한가: 선물은 **그 분에 체결이 없으면 봉이 아예 생기지 않는다**.
    따라서 '봉이 빈 분' 은 두 가지가 섞여 있다 — (a) 무거래(정상) (b) 수집 중단
    (진짜 결측). 수집기가 그때 돌고 있었다면 (a) 다. 이 구분을 못 하면 정상적인
    한산한 장을 데이터 사고로 잘못 보고하게 된다.
    """
    if slack_min in _UPCACHE:
        return _UPCACHE[slack_min]
    ts = [r[0] for r in con.execute("SELECT DISTINCT ts_utc FROM collect_log ORDER BY ts_utc")]
    wins = []
    for t in ts:
        try:
            u = dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        k = u + dt.timedelta(hours=9)                     # UTC -> KST
        a, b = k - dt.timedelta(minutes=slack_min), k + dt.timedelta(minutes=slack_min)
        if wins and a <= wins[-1][1]:
            wins[-1][1] = max(wins[-1][1], b)
        else:
            wins.append([a, b])
    out = [(a, b) for a, b in wins]
    _UPCACHE[slack_min] = out
    return out


def covered_by(wins, a, b):
    """[a,b] 가 수집 가동 구간에 완전히 덮이나."""
    for wa, wb in wins:
        if wa <= a and b <= wb:
            return True
    return False


def bars_of(con, iid):
    return [dt.datetime.strptime(r[0], "%Y-%m-%d %H:%M")
            for r in con.execute(
                "SELECT bar_time FROM minbar WHERE instr_id=? ORDER BY bar_time", (iid,))]


def analyse(con, iid, now=None):
    """세션 분리 + 세션 내부 구멍 계산. 반환: (요약dict, 구멍리스트)"""
    now = now or dt.datetime.now()
    T = bars_of(con, iid)
    if not T:
        return {"instr_id": iid, "n_bars": 0, "status": "no_data"}, []
    segs, s = [], 0
    for i in range(1, len(T)):
        if (T[i] - T[i - 1]).total_seconds() > GAP_MIN * 60:
            segs.append((s, i)); s = i
    segs.append((s, len(T)))

    wins = uptime_windows(con)
    holes, notrade_min = [], 0
    covered = 0
    for a, b in segs:
        covered += b - a
        for i in range(a + 1, b):
            d = int((T[i] - T[i - 1]).total_seconds() // 60)
            if d > 1:
                g_from = T[i - 1] + dt.timedelta(minutes=1)
                g_to = T[i] - dt.timedelta(minutes=1)
                if covered_by(wins, g_from, g_to):
                    # 수집기는 돌고 있었다 → 그 분에 체결이 없었을 뿐(정상)
                    notrade_min += d - 1
                    continue
                holes.append({"from": g_from, "to": g_to, "minutes": d - 1, "kind": "hole"})
    # 마지막 봉 이후 지금까지의 공백(수집 정지 중일 수 있음)
    tail_min = int((now - T[-1]).total_seconds() // 60)
    span_min = int((T[-1] - T[0]).total_seconds() // 60) + 1
    summary = {
        "instr_id": iid, "n_bars": len(T), "status": "ok",
        "first": T[0].strftime("%Y-%m-%d %H:%M"), "last": T[-1].strftime("%Y-%m-%d %H:%M"),
        "n_sessions": len(segs), "span_min": span_min,
        "covered_min": covered,
        "hole_min": sum(h["minutes"] for h in holes),
        "notrade_min": notrade_min,
        "stale_min": tail_min,
        "sessions": [{"n": b - a, "from": T[a].strftime("%m-%d %H:%M"),
                      "to": T[b - 1].strftime("%m-%d %H:%M")} for a, b in segs],
    }
    return summary, holes


def record_gaps(con, iid, holes, now):
    """구멍을 data_gap 에 기록(복구 가능 여부 판정 포함). 반환: (복구가능수, 영구수)"""
    rec = perm = 0
    for h in holes:
        age_min = int((now - h["to"]).total_seconds() // 60)
        recoverable = 1 if age_min <= KR_REACH_MIN else 0
        rec += recoverable
        perm += 1 - recoverable
        con.execute(
            "INSERT INTO data_gap(instr_id,gap_from,gap_to,minutes,kind,recoverable,"
            "first_seen,last_seen,filled) VALUES(?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(instr_id,gap_from) DO UPDATE SET "
            "gap_to=excluded.gap_to, minutes=excluded.minutes,"
            "recoverable=excluded.recoverable, last_seen=excluded.last_seen",
            (iid, h["from"].strftime("%Y-%m-%d %H:%M"), h["to"].strftime("%Y-%m-%d %H:%M"),
             h["minutes"], h["kind"], recoverable, now_str(), now_str()))
    con.commit()
    return rec, perm


def mark_filled(con):
    """이전에 기록된 구멍 중 이제 데이터가 있는 것은 filled=1 로 닫는다."""
    n = 0
    for iid, gfrom, gto in con.execute(
            "SELECT instr_id, gap_from, gap_to FROM data_gap WHERE filled=0").fetchall():
        got = con.execute(
            "SELECT COUNT(*) FROM minbar WHERE instr_id=? AND bar_time BETWEEN ? AND ?",
            (iid, gfrom, gto)).fetchone()[0]
        if got:
            con.execute("UPDATE data_gap SET filled=1, last_seen=? "
                        "WHERE instr_id=? AND gap_from=?", (now_str(), iid, gfrom))
            n += 1
    con.commit()
    return n


def run_backfill(os_pages: int):
    """국내: cnt=900 최대 사거리로 재수집. 해외: cts 페이징(장중에만 응답)."""
    out = {}
    r = subprocess.run([PY, str(ROOT / "tools" / "collect_minbars.py"),
                        "--live", "--count", "900"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out["kr"] = {"rc": r.returncode, "tail": (r.stdout or "")[-400:]}
    print("[backfill] 국내 cnt=900 재수집 rc=%s" % r.returncode)
    if os_pages > 0:
        try:
            n = backfill_os(os_pages)
            out["os"] = {"rows": n}
        except Exception as e:
            out["os"] = {"error": str(e)}
            print("[backfill] 해외 실패 —", e)
    return out


def backfill_os(pages: int) -> int:
    """해외선옵 o3103 을 cts 커서로 과거 페이징하며 minbar 에 채운다."""
    from ls_openapi import call_tr, load_env
    from collect_minbars import CONFIG
    load_env()
    con = open_db()
    c = CONFIG["os_futopt"]["chart"]
    f = c["fields"]
    total = 0
    targets = con.execute(
        "SELECT instr_id, symbol FROM instrument WHERE channel='os_futopt' AND active=1"
    ).fetchall()
    for iid, sym in targets:
        if not sym:
            continue
        cts_d = cts_t = ""
        for p in range(pages):
            body = {c["in_block"]: {"shcode": sym, "ncnt": 1, "readcnt": 500,
                                    "cts_date": cts_d, "cts_time": cts_t}}
            try:
                j = call_tr("os_futopt", c["path"], c["tr_cd"], body)
            except Exception as e:
                print(f"[backfill:os] {iid} page{p+1} FAIL — {e}")
                break
            rows = j.get(c["out_block"], []) or []
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                break
            new = 0
            for r in rows:
                d = str(r.get(f["date"], ""))
                t = str(r.get(f["time"], "")).zfill(6)
                if len(d) != 8:
                    continue
                bar = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"
                cur = con.execute(
                    "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,"
                    "symbol,collected_utc) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(instr_id,bar_time) DO NOTHING",
                    (iid, bar, float(r.get(f["open"], 0) or 0), float(r.get(f["high"], 0) or 0),
                     float(r.get(f["low"], 0) or 0), float(r.get(f["close"], 0) or 0),
                     float(r.get(f["volume"], 0) or 0), sym,
                     dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
                new += cur.rowcount
            con.commit()
            total += new
            head = j.get(c["in_block"].replace("InBlock", "OutBlock")) or {}
            nd = str(head.get("cts_date", "") or "").strip()
            nt = str(head.get("cts_time", "") or "").strip()
            print(f"[backfill:os] {iid} page{p+1}: {len(rows)}행 · 신규 {new}행")
            if not (nd or nt) or (nd == cts_d and nt == cts_t):
                break
            cts_d, cts_t = nd, nt
    con.close()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="점검만 (API 호출 없음)")
    ap.add_argument("--os-pages", type=int, default=3, help="해외 과거 페이징 깊이")
    a = ap.parse_args()

    now = dt.datetime.now()
    con = open_db()
    iids = [r[0] for r in con.execute(
        "SELECT instr_id FROM instrument WHERE active=1 ORDER BY instr_id")]

    before = {}
    for iid in iids:
        s, h = analyse(con, iid, now)
        before[iid] = s
        if h:
            record_gaps(con, iid, h, now)

    need = any(s.get("hole_min", 0) > 0 or s.get("stale_min", 999) > 5
               for s in before.values())
    bf = None
    if need and not a.check:
        print("[health] 결측 또는 지연 감지 → backfill 시작")
        bf = run_backfill(a.os_pages)
        con.close()
        con = open_db()
    elif not need:
        print("[health] 결측 없음 · 최신 — backfill 불필요")

    filled = mark_filled(con)
    after, gaps_rec, gaps_perm = {}, 0, 0
    for iid in iids:
        s, h = analyse(con, iid, now)
        after[iid] = s
        if h:
            r, p = record_gaps(con, iid, h, now)
            gaps_rec += r
            gaps_perm += p

    perm_rows = [dict(instr_id=r[0], gap_from=r[1], gap_to=r[2], minutes=r[3])
                 for r in con.execute(
                     "SELECT instr_id,gap_from,gap_to,minutes FROM data_gap "
                     "WHERE filled=0 AND recoverable=0 ORDER BY gap_from DESC LIMIT 40")]
    gained = {i: after[i]["n_bars"] - before[i]["n_bars"] for i in iids}
    report = {
        "asof": now.strftime("%Y-%m-%d %H:%M:%S"),
        "kr_reach_min": KR_REACH_MIN,
        "backfilled_bars": sum(v for v in gained.values() if v > 0),
        "gained": gained,
        "closed_gaps": filled,
        "open_recoverable": gaps_rec,
        "open_permanent": gaps_perm,
        "permanent_gaps": perm_rows,
        "instruments": after,
        "backfill_run": bool(bf),
    }
    outp = ROOT / "reports" / "data_health.json"
    outp.parent.mkdir(exist_ok=True)
    outp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[health] {outp.name} 저장 · 메운 봉 {report['backfilled_bars']}개 · "
          f"닫힌 구멍 {filled}개 · 남은 구멍 복구가능 {gaps_rec} / 영구 {gaps_perm}")
    for iid in iids:
        s = after[iid]
        if s.get("status") != "ok":
            print(f"  {iid:6s} 데이터 없음")
            continue
        print(f"  {iid:6s} {s['n_bars']:>5}봉 · 세션 {s['n_sessions']} · "
              f"결측 {s['hole_min']:>4}분 · 무거래 {s.get('notrade_min',0):>4}분 · "
              f"마지막 {s['last']} ({s['stale_min']}분 전)")
    con.close()


if __name__ == "__main__":
    main()
