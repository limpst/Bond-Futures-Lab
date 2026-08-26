# -*- coding: utf-8 -*-
"""backfill 가능 깊이 실측 — 얼마나 과거까지 되받을 수 있나.

두 채널의 조회 방식이 다르다:
  국내선옵 t8461  : 날짜 파라미터 없음. cnt(요청 봉 수)만 키울 수 있다 →
                    "최근 N봉" 이므로 N 을 키운 만큼 과거로 내려간다.
  해외선옵 chart  : cts_date/cts_time 연속조회 커서가 있다 → 페이지를 계속
                    넘겨 과거로 갈 수 있다.

이 스크립트는 실제로 호출해서 (a) 응답이 몇 봉 오는지 (b) 가장 오래된 봉이
언제인지를 찍는다. 결과가 backfill 설계의 근거가 된다.

  python tools/probe_backfill_depth.py
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "tools"))
from ls_openapi import call_tr, load_env  # noqa: E402
from collect_minbars import CONFIG  # noqa: E402


def sym_of(instr_id):
    con = sqlite3.connect(ROOT / "data" / "minbars.db", timeout=60)
    r = con.execute("SELECT channel, symbol FROM instrument WHERE instr_id=?",
                    (instr_id,)).fetchone()
    con.close()
    return r


def probe_kr(instr_id, cnt):
    ch, sym = sym_of(instr_id)
    c = CONFIG[ch]["chart"]
    body = {c["in_block"]: {"focode": sym, "cgubun": "B", "bgubun": "1", "cnt": cnt}}
    j = call_tr(ch, c["path"], c["tr_cd"], body)
    rows = j.get(c["out_block"], []) or []
    if isinstance(rows, dict):
        rows = [rows]
    times = [str(r.get("chetime", "")).zfill(6) for r in rows]
    print(f"  KR {instr_id} cnt={cnt:>5} → 수신 {len(rows):>5}행"
          f" | 최신 {times[0] if times else '—'} | 최고(가장 과거) {times[-1] if times else '—'}")
    return len(rows)


def probe_os(instr_id, readcnt, pages=3):
    ch, sym = sym_of(instr_id)
    c = CONFIG[ch]["chart"]
    cts_d, cts_t = "", ""
    total, oldest = 0, None
    for p in range(pages):
        body = {c["in_block"]: {"shcode": sym, "ncnt": 1, "readcnt": readcnt,
                                "cts_date": cts_d, "cts_time": cts_t}}
        j = call_tr(ch, c["path"], c["tr_cd"], body)
        rows = j.get(c["out_block"], []) or []
        if isinstance(rows, dict):
            rows = [rows]
        head = j.get(c["in_block"].replace("InBlock", "OutBlock")) or {}
        total += len(rows)
        if rows:
            oldest = f'{rows[-1].get("date","")} {str(rows[-1].get("time","")).zfill(6)}'
        nd = str(head.get("cts_date", "") or "").strip()
        nt = str(head.get("cts_time", "") or "").strip()
        print(f"  OS {instr_id} page{p+1} readcnt={readcnt} → {len(rows)}행"
              f" | oldest {oldest} | next_cts=({nd},{nt})")
        if not rows or (nd == cts_d and nt == cts_t) or not (nd or nt):
            break
        cts_d, cts_t = nd, nt
    print(f"  OS {instr_id} 합계 {total}행, 가장 과거 {oldest}")
    return total


if __name__ == "__main__":
    load_env()
    print("[probe] 국내선옵 t8461 — cnt 를 키우면 과거로 얼마나 가나")
    for n in (500, 900, 2000, 5000):
        try:
            got = probe_kr("KTB10", n)
            if got < n * 0.9:
                print(f"    ↳ 상한 도달 추정: 요청 {n} > 응답 {got}")
                break
        except Exception as e:
            print(f"    KR cnt={n} FAIL — {e}")
            break
    print("[probe] 해외선옵 chart — cts 연속조회로 과거 페이징")
    try:
        probe_os("ZN", 500, pages=3)
    except Exception as e:
        print(f"    OS FAIL — {e}")
