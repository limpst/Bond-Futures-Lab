# -*- coding: utf-8 -*-
"""수집 검증 probe — "쌓이고 있다" 를 한 번의 조회로 말하지 않기 위해.

한 번 읽어 최신 봉 시각이 최근이면 그럴듯해 보이지만, 그것만으로는
(a) 예전에 쓰인 행을 지금 보고 있는 것인지 (b) 값이 말이 되는지
(c) 계속 늘고 있는지 를 구분할 수 없다. 이 probe 는 네 가지를 따로 본다.

  1) 신선도   collected_utc — DB 에 **언제 쓰였는지**. 봉 시각이 아니라 쓰인 시각.
  2) 증가     N 초 간격으로 두 번 세어 실제로 늘어나는지.
  3) 타당성   가격이 상품별 상식 범위인지 · OHLC 관계(H>=max(O,C)>=min(O,C)>=L)
              가 성립하는지 · 거래량이 음수가 아닌지.
  4) 겹침     KTB10-ZN 같은 분에 둘 다 있는 봉 수(본 pair 표본).

  python tools/verify_feed.py                기본 90초 간격 2회 측정
  python tools/verify_feed.py --wait 30      간격 조절
  python tools/verify_feed.py --json         기계용 출력
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"

# 상품별 상식 범위 (선물 가격) — 벗어나면 파싱/필드 오류를 의심한다
SANE = {
    "KTB3": (95, 120), "KTB10": (95, 130), "KTB30": (95, 140),
    "ZT": (95, 115), "ZF": (95, 120), "ZN": (95, 130),
    "ZB": (95, 145), "TN": (95, 135),
}


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def snapshot():
    con = ro()
    try:
        rows = {r[0]: {"n": r[1], "last_bar": r[2], "last_write": r[3]}
                for r in con.execute(
                    "SELECT instr_id, COUNT(*), MAX(bar_time), MAX(collected_utc) "
                    "FROM minbar GROUP BY instr_id")}
        overlap = con.execute(
            "SELECT COUNT(*) FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
            " WHERE x.instr_id='KTB10' AND y.instr_id='ZN'").fetchone()[0]
    finally:
        con.close()
    return rows, overlap


def sanity(iid, limit=30):
    """최근 봉의 값이 말이 되는가."""
    con = ro()
    try:
        rows = list(con.execute(
            "SELECT bar_time, open, high, low, close, volume FROM minbar "
            "WHERE instr_id=? ORDER BY bar_time DESC LIMIT ?", (iid, limit)))
    finally:
        con.close()
    if not rows:
        return {"checked": 0, "ok": False, "why": "봉 없음"}
    lo, hi = SANE.get(iid, (0, 1e9))
    bad_range = bad_ohlc = bad_vol = 0
    for _, o, h, l, c, v in rows:
        if not all(lo <= x <= hi for x in (o, h, l, c)):
            bad_range += 1
        if not (h >= max(o, c) and l <= min(o, c) and h >= l):
            bad_ohlc += 1
        if v is None or v < 0:
            bad_vol += 1
    return {"checked": len(rows), "bad_range": bad_range, "bad_ohlc": bad_ohlc,
            "bad_volume": bad_vol,
            "ok": (bad_range == 0 and bad_ohlc == 0 and bad_vol == 0),
            "px_last": rows[0][4], "vol_sum": sum(r[5] or 0 for r in rows)}


def age_min(ts_utc):
    if not ts_utc:
        return None
    try:
        t = dt.datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return round((dt.datetime.utcnow() - t).total_seconds() / 60, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=90, help="두 측정 사이 간격(초)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    t0 = dt.datetime.now()
    s1, ov1 = snapshot()
    time.sleep(a.wait)
    s2, ov2 = snapshot()
    t1 = dt.datetime.now()

    report = {"from": t0.strftime("%H:%M:%S"), "to": t1.strftime("%H:%M:%S"),
              "wait_s": a.wait, "overlap": {"before": ov1, "after": ov2,
                                            "gained": ov2 - ov1},
              "instruments": {}}
    for iid in sorted(set(s1) | set(s2)):
        b, c = s1.get(iid, {"n": 0}), s2.get(iid, {"n": 0})
        sane = sanity(iid)
        report["instruments"][iid] = {
            "n_before": b.get("n", 0), "n_after": c.get("n", 0),
            "gained": c.get("n", 0) - b.get("n", 0),
            "last_bar": c.get("last_bar"),
            "write_age_min": age_min(c.get("last_write")),
            "sane": sane,
        }
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(f"[verify] {report['from']} → {report['to']} ({a.wait}초 간격 2회 측정)")
    print(f"{'종목':<7}{'봉수':>7}{'증가':>6}{'쓰인지':>8}  {'값 검사':<26}마지막 봉")
    live = []
    for iid, r in report["instruments"].items():
        s = r["sane"]
        mark = "OK" if s["ok"] else (
            f"이상 range{s.get('bad_range')} ohlc{s.get('bad_ohlc')} vol{s.get('bad_volume')}")
        chk = f"{mark} · 최근가 {s.get('px_last')}"
        w = r["write_age_min"]
        print(f"{iid:<7}{r['n_after']:>7}{r['gained']:>+6}"
              f"{('—' if w is None else f'{w}분'):>8}  {chk:<26}{r['last_bar']}")
        if r["gained"] > 0:
            live.append(iid)
    print(f"\nKTB10-ZN 겹침: {ov1} → {ov2} ({ov2 - ov1:+d})")
    print("증가가 관측된 종목:", ", ".join(live) if live else "없음")
    print("판정:", "🟢 수집 살아 있음" if live else
          "🟠 이 간격에는 새 봉 없음 — 무거래이거나 수집 정지(둘 다 가능)")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
