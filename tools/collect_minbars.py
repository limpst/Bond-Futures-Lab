# -*- coding: utf-8 -*-
"""채권 선물 1분봉 수집기 — KTB(국내) + CME FI futures(해외) → SQLite.

실거래 트랙(delta-one 전략용)의 데이터 층이다. 유니버스는 **거래소 상장
채권 선물**: KTB 3/10년(KRX) + CME 미 국채 선물 ZT/ZF/ZN/ZB (US 2/5/10/30Y).

★ 기본 모드 = dry-run (2026-08-23 지시). 네트워크·DB 쓰기 없이 수집 계획과
  자격증명 존재 여부만 출력한다. 실제 동작은 명시 플래그로만:
    python tools/collect_minbars.py                    # dry-run (기본)
    python tools/collect_minbars.py --issue-token      # 채널별 토큰 발급 시험
    python tools/collect_minbars.py --init-db          # 스키마만 생성
    python tools/collect_minbars.py --discover         # 종목 마스터 조회(월물 코드 탐색)
    python tools/collect_minbars.py --live             # 1분봉 수집 → data/minbars.db
    python tools/collect_minbars.py --live --minutes 1 --count 500

★ 조회·수집 전용 — 주문/체결 API 는 어디에도 없다.

TR 코드·경로는 LS OpenAPI 문서 기준의 초안이며 CONFIG 에 모아 두었다.
실호출에서 코드가 다르면 rsp 메시지가 그대로 collect_log 에 남으므로,
CONFIG 만 고치면 된다 (코드 수정 불필요).

DB: data/minbars.db (★ 1분봉은 무한히 자라므로 .gitignore 대상 — 무거운
생성물 gitignore 규칙). 스키마는 아래 SCHEMA 참조. 대시보드 SQL 콘솔
연결은 수집 안정화 후 별도 작업.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from ls_openapi import CHANNELS, call_tr, issue_token, load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"

# ── 수집 유니버스 (delta-one 채권 선물) ──────────────────────────────────────
#   KRX 월물 코드는 t8426 상품선물마스터 실조회로 확보 (2026-08-23, 2026-09물):
#     3년 A6569000 · 5년 A6669000 · 10년 A6769000 · 30년 A7069000
#   롤오버 시 --discover 로 재확인. CME 심볼은 globex 규칙(상품+월문자+연도 2자리)
#   추정 — 첫 --live 가 검증하고, 틀리면 collect_log 에 에러가 남는다.
INSTRUMENTS = [
    {"instr_id": "KTB3",  "market": "KRX", "channel": "kr_futopt",
     "name": "KTB 3Y futures",  "underlying": "국고채 3년 바스켓",  "symbol": "A6569000"},
    {"instr_id": "KTB10", "market": "KRX", "channel": "kr_futopt",
     "name": "KTB 10Y futures", "underlying": "국고채 10년 바스켓", "symbol": "A6769000"},
    {"instr_id": "ZT", "market": "CME", "channel": "os_futopt",
     "name": "2-Year T-Note futures",  "underlying": "US Treasury 2Y",  "symbol": "ZTU26"},
    {"instr_id": "ZF", "market": "CME", "channel": "os_futopt",
     "name": "5-Year T-Note futures",  "underlying": "US Treasury 5Y",  "symbol": "ZFU26"},
    {"instr_id": "ZN", "market": "CME", "channel": "os_futopt",
     "name": "10-Year T-Note futures", "underlying": "US Treasury 10Y", "symbol": "ZNU26"},
    {"instr_id": "ZB", "market": "CME", "channel": "os_futopt",
     "name": "30-Year T-Bond futures", "underlying": "US Treasury 30Y", "symbol": "ZBU26"},
]

# ── TR 설정 (LS OpenAPI — 문서 대조 후 필요 시 여기만 수정) ──────────────────
CONFIG = {
    "kr_futopt": {
        # t8461 = 주간/야간 통합 선물옵션 차트 — 실검증 완료 (2026-08-23):
        #   cgubun="B", bgubun=분단위 → OutBlock1 rows (chetime/open/high/low/price/volume)
        #   ★ 날짜 필드가 없다 — 최근 세션분만 오므로 수집기가 세션 날짜를 합성한다
        #     (자정 걸친 야간 세션은 v0 근사 — 한계 명시). t8415/t8414/t2209 는
        #     REST 게이트웨이가 거부(IGW00215)함을 실측으로 확인.
        "chart": {"path": "/futureoption/chart", "tr_cd": "t8461",
                  "in_block": "t8461InBlock", "out_block": "t8461OutBlock1",
                  "fields": {"date": None, "time": "chetime", "open": "open",
                             "high": "high", "low": "low", "close": "price",
                             "volume": "volume"}},
        "master": {"path": "/futureoption/market-data", "tr_cd": "t8435",
                   "in_block": "t8435InBlock", "body": {"gubun": "MF"},
                   "out_block": "t8435OutBlock"},
    },
    "os_futopt": {
        "chart": {"path": "/overseas-futureoption/chart", "tr_cd": "o3103",
                  "in_block": "o3103InBlock", "out_block": "o3103OutBlock1",
                  "fields": {"date": "date", "time": "time", "open": "open",
                             "high": "high", "low": "low", "close": "close",
                             "volume": "volume"}},
        "master": {"path": "/overseas-futureoption/market-data", "tr_cd": "o3121",
                   "in_block": "o3121InBlock", "body": {"MktGb": "F", "BscGdsCd": ""},
                   "out_block": "o3121OutBlock"},
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument(
  instr_id     TEXT PRIMARY KEY,          -- KTB3 · ZN ...
  market       TEXT NOT NULL,             -- KRX · CME
  channel      TEXT NOT NULL,             -- kr_futopt · os_futopt
  name         TEXT NOT NULL,
  underlying   TEXT NOT NULL,             -- 기초자산
  symbol       TEXT,                      -- 활성 월물 코드 (discover 로 갱신)
  active       INTEGER NOT NULL DEFAULT 1,
  updated_utc  TEXT
);
CREATE TABLE IF NOT EXISTS minbar(
  instr_id     TEXT NOT NULL,
  bar_time     TEXT NOT NULL,             -- 'YYYY-MM-DD HH:MM' 거래소 현지시간
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  symbol       TEXT,                      -- 수집 당시 월물
  collected_utc TEXT NOT NULL,
  PRIMARY KEY(instr_id, bar_time)
);
CREATE TABLE IF NOT EXISTS collect_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  instr_id TEXT, tr_cd TEXT,
  rows_in INTEGER, rows_new INTEGER,
  status TEXT NOT NULL,                   -- ok · error · dry
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_minbar_time ON minbar(bar_time);
"""


