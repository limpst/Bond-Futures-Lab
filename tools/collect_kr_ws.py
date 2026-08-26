# -*- coding: utf-8 -*-
"""KTB 선물 1분봉 수집기 — LS 실시간 WebSocket(FC9) → minbars.db

왜 필요한가: REST 폴링(t8461)은 5분마다 한 번 훑는 방식이라 그 사이 체결을
놓친다. 실시간 tick 을 받으면 체결 하나하나가 들어온다.

★ 체결(FC9) + 호가 5단(FH9) 을 함께 받는다. 호가가 있어야 OBI·VOI·MLOFI·
  Micro-Price 같은 오더북 지표를 계산할 수 있다.

★ 주간 세션에만 흐른다 (2026-08-25 실측)
  09:00~15:45 → FC9 tick 정상 (KTB10 44건/30초 · KTB3 12건/30초)
  18:00~05:00 → tick 0건. 야간은 REST t8461 폴링으로 받아야 한다.
  어제 야간에만 시험하고 "국내 실시간 미신청" 이라고 잘못 결론냈던 자리다.

필드 (FC9 body, 실측):
  futcode  종목코드 · price 체결가 · chetime 체결시각 HHMMSS(KST)
  cvolume  이번 체결 수량 · volume 누적 거래량 · high/low 당일 고저

거래량은 cvolume 합으로 센다. 누적(volume) 차분이 아니라서 첫 봉도
과소계상되지 않는다 (CME 쪽 collect_cme_ws.py 의 알려진 한계를 여기선 피함).

조회 전용 — 주문 TR 은 건드리지 않는다.

  python tools/collect_kr_ws.py --minutes 300
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
import asyncio
import json
import sqlite3
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ls_openapi import issue_token  # noqa: E402

DB = ROOT / "data" / "minbars.db"
WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
CHANNEL = "kr_futopt"
TR_EXEC = "FC9"      # 체결
TR_QUOTE = "FH9"     # 호가 5단


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


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


QUOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote(
  instr_id TEXT NOT NULL,
  ts       TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS' KST (호가 시각)
  bp1 REAL, bp2 REAL, bp3 REAL, bp4 REAL, bp5 REAL,
  bq1 REAL, bq2 REAL, bq3 REAL, bq4 REAL, bq5 REAL,
  ap1 REAL, ap2 REAL, ap3 REAL, ap4 REAL, ap5 REAL,
  aq1 REAL, aq2 REAL, aq3 REAL, aq4 REAL, aq5 REAL,
  totbid REAL, totoffer REAL,
  symbol TEXT, collected_utc TEXT NOT NULL,
  PRIMARY KEY(instr_id, ts)
)"""


