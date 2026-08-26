# -*- coding: utf-8 -*-
"""pair 추정치 추이 기록 — half-life 가 표본과 함께 어떻게 변하는지 남긴다.

왜 필요한가: KTB10-ZN 의 half-life 가 68봉 표본에서 4.6분, 265봉에서 32.9분로
7배 변했다(2026-08-26 실측). 짧은 표본의 추정치는 못 믿는다. 매번 값을 새로
계산해 화면에 띄우는 것만으로는 그 사실이 안 보이므로, **추정치를 시계열로
쌓아** 언제 안정되는지 눈으로 확인할 수 있게 한다.

실행할 때마다 pair_history 테이블에 한 줄 append 한다. 값을 덮어쓰지 않는다.

  python tools/monitor_pairs.py            KTB3-KTB10, KTB10-ZN 둘 다 기록
  python tools/monitor_pairs.py --show     쌓인 추이 보기
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
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DB = ROOT / "data" / "minbars.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pair_history(
  ts        TEXT NOT NULL,          -- 기록 시각 (KST)
  pair      TEXT NOT NULL,
  n_bars    INTEGER, n_pairs INTEGER, n_sessions INTEGER,
  spread_now REAL, spread_mean REAL, spread_std REAL, z REAL,
  ar1 REAL, half_life REAL,
  ecm_gamma REAL, ecm_t_hac REAL,
  adf_p REAL, adf_n INTEGER,
  PRIMARY KEY(ts, pair)
)"""

PAIRS = [("KTB3", "KTB10"), ("KTB10", "ZN")]


def record():
    import econ_pair as EP
    con = sqlite3.connect(DB, timeout=60)
    con.execute(SCHEMA); con.commit()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for a, b in PAIRS:
        r = EP.analyse(a, b)
        if r.get("status") != "ok":
            print("  %-12s 건너뜀 — %s" % ("%s-%s" % (a, b), r.get("status")))
            continue
        con.execute(
            "INSERT INTO pair_history VALUES(" + ",".join(["?"] * 15) + ")"
            " ON CONFLICT(ts,pair) DO NOTHING",
            (now, r["pair"], r["n_bars"], r.get("n_pairs"), r.get("n_sessions"),
             r.get("spread_now"), r.get("spread_mean"), r.get("spread_std"),
             r.get("z_full"), r.get("ar1_b"), r.get("half_life_min"),
             r.get("ecm_gamma"), r.get("ecm_t_hac10"), r.get("adf_p"), r.get("adf_n")))
        hl = r.get("half_life_min")
        print("  %-12s %5d봉 · half-life %s · ECM t(HAC) %s · ADF p %s"
              % (r["pair"], r["n_bars"],
                 ("%.1f분" % hl) if hl else "—",
                 ("%.2f" % r["ecm_t_hac10"]) if r.get("ecm_t_hac10") == r.get("ecm_t_hac10") else "—",
                 ("%.4f" % r["adf_p"]) if r.get("adf_p") is not None else "—"))
    con.commit()


def show():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute("SELECT * FROM pair_history ORDER BY pair, ts"))
    except sqlite3.OperationalError:
        print("기록 없음 — 먼저 인자 없이 실행하십시오."); return
    if not rows:
        print("기록 없음"); return
    cur = None
    for r in rows:
        if r["pair"] != cur:
            cur = r["pair"]
            print("\n=== %s ===" % cur)
            print("  %-16s %6s %10s %10s %8s %8s"
                  % ("기록시각", "봉", "half-life", "ECM t(HAC)", "ADF p", "z"))
        print("  %-16s %6d %10s %10s %8s %8s"
              % (r["ts"], r["n_bars"],
                 ("%.1f분" % r["half_life"]) if r["half_life"] else "—",
                 ("%.2f" % r["ecm_t_hac"]) if r["ecm_t_hac"] is not None else "—",
                 ("%.4f" % r["adf_p"]) if r["adf_p"] is not None else "—",
                 ("%.2f" % r["z"]) if r["z"] is not None else "—"))
    print("\n읽는 법: 표본(봉)이 늘어도 half-life 가 흔들리면 아직 추정이 안정되지 않은 것이다.")
    print("         ECM t(HAC) 의 절대값이 1.96 을 넘어야 '되돌아온다' 를 통계로 말할 수 있다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.show:
        show()
    else:
        print("pair 추정치 기록 · %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        record()
    return 0


if __name__ == "__main__":
    sys.exit(main())
