# -*- coding: utf-8 -*-
"""USDKRW 1분봉 수집 — FX 다리 (LS 로는 받을 수 없어 외부 소스를 쓴다).

■ 왜 외부 소스인가 (2026-08-26 실측)
  · 국내선옵 마스터 t8435: gubun 10종을 훑어도 통화선물 없음.
  · 해외선옵 마스터 o3121: 취급 12상품, FX 는 CNH 하나뿐 — 원화 없음.
  · LS WebSocket(OVC) 로 6KU26·6KZ26·6EU26 을 직접 구독 → **전부 무응답**
    (구독 자체는 "정상처리" 응답). 6E 는 세계 최대 유동성 통화선물이므로
    '거래가 없어서' 가 아니라 **LS 가 전달하지 않는다**고 판단.
  → 그래서 FX 만 외부에서 받는다.

■ 소스 순서
  1) yfinance `KRW=X` 1분봉 — 무료·키 불필요·최근 7일 backfill 가능.
     ★ 채권 다리(LS)와 **소스가 다르다**. 그래서 같은 시계열에 섞지 않고
       instr_id='USDKRW' 로 따로 저장하고, symbol 에 소스를 남긴다.
  2) Alpha Vantage `CURRENCY_EXCHANGE_RATE` — 키 필요(PoC .env 재사용).
     스냅샷 1점만 주므로 yfinance 실패 시 '현재 분' 을 메우는 용도.

■ 주의 — 이 데이터로 무엇을 할 수 있고 없나
  · 신호(금리 상대가치)는 FX 없이도 성립한다. FX 는 **손익 계산** 용이다.
  · yfinance FX 는 은행간 호가 기반이라 체결 기준 선물과 미세하게 다르다.
    정밀 손익이 필요하면 브로커 실체결 환율로 대체해야 한다 — 화면에 명시.

  python tools/collect_fx.py               최근 2일 1분봉 수집(멱등)
  python tools/collect_fx.py --days 7      최대 backfill (yfinance 상한)
  python tools/collect_fx.py --spot        AV 스냅샷만
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
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
INSTR = "USDKRW"
YF_TICKER = "KRW=X"
KST = dt.timezone(dt.timedelta(hours=9))


def con_rw():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def ensure_instrument(con, source):
    """active=0 으로 넣는다 — LS 수집기(collect_minbars)가 active=1 만 훑기 때문에
    여기에 1 을 주면 LS 쪽에서 채널을 못 찾아 죽는다."""
    con.execute(
        "INSERT INTO instrument(instr_id,market,channel,name,underlying,symbol,active,updated_utc)"
        " VALUES(?,?,?,?,?,?,0,?)"
        " ON CONFLICT(instr_id) DO UPDATE SET symbol=excluded.symbol,"
        " channel=excluded.channel, updated_utc=excluded.updated_utc",
        (INSTR, "FX", source, "USD/KRW spot", "USDKRW", YF_TICKER,
         dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()


def save_bars(con, rows, source):
    """rows: [(bar_time_kst_str, o,h,l,c,v)] — 멱등 upsert."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for bt, o, h, l, c, v in rows:
        cur = con.execute(
            "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,symbol,collected_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(instr_id,bar_time) DO UPDATE SET"
            " open=excluded.open,high=excluded.high,low=excluded.low,"
            " close=excluded.close,volume=excluded.volume,collected_utc=excluded.collected_utc",
            (INSTR, bt, o, h, l, c, v, source, now))
        n += cur.rowcount
    con.commit()
    return n


def from_yfinance(days: int):
    import yfinance as yf
    period = f"{max(1, min(days, 7))}d"
    df = yf.download(YF_TICKER, period=period, interval="1m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return []
    out = []
    for ts, r in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:                       # naive 면 UTC 로 본다
            t = t.replace(tzinfo=dt.timezone.utc)
        k = t.astimezone(KST)
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
        out.append((k.strftime("%Y-%m-%d %H:%M"), o, h, l, c, v))
    return out


def from_alphavantage():
    import requests
    key = ""
    for envf in (Path(r"C:\Users\leeli\Downloads\PoC-proto\.env"),
                 ROOT / ".env.ls"):
        if envf.is_file():
            for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ALPHAVANTAGE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    key = os.environ.get("ALPHAVANTAGE_API_KEY", key)
    if not key:
        raise RuntimeError("ALPHAVANTAGE_API_KEY 없음")
    r = requests.get("https://www.alphavantage.co/query",
                     params={"function": "CURRENCY_EXCHANGE_RATE",
                             "from_currency": "USD", "to_currency": "KRW",
                             "apikey": key}, timeout=25)
    j = r.json().get("Realtime Currency Exchange Rate") or {}
    px = float(j.get("5. Exchange Rate", 0) or 0)
    if px <= 0:
        raise RuntimeError(f"AV 응답 이상: {str(j)[:120]}")
    now_k = dt.datetime.now(KST).replace(second=0, microsecond=0)
    return [(now_k.strftime("%Y-%m-%d %H:%M"), px, px, px, px, 0.0)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="yfinance 조회 일수(최대 7)")
    ap.add_argument("--spot", action="store_true", help="Alpha Vantage 스냅샷만")
    a = ap.parse_args()

    con = con_rw()
    rows, source = [], ""
    if not a.spot:
        try:
            rows = from_yfinance(a.days)
            source = "yfinance:KRW=X"
            print(f"[fx] yfinance {a.days}일 · {len(rows)}봉")
        except Exception as e:                                   # noqa: BLE001
            print(f"[fx] yfinance 실패 — {e}")
    if not rows:
        try:
            rows = from_alphavantage()
            source = "alphavantage:spot"
            print(f"[fx] Alpha Vantage 스냅샷 {rows[0][4] if rows else '-'}")
        except Exception as e:                                   # noqa: BLE001
            print(f"[fx] Alpha Vantage 실패 — {e}")
    if not rows:
        print("[fx] 두 소스 모두 실패 — 저장할 것 없음")
        return 1
    ensure_instrument(con, source)
    n = save_bars(con, rows, source)
    tot, lo, hi = con.execute(
        "SELECT COUNT(*), MIN(bar_time), MAX(bar_time) FROM minbar WHERE instr_id=?",
        (INSTR,)).fetchone()
    print(f"[fx] 신규 {n}봉 · 누적 {tot}봉 · {lo} ~ {hi} (KST, source={source})")
    # 채권 다리와 겹치는 분 (손익 환산 가능 구간)
    ov = con.execute(
        "SELECT COUNT(*) FROM minbar x JOIN minbar y ON x.bar_time=y.bar_time"
        " WHERE x.instr_id='ZN' AND y.instr_id=?", (INSTR,)).fetchone()[0]
    print(f"[fx] ZN 과 겹치는 분: {ov}")
    con.close()
    return 0


if __name__ == "__main__":
    _sys.exit(main())
