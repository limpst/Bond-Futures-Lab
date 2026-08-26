# -*- coding: utf-8 -*-
"""체결 지연 추정 — 주문을 내지 않고 잰다.

왜 주문을 안 내나: 이 프로젝트의 주문 경로는 3중으로 막혀 있고(dry 고정 ·
RMS 게이트 미연결 · 주문 TR body 미구현), dry 주문은 DB 에 기록만 되므로
거래소 왕복이 발생하지 않는다. 실주문으로 재는 것은 사람이 직접 할 일이다.

대신 leg risk 모델의 LEG_LAG_SEC(지금은 10초 가정) 를 좁히는 데 쓸 수 있는
세 가지를 시장 데이터와 읽기 전용 API 로 측정한다.

  A. API 왕복 지연     읽기 전용 TR(t8461) 을 반복 호출한 시간.
                       주문 지연의 **하한** 이다 — 주문은 이보다 느릴 수밖에 없다.
  B. 호가 갱신 간격    FH9 스냅샷 사이 시간. 시장이 얼마나 빨리 변하는지.
                       내 주문이 도착하기 전에 호가가 몇 번 바뀌는지를 알려준다.
  C. L1 소진·복원 시간 최우선 호가 잔량이 줄었다가 회복되는 데 걸리는 시간.
                       시장가로 쳤을 때 다음 단으로 밀릴 위험의 대용치.

★ 이 셋 중 어느 것도 '진짜 체결 지연' 이 아니다. A 는 하한, B·C 는 시장 속도다.
   실제 지연은 A + 브로커 내부 처리 + 거래소 매칭이며, 그건 실주문으로만 잴 수 있다.

  python tools/latency_probe.py            전부
  python tools/latency_probe.py --n 20     API 왕복 횟수
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
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DB = ROOT / "data" / "minbars.db"


def api_roundtrip(n=15, symbol="A6769000"):
    """A. 읽기 전용 TR 왕복 — 주문 지연의 하한."""
    from ls_openapi import call_tr
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            call_tr("kr_futopt", "/futureoption/chart", "t8461",
                    {"t8461InBlock": {"focode": symbol, "cgubun": "B",
                                      "bgubun": "1", "cnt": 1}})
            ts.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    return ts


def quote_gaps(instr="KTB10", limit=3000):
    """B. 호가 갱신 간격(초). 초 단위 PK 라 같은 초는 하나로 합쳐진 값이다."""
    c = sqlite3.connect(DB, timeout=60)
    rows = [r[0] for r in c.execute(
        "SELECT ts FROM quote WHERE instr_id=? ORDER BY ts DESC LIMIT ?", (instr, limit))]
    rows.reverse()
    T = [dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t in rows]
    g = [(T[i] - T[i - 1]).total_seconds() for i in range(1, len(T))]
    return [x for x in g if 0 < x <= 300]        # 세션 공백 제외


def l1_refill(instr="KTB10", limit=3000):
    """C. L1 잔량이 직전 대비 절반 이하로 준 뒤, 원래 수준을 회복하기까지의 시간."""
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        "SELECT ts,bq1,aq1 FROM quote WHERE instr_id=? ORDER BY ts DESC LIMIT ?",
        (instr, limit)))
    rows.reverse()
    T = [dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S") for r in rows]
    out = []
    for side in ("bq1", "aq1"):
        q = [r[side] or 0 for r in rows]
        i = 1
        while i < len(q):
            if q[i - 1] > 0 and q[i] <= q[i - 1] * 0.5:
                base, j = q[i - 1], i + 1
                while j < len(q) and q[j] < base:
                    if (T[j] - T[i]).total_seconds() > 300:
                        break
                    j += 1
                if j < len(q) and q[j] >= base:
                    out.append((T[j] - T[i]).total_seconds())
                i = j
            i += 1
    return [x for x in out if 0 < x <= 300]


def show(name, xs, unit, note=""):
    if not xs:
        print("  %-22s 표본 없음 %s" % (name, note)); return None
    xs = sorted(xs)
    p = lambda q: xs[min(len(xs) - 1, int(len(xs) * q))]
    print("  %-22s n=%-5d 중앙 %7.1f %s · p90 %7.1f · p99 %7.1f · 최대 %7.1f  %s"
          % (name, len(xs), st.median(xs), unit, p(.9), p(.99), xs[-1], note))
    return st.median(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--instr", default="KTB10")
    a = ap.parse_args()
    print("체결 지연 추정 · %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("(주문을 내지 않는다 — 아래는 전부 하한·대용치다)\n")

    print("A. API 왕복 (읽기 전용 t8461) — 주문 지연의 하한")
    rt = api_roundtrip(a.n)
    m_api = show("왕복", rt, "ms")

    print("\nB. 호가 갱신 간격 — 시장이 변하는 속도")
    g = quote_gaps(a.instr)
    m_gap = show("갱신 간격", g, "초", "(초 단위 PK 라 실제는 더 촘촘하다)")

    print("\nC. L1 잔량 소진 후 회복 시간 — 다음 단으로 밀릴 위험의 대용치")
    rf = l1_refill(a.instr)
    m_rf = show("회복", rf, "초")

    print("\n=== LEG_LAG_SEC 에 대한 시사 ===")
    if m_api:
        print("  · API 왕복만으로 이미 %.0f ms. 두 다리를 순차로 치면 최소 %.1f 초."
              % (m_api, m_api * 2 / 1000))
    if m_gap:
        print("  · 호가는 중앙값 %.0f 초마다 바뀐다. 지연 10초 동안 호가가 여러 번 갱신된다." % m_gap)
    if m_rf:
        print("  · L1 이 마르면 회복에 중앙값 %.0f 초. 이보다 짧게 기다리면 다음 단으로 밀린다." % m_rf)
    print("  · 현재 strategy_lab.py 의 LEG_LAG_SEC = 10.0 은 가정값이다.")
    print("    위 셋 중 무엇도 진짜 체결 지연이 아니므로, 확정하려면 사람이 직접")
    print("    모의투자 계좌로 주문을 내 왕복을 재야 한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
