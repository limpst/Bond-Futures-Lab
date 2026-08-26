# -*- coding: utf-8 -*-
"""오더북 마이크로스트럭처 지표 — quote(FH9 5단 호가) 에서 계산.

PLANC_RMS_v7 의 `_computeMicro`(frontend/index.html) 공식을 Python 으로 옮겼다.
그쪽은 브라우저에서 tick 을 받아 즉석 계산하고, 여기서는 DB 에 쌓인 호가를
읽어 시계열로 만든다 — 그래야 '이 지표가 다음 1분 가격을 맞히는가' 를 잴 수 있다.

지표 (근거 논문 병기):
  OBI       order book imbalance L1 = (bq1−aq1)/(bq1+aq1)
  OBI_w     거리 1/i 가중 다단 OBI
  wmid      weighted mid = (bq1·ap1 + aq1·bp1)/(bq1+aq1)  — 잔량이 많은 쪽에서 멀어진다
  micro     Micro-Price 근사 (Stoikov 2018, Quant. Finance 18:12) — 여기서는 wmid 를 쓴다.
            정식 Stoikov 는 마팅게일 보정을 반복 적용하지만 호가 스냅샷만으로는 불가.
  spr_bps   relative spread = (ap1−bp1)/mid × 10⁴
  depth1p   mid ±1% 안쪽 잔량 합
  VOI       Cont-Kukanov-Stoikov (2014) 가격 조건부 order flow imbalance
              eb = bq1(bid↑) · 0(bid↓) · Δbq1(bid=)
              ea = aq1(ask↓) · 0(ask↑) · Δaq1(ask=)
              VOI = eb − ea
  MLOFI     multi-level OFI — VOI 를 5단 전부에 적용해 합산 (CKS 2014 확장)
  LC_bps    liquidity cost — 주어진 수량을 호가를 걸어 체결할 때의 양방향 평균 비용

★ 정직: 이 지표들은 **호가 스냅샷 사이의 변화**로 정의된다. 우리 quote 는 초
   단위 PK 라 같은 초의 갱신이 덮인다 — 진짜 tick-by-tick 이 아니다. VOI/MLOFI
   는 그만큼 과소 추정된다. 방향 판단에는 쓸 수 있으나 절대 수준은 못 믿는다.

  python tools/microstructure.py                KTB10 지표 + 예측력 검정
  python tools/microstructure.py --instr KTB3
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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
LEVELS = 5


def load_quotes(instr, limit=20000):
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        "SELECT * FROM quote WHERE instr_id=? ORDER BY ts DESC LIMIT ?", (instr, limit)))
    rows.reverse()
    return rows


def compute(rows):
    """호가 스냅샷 열 → 지표 열. 첫 행은 이전 상태가 없어 VOI 가 None."""
    out = []
    prev = None
    for r in rows:
        bp = [r["bp%d" % i] for i in range(1, LEVELS + 1)]
        ap = [r["ap%d" % i] for i in range(1, LEVELS + 1)]
        bq = [r["bq%d" % i] or 0 for i in range(1, LEVELS + 1)]
        aq = [r["aq%d" % i] or 0 for i in range(1, LEVELS + 1)]
        if bp[0] is None or ap[0] is None or ap[0] <= 0:
            prev = None; continue
        mid = (bp[0] + ap[0]) / 2
        tot = bq[0] + aq[0]
        obi = ((bq[0] - aq[0]) / tot) if tot else 0.0
        wb = sum(bq[i] / (i + 1) for i in range(LEVELS))
        wa = sum(aq[i] / (i + 1) for i in range(LEVELS))
        obiw = ((wb - wa) / (wb + wa)) if (wb + wa) else 0.0
        wmid = ((bq[0] * ap[0] + aq[0] * bp[0]) / tot) if tot else mid
        spr_bps = ((ap[0] - bp[0]) / mid) * 1e4 if mid else 0.0
        lo, hi = mid * 0.99, mid * 1.01
        depth = sum(bq[i] for i in range(LEVELS) if bp[i] and bp[i] >= lo) \
              + sum(aq[i] for i in range(LEVELS) if ap[i] and ap[i] <= hi)

        voi = mlofi = None
        if prev is not None:
            def leg(p_now, q_now, p_prev, q_prev, is_bid):
                if p_now is None or p_prev is None:
                    return 0.0
                if p_now > p_prev:
                    return q_now if is_bid else 0.0
                if p_now < p_prev:
                    return 0.0 if is_bid else q_now
                return q_now - q_prev
            eb = leg(bp[0], bq[0], prev["bp"][0], prev["bq"][0], True)
            ea = leg(ap[0], aq[0], prev["ap"][0], prev["aq"][0], False)
            voi = eb - ea
            mlofi = sum(leg(bp[i], bq[i], prev["bp"][i], prev["bq"][i], True)
                        - leg(ap[i], aq[i], prev["ap"][i], prev["aq"][i], False)
                        for i in range(LEVELS))
        out.append(dict(ts=r["ts"], mid=mid, obi=obi, obiw=obiw, wmid=wmid,
                        micro=wmid, spr_bps=spr_bps, depth=depth,
                        voi=voi, mlofi=mlofi,
                        tilt=(wmid - mid)))
        prev = dict(bp=bp, ap=ap, bq=bq, aq=aq)
    return out


def liquidity_cost(r, size):
    """size 계약을 호가를 걸어 체결할 때의 양방향 평균 비용(bps)."""
    bp = [r["bp%d" % i] for i in range(1, LEVELS + 1)]
    ap = [r["ap%d" % i] for i in range(1, LEVELS + 1)]
    bq = [r["bq%d" % i] or 0 for i in range(1, LEVELS + 1)]
    aq = [r["aq%d" % i] or 0 for i in range(1, LEVELS + 1)]
    if bp[0] is None or ap[0] is None:
        return None
    mid = (bp[0] + ap[0]) / 2

    def walk(px, qty):
        left, cost = size, 0.0
        for p, q in zip(px, qty):
            if p is None or left <= 0:
                break
            take = min(left, q)
            cost += take * p
            left -= take
        return None if left > 0 else cost / size      # 호가가 모자라면 산출 불가

    buy, sell = walk(ap, aq), walk(bp, bq)
    if buy is None or sell is None:
        return None
    return ((buy - mid) + (mid - sell)) / 2 / mid * 1e4


def corr(x, y):
    n = len(x)
    if n < 10:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx <= 0 or sy <= 0:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (sx * sy)


def predictive(series, horizon_sec=60):
    """지표가 '다음 구간 mid 변화' 를 맞히는가 — 상관계수로 본다.

    시그널로 쓰려면 지금 값이 앞으로의 움직임과 상관이 있어야 한다.
    같은 시점 상관(동시성)은 예측력이 아니다.
    """
    T = [dt.datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S") for s in series]
    out = {}
    for key in ("obi", "obiw", "voi", "mlofi", "tilt"):
        xs, ys = [], []
        j = 0
        for i in range(len(series)):
            if series[i][key] is None:
                continue
            tgt = T[i] + dt.timedelta(seconds=horizon_sec)
            j = max(j, i)
            while j + 1 < len(T) and T[j] < tgt:
                j += 1
            if T[j] < tgt or j <= i:
                continue
            xs.append(series[i][key])
            ys.append(series[j]["mid"] - series[i]["mid"])
        c = corr(xs, ys)
        out[key] = (c, len(xs))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instr", default="KTB10")
    ap.add_argument("--size", type=float, default=50, help="유동성 비용 계산 수량(계약)")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    rows = load_quotes(a.instr)
    if len(rows) < 20:
        print("%s 호가 %d건 — 20건 미만이라 계산하지 않습니다." % (a.instr, len(rows)))
        return 1
    S = compute(rows)
    if not S:
        print("유효한 호가가 없습니다."); return 1
    last, lastrow = S[-1], rows[-1]
    lc = liquidity_cost(lastrow, a.size)
    print("=== %s 오더북 지표 · 호가 %d건 (%s ~ %s) ==="
          % (a.instr, len(rows), rows[0]["ts"], rows[-1]["ts"]))
    print("  현재 mid %.4f · wmid(=Micro-Price 근사) %.4f · 기울기 %+.4f"
          % (last["mid"], last["wmid"], last["tilt"]))
    print("  OBI %+.3f · 가중 OBI %+.3f · spread %.2f bps · ±1%% 깊이 %.0f"
          % (last["obi"], last["obiw"], last["spr_bps"], last["depth"]))
    print("  VOI %s · MLOFI %s · 유동성 비용(%.0f계약) %s"
          % (("%+.0f" % last["voi"]) if last["voi"] is not None else "—",
             ("%+.0f" % last["mlofi"]) if last["mlofi"] is not None else "—",
             a.size, ("%.2f bps" % lc) if lc is not None else "호가 부족"))

    print("\n=== 예측력 — 지표가 60초 뒤 mid 변화를 맞히는가 ===")
    print("  %-8s %10s %8s  %s" % ("지표", "상관계수", "표본", "판정"))
    pr = predictive(S, 60)
    for k, (c, n) in pr.items():
        if c is None:
            print("  %-8s %10s %8d  표본 부족" % (k, "—", n)); continue
        verdict = ("의미 있음" if abs(c) >= 0.2 else
                   "약함" if abs(c) >= 0.1 else "없음")
        print("  %-8s %+10.3f %8d  %s" % (k, c, n, verdict))
    print("\n  [주의] 우리 호가는 초 단위 PK 라 같은 초의 갱신이 덮인다.")
    print("         진짜 tick-by-tick 이 아니므로 VOI/MLOFI 는 과소 추정된다.")
    print("         상관이 약해도 '지표가 쓸모없다' 가 아니라 '해상도가 모자라다' 일 수 있다.")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"instr": a.instr, "n_quotes": len(rows),
             "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "last": {k: v for k, v in last.items()},
             "liquidity_cost_bps": lc, "size": a.size,
             "predictive": {k: {"corr": c, "n": n} for k, (c, n) in pr.items()},
             "series": S[-300:]}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n  wrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