def open_db() -> sqlite3.Connection:
    c = _sqlite_connect_safe(DB, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute(QUOTE_SCHEMA)
    c.commit()
    return c


def save_quote(con, iid, sym, b, dry):
    """FH9 스냅샷 1건. 초 단위 PK 라 같은 초의 갱신은 마지막 값으로 덮인다
       — 1초에 수십 번 오는 호가를 전부 남기면 DB 가 금방 부푼다."""
    if dry:
        return 0
    t = str(b.get("hotime") or "").zfill(6)
    if len(t) < 6:
        return 0
    ts = "%s %s:%s:%s" % (dt.date.today().isoformat(), t[:2], t[2:4], t[4:6])
    g = lambda k: _f(b.get(k))
    con.execute(
        "INSERT INTO quote VALUES(" + ",".join(["?"] * 26) + ")"
        " ON CONFLICT(instr_id,ts) DO UPDATE SET"
        " bp1=excluded.bp1,bq1=excluded.bq1,ap1=excluded.ap1,aq1=excluded.aq1,"
        " totbid=excluded.totbid,totoffer=excluded.totoffer,"
        " collected_utc=excluded.collected_utc",
        [iid, ts] + [g("bidho%d" % i) for i in range(1, 6)]
                  + [g("bidrem%d" % i) for i in range(1, 6)]
                  + [g("offerho%d" % i) for i in range(1, 6)]
                  + [g("offerrem%d" % i) for i in range(1, 6)]
                  + [g("totbidrem"), g("totofferrem"), sym, now_utc()])
    return 1


def targets(con) -> dict[str, str]:
    """symbol -> instr_id (KRX 활성 종목)."""
    return {r["symbol"]: r["instr_id"] for r in con.execute(
        "SELECT instr_id, symbol FROM instrument "
        "WHERE market='KRX' AND active=1 AND symbol IS NOT NULL")}


def _f(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


class Bars:
    def __init__(self):
        self.cur: dict[str, dict] = {}

    def add(self, iid, sym, key, px, qty):
        done = None
        b = self.cur.get(iid)
        if b and b["bar_time"] != key:
            done, b = b, None
        if b is None:
            b = {"instr_id": iid, "bar_time": key, "symbol": sym,
                 "open": px, "high": px, "low": px, "close": px, "volume": 0.0}
            self.cur[iid] = b
        b["high"] = max(b["high"], px)
        b["low"] = min(b["low"], px)
        b["close"] = px
        b["volume"] += qty or 0.0
        return done

    def flush(self):
        out = list(self.cur.values())
        self.cur.clear()
        return out


def save(con, bars, dry):
    if not bars or dry:
        return 0
    ts = now_utc()
    con.executemany(
        "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,symbol,collected_utc)"
        " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(instr_id,bar_time) DO UPDATE SET"
        " open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,"
        " volume=MAX(minbar.volume, excluded.volume), symbol=excluded.symbol,"
        " collected_utc=excluded.collected_utc",
        [(b["instr_id"], b["bar_time"], b["open"], b["high"], b["low"], b["close"],
          b["volume"], b["symbol"], ts) for b in bars])
    con.commit()
    return len(bars)


async def collect(minutes: int, dry: bool) -> int:
    import websockets
    con = open_db()
    tg = targets(con)
    if not tg:
        print("instrument 테이블에 KRX 활성 종목이 없습니다.")
        return 1
    print("KTB 1분봉 실시간 수집 · %d분 · 대상 %d종: %s"
          % (minutes, len(tg), " ".join(sorted(tg))))
    bars = Bars()
    n_tick = n_saved = n_quote = 0
    end = asyncio.get_event_loop().time() + minutes * 60
    backoff = 3
    while asyncio.get_event_loop().time() < end:
        try:
            tok = issue_token(CHANNEL, force=True)
            async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
                for sym in sorted(tg):
                    for tr in (TR_EXEC, TR_QUOTE):
                        await ws.send(json.dumps({
                            "header": {"token": tok, "tr_type": "3"},
                            "body": {"tr_cd": tr, "tr_key": sym.ljust(8)}}))
                        await asyncio.sleep(0.15)
                print("구독 완료 — 수신 시작")
                backoff = 3
                while asyncio.get_event_loop().time() < end:
                    left = end - asyncio.get_event_loop().time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(60, max(1, left)))
                    m = json.loads(raw)
                    b = m.get("body") or {}
                    sym = str(b.get("futcode") or "").strip()
                    if sym not in tg:
                        continue
                    # ★ FC9 체결 payload 에도 bidho1/offerho1 이 들어 있다(실측).
                    #   bidho1 로 가르면 체결이 전부 호가로 오분류되어 봉이 하나도
                    #   안 만들어진다(2026-08-26 실측: 호가 619건 · 봉 0개).
                    #   체결에만 있는 chetime/price 를 먼저 보고, 없으면 호가로 본다.
                    if "price" in b and "chetime" in b:        # FC9 체결
                        pass
                    elif "bidho2" in b:                        # FH9 호가 5단
                        n_quote += save_quote(con, tg[sym], sym, b, dry)
                        if n_quote % 200 == 0:
                            con.commit()
                        continue
                    else:
                        continue
                    px = _f(b.get("price"))
                    t = str(b.get("chetime") or "").zfill(6)
                    if px is None or len(t) < 4:
                        continue
                    # chetime 은 KST 시각만 온다. 자정을 넘기는 야간은 날짜를
                    # 하루 더해야 하지만, FC9 는 주간에만 흐르므로 오늘로 충분하다.
                    d = dt.date.today()
                    key = "%s %s:%s" % (d.isoformat(), t[:2], t[2:4])
                    n_tick += 1
                    done = bars.add(tg[sym], sym, key, px, _f(b.get("cvolume")))
                    if done:
                        n_saved += save(con, [done], dry)
                        print("  %-6s %s  O=%s H=%s L=%s C=%s V=%.0f"
                              % (done["instr_id"], done["bar_time"], done["open"],
                                 done["high"], done["low"], done["close"], done["volume"]))
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            if asyncio.get_event_loop().time() >= end:
                break
            print("  연결 끊김 (%s) — %d초 후 재접속" % (str(e)[:60], backoff))
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)
    n_saved += save(con, bars.flush(), dry)
    con.execute("INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_utc(), "KR_WS", TR_EXEC + "+" + TR_QUOTE, n_tick, n_saved, "dry" if dry else "ok",
                 "%d종 %d분" % (len(tg), minutes)))
    con.commit()
    print("\ntick %d건 · 저장 %d봉" % (n_tick, n_saved))
    for r in con.execute(
            "SELECT instr_id, COUNT(*) n, MIN(bar_time) a, MAX(bar_time) b FROM minbar"
            " WHERE instr_id IN (SELECT instr_id FROM instrument WHERE market='KRX')"
            " GROUP BY instr_id ORDER BY instr_id"):
        print("  %-6s %5d봉  %s ~ %s" % (r["instr_id"], r["n"], r["a"], r["b"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return asyncio.run(collect(a.minutes, a.dry_run))


if __name__ == "__main__":
    sys.exit(main())
