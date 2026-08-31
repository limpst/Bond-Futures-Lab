# -*- coding: utf-8 -*-
"""CME 채권 선물 1분봉 수집기 — LS 실시간 WebSocket(OVC) → minbars.db

왜 WebSocket 인가: 같은 계정에서 CME 는 REST 조회 계열이 아무것도 주지 않는다
(o3121 마스터에 CME 없음 · o3106 현재가 빈 응답 · o3103 차트 0봉, 2026-08-24 실측).
반면 실시간 WebSocket OVC/OVH 는 정상 수신된다. 그래서 tick 을 직접 받아
1분봉으로 집계한다.

  · 소스   : wss://openapi.ls-sec.co.kr:9443/websocket · TR `OVC`(해외선물 체결)
  · 대상   : instrument 테이블의 market='CME' 활성 종목 (ZT · ZF · ZN · ZB · TN)
  · 저장   : minbar(instr_id, bar_time, o,h,l,c,volume, symbol) — PK 로 멱등 upsert

★ bar_time 은 KST 로 저장한다.
  minbar 스키마 주석은 '거래소 현지시간' 이지만, 이 수집기의 목적은 KTB(=KST)
  와 같은 시계 위에서 pair 를 만드는 것이다. 두 다리를 서로 다른 시계로 저장하면
  조인할 때마다 변환이 필요하고 실수가 난다. 대신 meta 에 근거를 남긴다.
  (LS 가 tick 마다 kordate/kortm 를 함께 주므로 변환 없이 그대로 쓴다.)

volume: OVC 의 totq 는 '세션 누적' 계약수다. 봉 거래량 = 구간 내 totq 증가분.
  세션이 바뀌면 totq 가 되감기므로, 감소를 감지하면 그 값을 그대로 봉 거래량으로 쓴다.

조회 전용 — 주문 TR 은 건드리지 않는다.

  python tools/collect_cme_ws.py --minutes 60
  python tools/collect_cme_ws.py --minutes 5 --dry-run
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
CHANNEL = "os_futopt"
TR = "OVC"


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


def open_db() -> sqlite3.Connection:
    c = _sqlite_connect_safe(DB, timeout=60)
    c.row_factory = sqlite3.Row
    return c


# 이 수집기가 담당하는 시장. 전부 같은 TR(OVC) 한 줄로 들어오므로 거래소가
# 늘어도 코드가 갈라지지 않는다. EUREX 는 2026-08-27 실측으로 추가
# (FGBL 124.00 · FGBM 113.72 · FGBS 105.445 수신 확인 — probe_g7_bonds_ws.py).
WS_MARKETS = ("CME", "EUREX")
_MK = ",".join("'%s'" % m for m in WS_MARKETS)


def targets(con, markets=WS_MARKETS) -> dict[str, str]:
    """symbol -> instr_id (해외 WS 활성 종목)."""
    q = ",".join("?" * len(markets))
    return {r["symbol"]: r["instr_id"] for r in con.execute(
        "SELECT instr_id, symbol FROM instrument "
        "WHERE market IN (%s) AND active=1 AND symbol IS NOT NULL" % q,
        tuple(markets))}


def _f(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


class Bars:
    """종목별 현재 진행 봉을 들고 있다가, 분이 바뀌면 완성 봉을 뱉는다."""

    def __init__(self):
        self.cur: dict[str, dict] = {}     # instr_id -> bar dict
        self.last_totq: dict[str, float] = {}

    def add(self, instr_id: str, symbol: str, bar_key: str,
            px: float, totq: float | None) -> dict | None:
        done = None
        b = self.cur.get(instr_id)
        if b and b["bar_time"] != bar_key:
            done = b
            b = None
        if b is None:
            b = {"instr_id": instr_id, "bar_time": bar_key, "symbol": symbol,
                 "open": px, "high": px, "low": px, "close": px, "volume": 0.0}
            self.cur[instr_id] = b
        b["high"] = max(b["high"], px)
        b["low"] = min(b["low"], px)
        b["close"] = px
        if totq is not None:
            prev = self.last_totq.get(instr_id)
            if prev is None:
                pass                        # 첫 tick — 증가분을 알 수 없다
            elif totq >= prev:
                b["volume"] += totq - prev
            else:
                b["volume"] += totq         # 세션 되감김
            self.last_totq[instr_id] = totq
        return done

    def flush_all(self) -> list[dict]:
        out = list(self.cur.values())
        self.cur.clear()
        return out


def save(con, bars: list[dict], dry: bool) -> int:
    if not bars or dry:
        return 0
    ts = now_utc()
    con.executemany(
        "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,symbol,collected_utc)"
        " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(instr_id,bar_time) DO UPDATE SET"
        " open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,"
        " volume=excluded.volume, symbol=excluded.symbol, collected_utc=excluded.collected_utc",
        [(b["instr_id"], b["bar_time"], b["open"], b["high"], b["low"], b["close"],
          b["volume"], b["symbol"], ts) for b in bars])
    con.commit()
    return len(bars)


async def collect(minutes: int, dry: bool) -> int:
    try:
        import websockets
    except ImportError:
        print("websockets 미설치 — pip install websockets")
        return 1
    con = open_db()
    tg = targets(con)
    if not tg:
        print("instrument 테이블에 해외 WS 활성 종목이 없습니다 (%s)."
              % ", ".join(WS_MARKETS))
        return 1
    # 이 meta 한 줄 때문에 수집이 죽으면 안 된다 — 자료가 아니라 메모다.
    # 다른 수집기가 쓰기 트랜잭션을 붙들고 있으면 여기서 'database is locked'
    # 가 나고, 예전 코드는 그대로 종료됐다(2026-08-27 실측: backfill 이 락을
    # 잡은 사이 CME 수집기가 시작조차 못 함). 실패해도 수집은 계속한다.
    try:
        con.execute("INSERT INTO meta(k,v) VALUES('cme_bar_timezone','KST (kordate+kortm) "
                    "— KTB 와 같은 시계로 pair 를 만들기 위함')"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v")
        con.commit()
    except sqlite3.Error as e:
        print("  (meta 기록 건너뜀 — %s)" % str(e)[:80])

    print("해외 1분봉 수집(%s) · %d분 · 대상 %d종: %s"
          % ("+".join(WS_MARKETS), minutes, len(tg), " ".join(sorted(tg))))
    if dry:
        print("(dry-run — DB 에 쓰지 않습니다)")

    bars = Bars()
    n_tick = n_saved = 0
    end = asyncio.get_event_loop().time() + minutes * 60
    backoff = 3
    try:
        while asyncio.get_event_loop().time() < end:
            try:
                # ★ 매 접속마다 토큰을 강제 재발급한다 (2026-08-27).
                #   LS access token 은 하루 단위로 죽고, expires_in 을 그대로
                #   믿을 수 없다(ls_openapi.call_tr 의 IGW00121 주석 참고).
                #   REST 는 거부당하면 재발급 훅이 돌지만 WebSocket 은 그런
                #   훅이 없어 조용히 끊긴다 — 08-24·08-25·08-26 사흘 연속
                #   야간 수집이 정확히 여기서 멈췄다(최대 965분 결측).
                tok = issue_token(CHANNEL, force=True)
                async with websockets.connect(WS_URL, ping_interval=20,
                                              close_timeout=5) as ws:
                    for sym in sorted(tg):
                        await ws.send(json.dumps({
                            "header": {"token": tok, "tr_type": "3"},
                            "body": {"tr_cd": TR, "tr_key": sym.ljust(8)}}))
                        await asyncio.sleep(0.2)
                    print("구독 완료 — 수신 시작 (Ctrl+C 로 중단, 진행 봉은 저장됩니다)")
                    backoff = 3
                    while asyncio.get_event_loop().time() < end:
                        left = end - asyncio.get_event_loop().time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(),
                                                         timeout=min(30, max(1, left)))
                        except asyncio.TimeoutError:
                            continue
                        try:
                            m = json.loads(raw)
                        except ValueError:
                            continue
                        b = m.get("body") or {}
                        sym = str(b.get("symbol") or "").strip()
                        if sym not in tg or "curpr" not in b:
                            continue
                        px = _f(b.get("curpr"))
                        if px is None:
                            continue
                        kd = str(b.get("kordate") or "").strip()   # YYYYMMDD (KST)
                        kt = str(b.get("kortm") or "").strip()     # HHMMSS  (KST)
                        if len(kd) != 8 or len(kt) < 4:
                            continue
                        bar_key = "%s-%s-%s %s:%s" % (kd[:4], kd[4:6], kd[6:8],
                                                      kt[:2], kt[2:4])
                        n_tick += 1
                        done = bars.add(tg[sym], sym, bar_key, px, _f(b.get("totq")))
                        if done:
                            n_saved += save(con, [done], dry)
                            print("  %-5s %s  O=%s H=%s L=%s C=%s V=%.0f"
                                  % (done["instr_id"], done["bar_time"], done["open"],
                                     done["high"], done["low"], done["close"],
                                     done["volume"]))
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if asyncio.get_event_loop().time() >= end:
                    break
                # 진행 중이던 봉을 흘리지 않는다 — 재접속 전에 flush
                n_saved += save(con, bars.flush(), dry)
                print("  연결 끊김 (%s) — %d초 후 재접속(토큰 재발급)"
                      % (str(e)[:60], backoff))
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
    except KeyboardInterrupt:
        print("\n중단 요청 — 진행 중인 봉을 저장합니다")
    finally:
        rest = bars.flush_all()
        n_saved += save(con, rest, dry)
        con.execute("INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (now_utc(), "CME_WS", TR, n_tick, n_saved,
                     "dry" if dry else "ok", "%d종 %d분" % (len(tg), minutes)))
        con.commit()
    print("\ntick %d건 · 저장 %d봉" % (n_tick, n_saved))
    for r in con.execute(
            "SELECT instr_id, COUNT(*) n, MIN(bar_time) a, MAX(bar_time) b FROM minbar "
            "WHERE instr_id IN (SELECT instr_id FROM instrument "
            "                   WHERE market IN (" + _MK + ")) "
            "GROUP BY instr_id ORDER BY instr_id"):
        print("  %-5s %4d봉  %s ~ %s" % (r["instr_id"], r["n"], r["a"], r["b"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return asyncio.run(collect(a.minutes, a.dry_run))


if __name__ == "__main__":
    sys.exit(main())
