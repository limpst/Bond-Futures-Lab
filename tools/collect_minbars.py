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
    {"instr_id": "KTB30", "market": "KRX", "channel": "kr_futopt",
     "name": "KTB 30Y futures", "underlying": "국고채 30년 바스켓", "symbol": "A7069000"},
    {"instr_id": "ZT", "market": "CME", "channel": "os_futopt",
     "name": "2-Year T-Note futures",  "underlying": "US Treasury 2Y",  "symbol": "ZTU26"},
    {"instr_id": "ZF", "market": "CME", "channel": "os_futopt",
     "name": "5-Year T-Note futures",  "underlying": "US Treasury 5Y",  "symbol": "ZFU26"},
    {"instr_id": "ZN", "market": "CME", "channel": "os_futopt",
     "name": "10-Year T-Note futures", "underlying": "US Treasury 10Y", "symbol": "ZNU26"},
    {"instr_id": "ZB", "market": "CME", "channel": "os_futopt",
     "name": "30-Year T-Bond futures", "underlying": "US Treasury 30Y", "symbol": "ZBU26"},
    {"instr_id": "TN", "market": "CME", "channel": "os_futopt",
     "name": "Ultra 10-Year T-Note futures", "underlying": "US Treasury Ultra 10Y",
     "symbol": "TNU26"},
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
CREATE TABLE IF NOT EXISTS universe_scan(
  ts_utc TEXT NOT NULL,
  bsc_cd TEXT NOT NULL, name TEXT, exch TEXT,
  symbol TEXT, volume INTEGER, passed INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


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


def open_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = _sqlite_connect_safe(DB, timeout=60)
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


def _session_start_date(bar: dt.datetime) -> dt.date:
    """이 봉이 속한 세션의 '거래일'(= 세션이 시작한 날).

    KRX 파생: 주간 09:00~15:45 · 야간 18:00~익일 05:00.
    야간 후반(00:00~05:59)은 **전날** 세션에 속한다.
    """
    if bar.hour < 6:
        return bar.date() - dt.timedelta(days=1)
    return bar.date()


def _reject_reason(bar_times: list[str]) -> str:
    """합성한 날짜가 말이 되는지 본다. 이상하면 사유, 멀쩡하면 빈 문자열.

    왜 필요한가 (2026-08-26 사고):
      t8461 은 날짜 필드를 주지 않아 수집기가 `now` 를 기준으로 날짜를 합성한다.
      그 기준이 하루 어긋나면 **세션 전체가 통째로 잘못된 날짜에 박힌다**.
      실제로 08-25 백필이 08-24 야간 세션을 08-23(일요일) 야간으로 적재해,
      OHLCV 까지 똑같은 유령 세션 711봉이 생겼다. upsert 라 조용히 들어갔고
      ADF·AR(1)·ECM·전략 성과가 전부 오염됐다(전략 순위가 뒤집혔다).

      한 줄로 잡을 수 있다: **일요일 밤에는 KRX 야간 세션이 없다.**
      거래일이 토/일이면 그 배치는 통째로 버린다 — 절반만 맞은 데이터보다
      아예 없는 편이 낫다.
    """
    days = set()
    for s in bar_times:
        try:
            days.add(_session_start_date(dt.datetime.strptime(s, "%Y-%m-%d %H:%M")))
        except ValueError:
            return "bar_time 파싱 실패: %s" % s[:20]
    weekend = sorted(d for d in days if d.weekday() >= 5)   # 5=토 6=일
    if weekend:
        return ("거래일이 주말로 합성됨 %s — 날짜 기준(now)이 어긋난 배치로 보고 버림"
                % ", ".join("%s(%s)" % (d, "토일"[d.weekday() - 5]) for d in weekend[:3]))
    if len(days) > 2:
        return "한 배치가 거래일 %d개에 걸침 %s — 세션 경계 합성 오류로 보고 버림" % (
            len(days), sorted(days)[:4])
    return ""


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
    # ★ 수집 대상은 DB(instrument, active=1) 기준 — --scan 이 편입한 종목까지 전부
    targets = con.execute("SELECT instr_id, channel, symbol FROM instrument "
                          "WHERE active=1 ORDER BY instr_id").fetchall()
    for iid, ch, sym in targets:
        sym = (sym or "").strip()
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
            # t8461 은 날짜 필드가 없다. 예전에는 '최근 평일' 하나로 전 봉을 눌러
            # 담았는데, 야간 세션(18:00~익일 05:00)이 자정을 넘기므로 18:00~23:59
            # 블록과 00:00~05:59 블록이 같은 날짜가 되어 시계열이 06:00 -> 18:01 로
            # 12시간 거꾸로 점프했다(2026-08-24 실측: 그 이음매의 가격 변화가 표본
            # 전체 최대값 = AR(1)/ADF/ECM 을 직접 오염).
            #
            # 고친 방식: 응답은 최신 봉이 먼저 온다. 최신 봉을 '지금 날짜'에 걸고
            # 과거로 내려가다가 시각이 되레 커지면(예: 00:00 -> 23:59) 자정을
            # 거꾸로 넘은 것이므로 날짜를 하루 뺀다.
            now = dt.datetime.now()
            sess = now.date()
            if rows:
                t0 = str(rows[0].get(f["time"], "")).zfill(6)
                # 최신 봉이 현재 시각보다 뚜렷이 앞서면 지난 날의 자료다
                if t0 > (now + dt.timedelta(minutes=2)).strftime("%H%M%S"):
                    sess -= dt.timedelta(days=1)
            # 1) 먼저 전 봉의 날짜를 합성만 해 둔다 (아직 쓰지 않는다)
            staged = []
            prev_t = None
            for r in rows:
                t = str(r.get(f["time"], "")).zfill(6)
                if f["date"]:
                    d = str(r.get(f["date"], ""))
                    if len(d) != 8:
                        continue
                else:
                    if prev_t is not None and t > prev_t:
                        sess -= dt.timedelta(days=1)      # 자정을 거꾸로 넘음
                    prev_t = t
                    d = f"{sess:%Y%m%d}"
                staged.append((f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}", r))

            # 2) 합성 결과가 말이 되는지 본다 — 날짜 필드가 없는 채널만 검사한다
            #    (o3103 은 date 를 주므로 합성하지 않는다)
            if staged and not f["date"]:
                why = _reject_reason([b for b, _ in staged])
                if why:
                    _log(con, iid, c["tr_cd"], len(rows), 0, "error", "날짜 검증 실패: " + why)
                    print(f"[live] {iid} ({sym}): 배치 거부 — {why}")
                    continue

            # 3) 통과한 배치만 쓴다
            for bar, r in staged:
                cur = con.execute(
                    "INSERT INTO minbar(instr_id,bar_time,open,high,low,close,volume,symbol,collected_utc) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(instr_id,bar_time) DO UPDATE SET "
                    "open=excluded.open,high=excluded.high,low=excluded.low,"
                    "close=excluded.close,volume=excluded.volume,"
                    "collected_utc=excluded.collected_utc",
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


def do_scan(env, min_vol: int) -> int:
    """해외선물 유니버스 스캔 — 상품별 근월물 거래량을 읽어 거래량순 정렬,
    min_vol 계약 이상은 전부 수집 대상으로 편입한다 (2026-08-23 지시: 50만+ 전부).

    ※ 마스터(o3121)는 현재 계정에 반영된 거래소의 상품만 보여준다 — CME 시세가
      API 유니버스에 반영되면 그날 스캔부터 자동으로 잡힌다. 거래량은 o3106
      현재가의 당일 누적 계약 수 (휴장일에는 직전 세션 값/0 일 수 있음 — 한계).
    """
    con = open_db()
    m = CONFIG["os_futopt"]["master"]
    try:
        j = call_tr("os_futopt", m["path"], m["tr_cd"], {m["in_block"]: m["body"]})
    except Exception as e:
        print(f"[scan] 마스터 조회 실패: {e}")
        return 1
    rows = j.get(m["out_block"], [])
    if isinstance(rows, dict):
        rows = [rows]
    # 상품별 근월물: (연도, 월코드 순번) 최소값. LstngM 은 월 '문자'(F~Z)다.
    MONTH_ORD = {c: i for i, c in enumerate("FGHJKMNQUVXZ", 1)}
    prods: dict[str, dict] = {}
    for r in rows:
        cd = r.get("BscGdsCd", "")
        yr = str(r.get("LstngYr") or "9999")
        mo = MONTH_ORD.get(str(r.get("LstngM") or "").strip(), 99)
        key = (int(yr) if yr.isdigit() else 9999, mo)
        if cd and (cd not in prods or key < prods[cd]["_key"]):
            prods[cd] = {"_key": key, "sym": r.get("Symbol", ""),
                         "nm": r.get("BscGdsNm", ""), "ex": r.get("ExchCd", "")}
    ranked = []
    for cd, p in prods.items():
        vol = 0
        try:
            q = call_tr("os_futopt", "/overseas-futureoption/market-data", "o3106",
                        {"o3106InBlock": {"symbol": p["sym"]}})
            vol = int((q.get("o3106OutBlock") or {}).get("volume") or 0)
        except Exception:
            pass
        ranked.append((vol, cd, p))
    ranked.sort(reverse=True)
    print(f"[scan] 상품 {len(ranked)}종 — 거래량 내림차순 (기준 {min_vol:,} 계약):")
    n_in = 0
    for vol, cd, p in ranked:
        passed = vol >= min_vol
        mark = "✅ 편입" if passed else "  "
        print(f"  {mark} {cd:5s} {p['ex']:5s} {p['sym']:8s} vol={vol:>10,}  {p['nm']}")
        con.execute("INSERT INTO universe_scan(ts_utc,bsc_cd,name,exch,symbol,volume,passed) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_now(), cd, p["nm"], p["ex"], p["sym"], vol, int(passed)))
        if passed:
            con.execute(
                "INSERT INTO instrument(instr_id,market,channel,name,underlying,symbol,active,updated_utc) "
                "VALUES(?,?,?,?,?,?,1,?) "
                "ON CONFLICT(instr_id) DO UPDATE SET symbol=excluded.symbol, active=1, "
                "updated_utc=excluded.updated_utc",
                (cd, p["ex"], "os_futopt", p["nm"], p["nm"], p["sym"], _now()))
            n_in += 1
    con.execute("INSERT INTO meta(k,v) VALUES('last_scan',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_now()[:10],))
    con.commit()
    con.close()
    print(f"[scan] {n_in}종 편입 (기존 채권 선물 유니버스는 유지)")
    return 0


def do_health() -> int:
    """월요일 아침 원커맨드 점검 — KTB 봉 증가·CME 유입·최근 수집 상태·시그널."""
    if not DB.is_file():
        print("[health] DB 없음 — 수집 전")
        return 1
    con = _sqlite_connect_safe(DB, timeout=60)
    today = f"{dt.date.today()}"
    print(f"[health] {_now()} UTC · DB {DB.name}")
    print("  종목별 봉수 (전체 / 오늘):")
    for iid, tot in con.execute("SELECT instr_id, COUNT(*) FROM minbar GROUP BY instr_id"):
        td = con.execute("SELECT COUNT(*) FROM minbar WHERE instr_id=? AND bar_time LIKE ?",
                         (iid, today + "%")).fetchone()[0]
        print(f"    {iid:6s} {tot:>7,} / 오늘 {td:,}")
    cme = con.execute("SELECT COUNT(*) FROM minbar WHERE instr_id IN "
                      "('ZT','ZF','ZN','ZB','TN')").fetchone()[0]
    print(f"  CME 유입: {'✅ ' + format(cme, ',') + '봉' if cme else '⬜ 아직 0 — API 유니버스 반영 대기'}")
    print("  최근 수집 로그:")
    for r in con.execute("SELECT ts_utc,instr_id,rows_in,rows_new,status FROM collect_log "
                         "ORDER BY id DESC LIMIT 6"):
        print(f"    {r}")
    sig = ROOT / "data" / "signals.json"
    if sig.is_file():
        import json
        s = json.loads(sig.read_text(encoding="utf-8"))
        print(f"  시그널 (생성 {s.get('generated_utc')} UTC):")
        for p in s.get("pairs", []):
            if "z" in p:
                print(f"    {p['pair']:12s} z={p['z']:+.2f} ecm_t={p['ecm_t']} -> {p['signal']}")
    else:
        print("  시그널: signals.json 없음 — python tools/signal_monitor.py 실행")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KTB+CME 채권 선물 1분봉 수집기 (조회 전용)")
    ap.add_argument("--issue-token", action="store_true", help="채널별 토큰 발급 시험")
    ap.add_argument("--init-db", action="store_true", help="스키마·instrument 만 생성")
    ap.add_argument("--discover", action="store_true", help="종목 마스터 조회 (월물 코드 탐색)")
    ap.add_argument("--live", action="store_true", help="1분봉 실수집 → SQLite")
    ap.add_argument("--minutes", type=int, default=1, help="봉 주기 분 (기본 1)")
    ap.add_argument("--count", type=int, default=500, help="요청당 봉 수 (기본 500)")
    ap.add_argument("--scan", action="store_true",
                    help="해외 유니버스 스캔 — 거래량순 정렬·min-vol 이상 전부 편입")
    ap.add_argument("--min-vol", type=int, default=500_000,
                    help="스캔 편입 기준 계약 수 (기본 500,000)")
    ap.add_argument("--scan-daily", action="store_true",
                    help="--live 앞에 하루 1회 자동 스캔 (스케줄러용)")
    ap.add_argument("--health", action="store_true",
                    help="점검 원커맨드 — 봉 증가·CME 유입·수집 로그·시그널")
    a = ap.parse_args()

    env = load_env()
    if a.health:
        return do_health()
    if a.scan:
        return do_scan(env, a.min_vol)
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
        if a.scan_daily:
            con = open_db()
            last = con.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
            con.close()
            if not last or last[0] != f"{dt.date.today()}":
                do_scan(env, a.min_vol)
        return do_live(env, a.minutes, a.count)
    return dry_run(env)                       # ★ 기본 = dry-run


if __name__ == "__main__":
    sys.exit(main())