def open_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    for ins in INSTRUMENTS:
        con.execute(
            "INSERT INTO instrument(instr_id,market,channel,name,underlying,symbol,updated_utc) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(instr_id) DO UPDATE SET symbol=excluded.symbol, updated_utc=excluded.updated_utc "
            "WHERE excluded.symbol != ''",
            (ins["instr_id"], ins["market"], ins["channel"], ins["name"],
             ins["underlying"], ins["symbol"], _now()))
    con.commit()
    return con


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _log(con, instr_id, tr_cd, rows_in, rows_new, status, detail=""):
    con.execute("INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (_now(), instr_id, tr_cd, rows_in, rows_new, status, detail[:500]))
    con.commit()


def dry_run(env) -> int:
    print("[dry-run] 기본 모드 — 네트워크·DB 쓰기 없음. 수집 계획:")
    print(f"  DB: {DB} (gitignore 대상)")
    for ch, (kk, ks) in CHANNELS.items():
        ok = bool(env.get(kk) and env.get(ks))
        print(f"  채널 {ch:9s}: 자격증명 {'OK' if ok else 'MISSING'}")
    for i in INSTRUMENTS:
        sym = i["symbol"] or "(월물 미지정 -> --discover)"
        print(f"  {i['instr_id']:5s} {i['market']:3s} via {i['channel']:9s} "
              f"symbol={sym}  [{i['underlying']}]")
    print("  주문/체결 API: 없음 (조회 전용)")
    print("  다음 단계: --issue-token -> --discover -> --live")
    return 0


def do_issue_token(env) -> int:
    rc = 0
    for ch in ("kr_futopt", "os_futopt", "general"):
        kk, ks = CHANNELS[ch]
        if not (env.get(kk) and env.get(ks)):
            print(f"[token] {ch:9s}: 자격증명 없음 — 건너뜀")
            continue
        try:
            tok = issue_token(ch, env, force=True)
            print(f"[token] {ch:9s}: OK (길이 {len(tok)}, 캐시 저장)")
        except Exception as e:
            print(f"[token] {ch:9s}: FAIL — {e}")
            rc = 1
    return rc


def do_discover(env) -> int:
    """종목 마스터를 조회해 KTB·채권 선물 월물 코드 후보를 보여준다."""
    for ch in ("kr_futopt", "os_futopt"):
        m = CONFIG[ch]["master"]
        print(f"\n[discover] {ch} · {m['tr_cd']} {m['path']}")
        try:
            j = call_tr(ch, m["path"], m["tr_cd"], {m["in_block"]: m["body"]})
            rows = j.get(m["out_block"], [])
            if isinstance(rows, dict):
                rows = [rows]
            print(f"  rows={len(rows)}")
            kw = ("국채", "KTB", "T-NOTE", "T-BOND", "ZT", "ZF", "ZN", "ZB",
                  "2YR", "5YR", "10YR", "30YR", "10Y", "US")
            hits = 0
            for r in rows:
                s = " ".join(str(v) for v in r.values())
                if any(k.lower() in s.lower() for k in kw):
                    print("   ", {k: r[k] for k in list(r)[:8]})
                    hits += 1
                    if hits >= 40:
                        print("    ... (40건에서 중단)")
                        break
            if hits == 0:
                print("  (키워드 일치 없음 — 첫 3행 원본)")
                for r in rows[:3]:
                    print("   ", r)
        except Exception as e:
            print(f"  FAIL — {e}")
    print("\n찾은 월물 코드를 INSTRUMENTS[*]['symbol'] 에 넣거나 "
          "instrument 테이블 symbol 컬럼을 갱신하십시오.")
    return 0


def do_live(env, minutes: int, count: int) -> int:
    con = open_db()
    rc = 0
    for i in INSTRUMENTS:
        ch, iid = i["channel"], i["instr_id"]
        row = con.execute("SELECT symbol FROM instrument WHERE instr_id=?",
                          (iid,)).fetchone()
        sym = (row[0] if row and row[0] else i["symbol"]).strip()
        if not sym:
            print(f"[live] {iid}: 월물 코드 미지정 — 건너뜀 (--discover 로 확인)")
            _log(con, iid, "", 0, 0, "error", "symbol not set")
            continue
        c = CONFIG[ch]["chart"]
        if ch == "kr_futopt":     # t8461 — 필드가 다르다 (위 CONFIG 주석)
            body = {c["in_block"]: {"focode": sym, "cgubun": "B",
                                    "bgubun": str(minutes), "cnt": count}}
        else:
            body = {c["in_block"]: {"shcode": sym, "ncnt": minutes,
                                    "readcnt": count,
                                    "cts_date": "", "cts_time": ""}}
        try:
            j = call_tr(ch, c["path"], c["tr_cd"], body)
            rows = j.get(c["out_block"], [])
            if isinstance(rows, dict):
                rows = [rows]
            f = c["fields"]
            new = 0
            # t8461 은 날짜가 없다 — 최근 KRX 세션 날짜로 합성 (야간 근사 한계)
            sess = dt.date.today()
            while sess.weekday() >= 5:
                sess -= dt.timedelta(days=1)
            for r in rows:
                t = str(r.get(f["time"], "")).zfill(6)
                if f["date"]:
                    d = str(r.get(f["date"], ""))
                    if len(d) != 8:
                        continue
                else:
                    d = f"{sess:%Y%m%d}"
                bar = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"
                cur = con.execute(
                    "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,symbol,collected_utc) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(instr_id,bar_time) DO NOTHING",
                    (iid, bar,
                     float(r.get(f["open"], 0) or 0), float(r.get(f["high"], 0) or 0),
                     float(r.get(f["low"], 0) or 0), float(r.get(f["close"], 0) or 0),
                     float(r.get(f["volume"], 0) or 0), sym, _now()))
                new += cur.rowcount
            con.commit()
            _log(con, iid, c["tr_cd"], len(rows), new, "ok")
            print(f"[live] {iid} ({sym}): 수신 {len(rows)}행 · 신규 {new}행")
        except Exception as e:
            _log(con, iid, c["tr_cd"], 0, 0, "error", str(e))
            print(f"[live] {iid} ({sym}): FAIL — {e}")
            rc = 1
    n = con.execute("SELECT COUNT(*) FROM minbar").fetchone()[0]
    print(f"[live] minbar 누적 {n}행 -> {DB}")
    con.close()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="KTB+CME 채권 선물 1분봉 수집기 (조회 전용)")
    ap.add_argument("--issue-token", action="store_true", help="채널별 토큰 발급 시험")
    ap.add_argument("--init-db", action="store_true", help="스키마·instrument 만 생성")
    ap.add_argument("--discover", action="store_true", help="종목 마스터 조회 (월물 코드 탐색)")
    ap.add_argument("--live", action="store_true", help="1분봉 실수집 → SQLite")
    ap.add_argument("--minutes", type=int, default=1, help="봉 주기 분 (기본 1)")
    ap.add_argument("--count", type=int, default=500, help="요청당 봉 수 (기본 500)")
    a = ap.parse_args()

    env = load_env()
    if a.issue_token:
        return do_issue_token(env)
    if a.init_db:
        con = open_db()
        n = con.execute("SELECT COUNT(*) FROM instrument").fetchone()[0]
        print(f"[init-db] {DB} 준비 완료 · instrument {n}종")
        con.close()
        return 0
    if a.discover:
        return do_discover(env)
    if a.live:
        return do_live(env, a.minutes, a.count)
    return dry_run(env)                       # ★ 기본 = dry-run


if __name__ == "__main__":
    sys.exit(main())
