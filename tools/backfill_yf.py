# -*- coding: utf-8 -*-
"""CME 결측 복구 — yfinance 1분봉으로 빠진 분만 메운다.

■ 왜 이게 가능한가 (2026-08-26 실측, 앞선 판단을 뒤집는 결과)
  나는 어제 "CME 는 LS REST 가 아무것도 주지 않으므로 수집기가 꺼져 있던
  시간은 **영구 결측**" 이라고 적었다. LS 안에서는 맞다. 그러나 **밖에**
  소스가 있다:

      yfinance ZN=F · 7일치 1분봉 6,581 봉
      LS ZN(ZNU26) 과 겹치는 429 봉에서
        가격차 중앙값 0.000000 · 평균 0.000036 · 최대 0.015625 (= 1/64, 반 틱)

  즉 두 소스는 사실상 같은 값을 준다. 그래서 **빠진 분만** yfinance 로 채운다.

■ 섞이지 않게 하는 규칙
  1) LS 가 이미 넣은 봉은 **절대 덮지 않는다** (INSERT ... DO NOTHING).
  2) 출처를 행마다 남긴다 — symbol 열에 'yfinance:ZN=F' 로 기록해 두면
     나중에 "이 봉은 어디서 왔나" 를 언제든 되물을 수 있다.
  3) 연속물(ZN=F)과 특정 월물(ZNU26)은 롤오버 시점에 갈라진다. 그래서 채운
     뒤에도 겹침 구간의 가격차를 매번 재보고, 임계(기본 0.05)를 넘으면
     경고한다 — 조용히 오염되는 것을 막는다.

  python tools/backfill_yf.py              결측만 채움 (기본 7일)
  python tools/backfill_yf.py --dry        무엇을 채울지만 보기
  python tools/backfill_yf.py --syms ZN,ZB 대상 지정
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
KST = dt.timezone(dt.timedelta(hours=9))

# instr_id -> yfinance 티커 (CME 채권선물 연속물)
YF = {"ZN": "ZN=F", "ZB": "ZB=F", "ZF": "ZF=F", "ZT": "ZT=F", "TN": "TN=F"}
DIFF_WARN = 0.05          # 겹침 구간 가격차가 이보다 크면 경고


def con_rw():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def fetch(ticker, days):
    import yfinance as yf
    df = yf.download(ticker, period=f"{max(1, min(days, 7))}d", interval="1m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return {}
    out = {}
    for ts, r in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        k = t.astimezone(KST).strftime("%Y-%m-%d %H:%M")

        def g(col):
            v = r[col]
            try:
                return float(v.iloc[0] if hasattr(v, "iloc") else v)
            except Exception:
                return None
        o, h, l, c = g("Open"), g("High"), g("Low"), g("Close")
        v = g("Volume") or 0.0
        if None in (o, h, l, c) or c <= 0:
            continue
        out[k] = (o, h, l, c, v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--syms", default=",".join(YF))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    syms = [s.strip().upper() for s in a.syms.split(",") if s.strip()]

    con = con_rw()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    grand_new = 0
    for iid in syms:
        tk = YF.get(iid)
        if not tk:
            print(f"[yf] {iid}: 티커 매핑 없음 — 건너뜀")
            continue
        try:
            bars = fetch(tk, a.days)
        except Exception as e:                                   # noqa: BLE001
            print(f"[yf] {iid} ({tk}) 조회 실패 — {e}")
            continue
        if not bars:
            print(f"[yf] {iid} ({tk}): 응답 없음")
            continue
        have = {r[0]: r[1] for r in con.execute(
            "SELECT bar_time, close FROM minbar WHERE instr_id=?", (iid,))}
        common = sorted(set(bars) & set(have))
        diffs = sorted(abs(bars[k][3] - have[k]) for k in common)
        med = diffs[len(diffs) // 2] if diffs else None
        mx = diffs[-1] if diffs else None
        missing = sorted(set(bars) - set(have))
        print(f"[yf] {iid} ({tk}): yf {len(bars)}봉 · 보유 {len(have)}봉 · "
              f"겹침 {len(common)} (차 중앙 {med if med is None else round(med,6)} / "
              f"최대 {mx if mx is None else round(mx,6)}) · 결측 {len(missing)}봉")
        if mx is not None and mx > DIFF_WARN:
            print(f"     ⚠ 겹침 가격차 최대 {mx:.4f} > {DIFF_WARN} — 월물 불일치 의심, 채우지 않음")
            continue
        if a.dry or not missing:
            continue
        n = 0
        for k in missing:
            o, h, l, c, v = bars[k]
            cur = con.execute(
                "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,"
                "symbol,collected_utc) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instr_id,bar_time) DO NOTHING",
                (iid, k, o, h, l, c, v, f"yfinance:{tk}", now))
            n += cur.rowcount
        con.commit()
        grand_new += n
        print(f"     → {n}봉 채움 (출처 표시 yfinance:{tk})")

    ov = con.execute(
        "SELECT COUNT(*) FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id='KTB10' AND y.instr_id='ZN'").fetchone()[0]
    src = dict(con.execute(
        "SELECT CASE WHEN symbol LIKE 'yfinance:%' THEN 'yfinance' ELSE 'LS' END, COUNT(*)"
        " FROM minbar WHERE instr_id='ZN' GROUP BY 1"))
    print(f"\n[yf] 총 {grand_new}봉 채움 · ZN 출처 구성 {src} · KTB10-ZN 겹침 {ov}")
    con.close()
    return 0


if __name__ == "__main__":
    _sys.exit(main())
