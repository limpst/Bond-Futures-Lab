# -*- coding: utf-8 -*-
"""월물 롤오버 — 만기가 가까워지면 차근월물로 옮기고, 그 이음매를 P&L 에서 지운다.

왜 필요한가
  선물은 만기가 있다. 최근월(front)이 만기에 다가가면 거래가 차근월(next)로
  옮겨간다. 코드를 그대로 두면 어느 순간부터 거래 없는 종목을 보게 되고
  (2026-08-25 KTB30 이 그 상태였다), 반대로 코드만 바꾸면 두 계약의 가격 차이가
  '1분 만에 생긴 손익'으로 둔갑한다. 두 문제를 함께 푼다.

  ① 언제 옮기나 — 거래량 우선, 캘린더 보조
       차근월 거래량 > 최근월 거래량  → 옮긴다 (시장이 먼저 옮겨간 것)
       또는 만기 D-ROLL_DAYS 영업일    → 옮긴다 (거래량을 못 볼 때의 안전망)
  ② 옮길 때 손익 — 이음매(gap)는 손익이 아니다
       gap = 새 계약 가격 − 옛 계약 가격 을 roll_event 에 남기고,
       연속 시계열은 back-adjust(파나마) 로 만든다:
         adj(t) = raw(t) + Σ{ gap_i : roll_i 가 t 보다 뒤 }
       이렇게 하면 이음매에서 값이 튀지 않고, 차분(1분 변화)이 전부
       '같은 계약 안에서의 가격 변화' 가 된다. 롤 자체로는 손익이 0 이다.

코드 체계 (2026-08-26 마스터 실측으로 역산 · 검증 완료)
  KRX  A + 상품2 + 연도끝자리 + 월문자 + '000'   월문자 1~9,A,B,C
       국채선물 상품코드 65=3년 66=5년 67=10년 70=30년, 결제월 3·6·9·12
       검산: 미니선물 2609=A0569000 / 2610=A056A000 / 2612=A056C000 재현 일치
       실조회: A656C000(3년 12월물)=102.88, A676C000(10년 12월물)=105.08
  CME  상품 + 월문자(H·M·U·Z) + 연도2자리        예: ZNU26 → ZNZ26
       ★ CME 는 이 계정의 REST 조회 범위 밖이다(o3121 마스터에 홍콩·런던만,
         o3103 차트는 rsp_cd=00000 에 0행). 그래서 거래량으로 판정할 수 없고
         캘린더 규칙만 쓴다. 실제 유효성은 WebSocket tick 수신으로 확인된다.

  python tools/roll.py --check           지금 옮겨야 하는지 보기만 (기본)
  python tools/roll.py --apply           instrument.symbol 갱신 + roll_event 기록
  python tools/roll.py --series KTB10    연속(back-adjusted) 종가 확인
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"

ROLL_DAYS = 5          # 만기 D-5 영업일이면 캘린더 규칙으로 롤
QUARTERLY = (3, 6, 9, 12)
KRX_MON = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
           9: "9", 10: "A", 11: "B", 12: "C"}
CME_MON = {3: "H", 6: "M", 9: "U", 12: "Z"}
KRX_PROD = {"KTB3": "65", "KTB5": "66", "KTB10": "67", "KTB30": "70"}
CME_PROD = {"ZT": "ZT", "ZF": "ZF", "ZN": "ZN", "ZB": "ZB", "TN": "TN"}
# EUREX 독일 국채선물 — 코드 규칙은 CME 와 같은 <root><월문자><연2자리>.
# 2026-08-27 WS 실측으로 수신 확인 (FGBL·FGBM·FGBS). FGBX(Buxl)는 tick 0 이라 제외.
EUREX_PROD = {"FGBS": "FGBS", "FGBM": "FGBM", "FGBL": "FGBL"}

ROLL_SCHEMA = """
CREATE TABLE IF NOT EXISTS roll_event(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instr_id    TEXT NOT NULL,
  roll_time   TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM' KST, 이음매 시각
  from_symbol TEXT NOT NULL,
  to_symbol   TEXT NOT NULL,
  px_from     REAL,                   -- 옛 계약 마지막 가격
  px_to       REAL,                   -- 새 계약 첫 가격
  gap         REAL,                   -- px_to - px_from  (손익이 아니다)
  reason      TEXT NOT NULL,          -- volume · calendar · manual
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_roll_instr ON roll_event(instr_id, roll_time);
"""


# ── 만기 계산 ────────────────────────────────────────────────────────────
def third_tuesday(year: int, month: int) -> dt.date:
    """KRX 국채선물 최종거래일 = 결제월 세 번째 화요일."""
    d = dt.date(year, month, 1)
    tue = d + dt.timedelta(days=(1 - d.weekday()) % 7)     # 첫 화요일
    return tue + dt.timedelta(days=14)


def last_business_day(year: int, month: int) -> dt.date:
    """그 달의 마지막 영업일(주말만 제외 — 공휴일은 보지 않는다)."""
    d = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def cme_first_notice(year: int, month: int) -> dt.date:
    """CME 채권선물 최초인도통지일 = 인도월 직전월의 마지막 영업일."""
    y, m = (year - 1, 12) if month == 1 else (year, month - 1)
    return last_business_day(y, m)


def eurex_last_trading(year: int, month: int) -> dt.date:
    """Eurex 채권선물 최종거래일.

    Eurex 규칙: 인도일 = 인도월 **10일**(거래일이 아니면 다음 거래일),
    최종거래일 = 인도일의 **2 거래일 전**. CME 의 '직전월 마지막 영업일'
    과 다르므로 별도 함수로 둔다 — 섞으면 롤이 이틀씩 어긋난다.
    (검산: 2026-09 인도 → 9/10 목 인도일 → 최종거래일 9/8 화)

    주의: 공휴일은 반영하지 않는다(주말만). 최종거래일이 공휴일과 겹치면
    실제보다 늦게 잡히므로, ROLL_DAYS 여유가 그 오차를 흡수한다.
    """
    d = dt.date(year, month, 10)
    while d.weekday() >= 5:                 # 인도일이 주말이면 다음 거래일
        d += dt.timedelta(days=1)
    for _ in range(2):                      # 2 거래일 전
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
    return d


def busdays_between(a: dt.date, b: dt.date) -> int:
    """a→b 영업일 수(주말 제외). b 가 과거면 음수."""
    step = 1 if b >= a else -1
    n, d = 0, a
    while d != b:
        d += dt.timedelta(days=step)
        if d.weekday() < 5:
            n += step
    return n


def next_quarter(year: int, month: int) -> tuple[int, int]:
    for m in QUARTERLY:
        if m > month:
            return year, m
    return year + 1, QUARTERLY[0]


# ── 코드 구성/해석 ───────────────────────────────────────────────────────
def krx_code(instr_id: str, year: int, month: int) -> str:
    return "A%s%s%s000" % (KRX_PROD[instr_id], str(year)[-1], KRX_MON[month])


def krx_parse(code: str) -> tuple[int, int]:
    """A6769000 → (2026, 9). 연도 끝자리만 있으므로 현재 연도 기준으로 편다."""
    y1, mc = code[3], code[4]
    month = {v: k for k, v in KRX_MON.items()}[mc]
    now = dt.date.today()
    for cand in (now.year, now.year + 1, now.year + 2):
        if str(cand)[-1] == y1:
            return cand, month
    return now.year, month


def cme_code(instr_id: str, year: int, month: int) -> str:
    return "%s%s%02d" % (CME_PROD[instr_id], CME_MON[month], year % 100)


def cme_parse(code: str) -> tuple[int, int]:
    mc, yy = code[-3], code[-2:]
    month = {v: k for k, v in CME_MON.items()}[mc]
    return 2000 + int(yy), month


# ── 롤 판정 ──────────────────────────────────────────────────────────────
def krx_volume(symbol: str, cnt: int = 30) -> float | None:
    """t8461 로 최근 cnt 분 거래량 합. 조회 실패/무응답이면 None."""
    try:
        from ls_openapi import call_tr
        j = call_tr("kr_futopt", "/futureoption/chart", "t8461",
                    {"t8461InBlock": {"focode": symbol, "cgubun": "B",
                                      "bgubun": "1", "cnt": cnt}})
        rows = j.get("t8461OutBlock1", [])
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return None
        return sum(float(r.get("volume", 0) or 0) for r in rows)
    except Exception:
        return None


def decide(instr_id: str, market: str, symbol: str, today: dt.date,
           use_api: bool) -> dict:
    """지금 롤해야 하는가. reason 은 volume · calendar · none 중 하나."""
    if market == "KRX":
        cy, cm = krx_parse(symbol)
        expiry = third_tuesday(cy, cm)
        ny, nm = next_quarter(cy, cm)
        nxt = krx_code(instr_id, ny, nm)
    elif market == "EUREX":
        # 코드 규칙은 CME 와 동일하지만 **만기 규칙이 다르다** (위 함수 주석 참조).
        cy, cm = cme_parse(symbol)
        expiry = eurex_last_trading(cy, cm)
        ny, nm = next_quarter(cy, cm)
        nxt = "%s%s%02d" % (EUREX_PROD[instr_id], CME_MON[nm], ny % 100)
    else:
        cy, cm = cme_parse(symbol)
        expiry = cme_first_notice(cy, cm)
        ny, nm = next_quarter(cy, cm)
        nxt = cme_code(instr_id, ny, nm)

    left = busdays_between(today, expiry)
    out = {"instr_id": instr_id, "market": market, "cur": symbol, "next": nxt,
           "expiry": expiry.isoformat(), "busdays_left": left,
           "roll": False, "reason": "none", "vol_cur": None, "vol_next": None}

    # ① 거래량 — KRX 만 관측 가능 (CME·EUREX 는 REST 조회 범위 밖이라 캘린더만)
    if market == "KRX" and use_api:
        vc, vn = krx_volume(symbol), krx_volume(nxt)
        out["vol_cur"], out["vol_next"] = vc, vn
        if vc is not None and vn is not None and vn > vc:
            out.update(roll=True, reason="volume")
            return out

    # ② 캘린더 — 만기 D-ROLL_DAYS 영업일
    if left <= ROLL_DAYS:
        out.update(roll=True, reason="calendar")
    return out


# ── 롤 실행 · 이음매 기록 ────────────────────────────────────────────────
def last_close(con, instr_id: str) -> tuple[str | None, float | None]:
    r = con.execute("SELECT bar_time, close FROM minbar WHERE instr_id=? "
                    "ORDER BY bar_time DESC LIMIT 1", (instr_id,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def apply_roll(con, d: dict, px_to: float | None) -> None:
    """instrument.symbol 을 바꾸고 이음매를 roll_event 에 남긴다."""
    bar_time, px_from = last_close(con, d["instr_id"])
    gap = (px_to - px_from) if (px_to is not None and px_from is not None) else None
    con.execute(
        "INSERT INTO roll_event(instr_id,roll_time,from_symbol,to_symbol,"
        "px_from,px_to,gap,reason,created_utc) VALUES(?,?,?,?,?,?,?,?,?)",
        (d["instr_id"], bar_time or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
         d["cur"], d["next"], px_from, px_to, gap, d["reason"],
         dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    con.execute("UPDATE instrument SET symbol=?, updated_utc=? WHERE instr_id=?",
                (d["next"], dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                 d["instr_id"]))
    con.commit()


# ── 연속(back-adjusted) 시계열 ───────────────────────────────────────────
def continuous(con, instr_id: str) -> list[tuple[str, float]]:
    """이음매를 지운 종가 시계열.

    adj(t) = raw(t) + Σ{ gap_i : roll_i 가 t 보다 뒤 }
    최근 계약 구간은 손대지 않고, 과거를 끌어올려 붙인다. 이렇게 하면
    연속 시계열의 1분 차분이 전부 '같은 계약 안의 가격 변화' 가 되어
    롤 자체가 손익으로 새지 않는다.
    """
    rolls = con.execute("SELECT roll_time, gap FROM roll_event "
                        "WHERE instr_id=? AND gap IS NOT NULL "
                        "ORDER BY roll_time", (instr_id,)).fetchall()
    bars = con.execute("SELECT bar_time, close FROM minbar WHERE instr_id=? "
                       "ORDER BY bar_time", (instr_id,)).fetchall()
    if not rolls:
        return [(b, c) for b, c in bars]
    out = []
    for bt, c in bars:
        shift = sum(g for rt, g in rolls if rt > bt)
        out.append((bt, c + shift))
    return out


def backfill_gaps(con) -> int:
    """gap 이 비어 있는 롤 이벤트를 봉 데이터로 메운다.

    CME 는 REST 조회가 막혀 있어 롤 시점에 새 계약 가격을 알 수 없다. 대신
    minbar.symbol 에 '수집 당시 월물' 이 남으므로, 새 월물의 첫 봉이 쌓인 뒤
    옛 월물 마지막 종가와 비교해 이음매를 사후에 채운다.
    """
    rows = con.execute("SELECT id, instr_id, roll_time, from_symbol, to_symbol "
                       "FROM roll_event WHERE gap IS NULL ORDER BY id").fetchall()
    n = 0
    for rid, iid, rt, s_from, s_to in rows:
        a = con.execute("SELECT close FROM minbar WHERE instr_id=? AND symbol=? "
                        "AND bar_time<=? ORDER BY bar_time DESC LIMIT 1",
                        (iid, s_from, rt)).fetchone()
        b = con.execute("SELECT close FROM minbar WHERE instr_id=? AND symbol=? "
                        "AND bar_time>=? ORDER BY bar_time LIMIT 1",
                        (iid, s_to, rt)).fetchone()
        if not (a and b):
            continue
        px_from, px_to = a[0], b[0]
        con.execute("UPDATE roll_event SET px_from=?, px_to=?, gap=? WHERE id=?",
                    (px_from, px_to, px_to - px_from, rid))
        n += 1
        print("   gap 보정: %s %s→%s  %.6f → %.6f  gap=%+.6f"
              % (iid, s_from, s_to, px_from, px_to, px_to - px_from))
    con.commit()
    return n


def roll_pnl_note(con, instr_id: str) -> dict:
    """롤로 인해 '손익처럼 보였던' 금액 — 실제 손익이 아님을 수치로 보인다."""
    rows = con.execute("SELECT roll_time, from_symbol, to_symbol, gap FROM roll_event "
                       "WHERE instr_id=? ORDER BY roll_time", (instr_id,)).fetchall()
    return {"n_rolls": len(rows),
            "gap_sum": sum((r[3] or 0) for r in rows),
            "events": [{"time": r[0], "from": r[1], "to": r[2], "gap": r[3]} for r in rows]}


# ── CLI ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="선물 월물 롤오버 (조회 전용 + DB 갱신)")
    ap.add_argument("--apply", action="store_true", help="실제로 symbol 을 바꾼다")
    ap.add_argument("--series", default="", help="연속 종가를 볼 instr_id")
    ap.add_argument("--no-api", action="store_true", help="거래량 조회 없이 캘린더만")
    ap.add_argument("--fixgap", action="store_true",
                    help="gap 이 빈 롤 이벤트를 봉 데이터로 사후 보정")
    a = ap.parse_args()

    # 수집기가 같은 파일을 5분마다 쓴다 — 잠금 대기를 넉넉히 준다.
    # executescript 는 시작할 때 COMMIT 을 먼저 던져 busy_timeout 을 건너뛰므로
    # 문장을 하나씩 실행한다. 그래도 잠겨 있으면 판정(--check)은 읽기만 하므로
    # 계속 진행하고, 쓰기가 필요한 --apply 에서만 실패시킨다.
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    try:
        for stmt in filter(None, (x.strip() for x in ROLL_SCHEMA.split(";"))):
            con.execute(stmt)
        con.commit()
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        print("   (알림) DB 가 수집기에 잠겨 있습니다 — 판정은 읽기만 하므로 계속합니다.")
        if a.apply:
            print("   --apply 는 쓰기가 필요합니다. 수집 사이클(5분) 사이에 다시 실행하십시오.")
            return 1

    if a.fixgap:
        n = backfill_gaps(con)
        print("보정한 롤 이벤트: %d건" % n)
        return 0

    if a.series:
        s = continuous(con, a.series)
        raw = con.execute("SELECT bar_time, close FROM minbar WHERE instr_id=? "
                          "ORDER BY bar_time DESC LIMIT 3", (a.series,)).fetchall()
        print("%s — 연속 시계열 %d봉" % (a.series, len(s)))
        for bt, c in s[-3:]:
            print("   %s  adj=%.6f" % (bt, c))
        print("   raw 최근 3봉:", [(b, c) for b, c in raw][::-1])
        print("   롤 이력:", roll_pnl_note(con, a.series))
        return 0

    today = dt.date.today()
    print("오늘 %s · 롤 기준: 차근월 거래량 우위 또는 만기 D-%d영업일\n"
          % (today.isoformat(), ROLL_DAYS))
    rows = con.execute("SELECT instr_id, market, symbol FROM instrument "
                       "WHERE active=1 ORDER BY market, instr_id").fetchall()
    todo = []
    for iid, mkt, sym in rows:
        _known = {"KRX": KRX_PROD, "CME": CME_PROD, "EUREX": EUREX_PROD}
        if mkt not in _known or iid not in _known[mkt]:
            print("   %-6s %-5s %-9s 스킵(상품코드 미등록)" % (iid, mkt, sym)); continue
        d = decide(iid, mkt, sym, today, use_api=not a.no_api)
        vol = ""
        if d["vol_cur"] is not None or d["vol_next"] is not None:
            vol = " 거래량 %s→%s" % (d["vol_cur"], d["vol_next"])
        print("   %-6s %-5s %-9s → %-9s 만기 %s (영업일 %+d)%s  %s"
              % (iid, mkt, d["cur"], d["next"], d["expiry"], d["busdays_left"], vol,
                 ("★ 롤 필요 (%s)" % d["reason"]) if d["roll"] else "유지"))
        if d["roll"]:
            todo.append(d)

    if not todo:
        print("\n지금 옮길 종목 없음.")
        return 0
    if not a.apply:
        print("\n%d종목이 롤 대상입니다. 실제 적용은 --apply." % len(todo))
        return 0
    for d in todo:
        px_to = None
        if d["market"] == "KRX" and not a.no_api:
            try:
                from ls_openapi import call_tr
                j = call_tr("kr_futopt", "/futureoption/chart", "t8461",
                            {"t8461InBlock": {"focode": d["next"], "cgubun": "B",
                                              "bgubun": "1", "cnt": 1}})
                r = j.get("t8461OutBlock1", [])
                r = [r] if isinstance(r, dict) else r
                if r:
                    px_to = float(r[0].get("price") or 0) or None
            except Exception as e:
                print("   %s 새 계약 가격 조회 실패: %s" % (d["instr_id"], str(e)[:80]))
        apply_roll(con, d, px_to)
        print("   적용: %s %s → %s (gap %s)"
              % (d["instr_id"], d["cur"], d["next"],
                 "미상" if px_to is None else "%.4f" % (px_to - (last_close(con, d['instr_id'])[1] or px_to))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
