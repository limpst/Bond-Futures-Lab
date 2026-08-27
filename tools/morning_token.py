# -*- coding: utf-8 -*-
"""LS 아침 토큰 갱신 — 하루 한 번 도는 '서비스 재시작'.

왜 필요한가 (2026-08-27 확정):
  LS 증권 Open API 의 access token 은 **하루 단위로 죽는다**. 그런데
  `ls_openapi.issue_token` 은 응답의 `expires_in` 을 믿고 캐시를 재사용하고,
  그 값이 실제와 안 맞는 것이 이미 실측됐다(잔여 170분으로 계산된 캐시에
  서버가 IGW00121 을 돌려줌 — `ls_openapi.call_tr` 주석).

  REST 는 거부당하면 강제 재발급 후 한 번 재시도하는 훅이 있지만,
  **WebSocket 은 그런 훅이 없다.** 08-24 · 08-25 · 08-26 사흘 연속 CME 야간
  수집이 조용히 멈춘 원인이 이것이다(최대 965분 결측).

  그래서 두 겹으로 막는다:
    ① 수집기 쪽 — WS 재접속마다 `issue_token(force=True)` (collect_*_ws.py)
    ② 이 스크립트 — 장 시작 전에 **캐시를 버리고 새로 받아** 하루를 시작한다

무엇을 하나:
  1) `.env.ls` 에 자격증명이 있는 채널마다 토큰을 **강제 재발급**한다.
  2) 실제 TR 을 한 번 때려 그 토큰이 **정말 먹히는지** 확인한다
     (발급 성공 ≠ 사용 가능 — 이게 이번 사고의 교훈이다).
  3) 결과를 화면과 `collect_log`(instr_id='TOKEN') 양쪽에 남긴다.
  4) 하나라도 실패하면 **exit 1** — 작업 스케줄러가 실패로 표시하게 한다.

  python tools/morning_token.py            갱신 + 검증
  python tools/morning_token.py --no-verify  발급만 (네트워크 아낌)
"""
from __future__ import annotations

import sys as _sys
# 작업 스케줄러 콘솔은 cp949 라 '—' 같은 문자에서 UnicodeEncodeError 로 죽는다.
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
from ls_openapi import CHANNELS, call_tr, issue_token, load_env  # noqa: E402

DB = ROOT / "data" / "minbars.db"

# 채널별 '이 토큰이 진짜 먹히나' 확인용 최소 호출 — 봉 1개만 달라고 한다.
VERIFY = {
    "kr_futopt": dict(path="/futureoption/chart", tr_cd="t8461",
                      body_of=lambda sym: {"t8461InBlock": {
                          "focode": sym, "cgubun": "B", "bgubun": "1", "cnt": 1}},
                      out="t8461OutBlock1", default_sym="A6769000"),
    "os_futopt": dict(path="/overseas-futureoption/chart", tr_cd="o3103",
                      body_of=lambda sym: {"o3103InBlock": {
                          "shcode": sym, "ncnt": 1, "readcnt": 1,
                          "cts_date": "", "cts_time": ""}},
                      out="o3103OutBlock1", default_sym="ZNU26"),
    # general 채널은 조회 전용 TR 을 쓰지 않으므로 발급까지만 확인한다.
}


def _sym_for(channel: str, fallback: str) -> str:
    """DB 에 등록된 활성 종목의 실제 월물 코드를 쓴다 (월물이 굴러가도 안 깨지게)."""
    market = "KRX" if channel == "kr_futopt" else "CME"
    try:
        con = sqlite3.connect(DB, timeout=30)
        r = con.execute("SELECT symbol FROM instrument"
                        " WHERE market=? AND active=1 AND symbol<>''"
                        " ORDER BY instr_id LIMIT 1", (market,)).fetchone()
        con.close()
        if r and r[0]:
            return r[0].strip()
    except sqlite3.Error:
        pass
    return fallback


def _log(status: str, detail: str) -> None:
    try:
        con = sqlite3.connect(DB, timeout=30)
        con.execute(
            "INSERT INTO collect_log(ts_utc,instr_id,tr_cd,rows_in,rows_new,status,detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "TOKEN", "oauth2", 0, 0, status, detail[:500]))
        con.commit(); con.close()
    except sqlite3.Error as e:
        print("  (collect_log 기록 실패: %s)" % e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true",
                    help="발급만 하고 TR 확인은 건너뛴다")
    a = ap.parse_args()

    env = load_env()
    print("LS 아침 토큰 갱신 · %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    ok, fail, skipped = [], [], []
    for ch in CHANNELS:
        kkey, ksec = CHANNELS[ch]
        if not (env.get(kkey) and env.get(ksec)):
            skipped.append(ch)
            print("  %-10s 자격증명 없음 — 건너뜀" % ch)
            continue
        try:
            issue_token(ch, env, force=True)        # ★ 캐시를 버리고 새로 받는다
        except Exception as e:
            fail.append((ch, "발급 실패: %s" % str(e)[:160]))
            print("  %-10s ✗ 발급 실패 — %s" % (ch, str(e)[:120]))
            continue
        if a.no_verify or ch not in VERIFY:
            ok.append(ch)
            print("  %-10s ✓ 발급 완료 (검증 생략)" % ch)
            continue
        v = VERIFY[ch]
        sym = _sym_for(ch, v["default_sym"])
        try:
            j = call_tr(ch, v["path"], v["tr_cd"], v["body_of"](sym))
            rows = j.get(v["out"], [])
            n = len(rows) if isinstance(rows, list) else 1
            ok.append(ch)
            print("  %-10s ✓ 발급 + 검증 (%s %s → %d행)" % (ch, v["tr_cd"], sym, n))
        except Exception as e:
            fail.append((ch, "검증 실패(%s): %s" % (v["tr_cd"], str(e)[:140])))
            print("  %-10s ✗ 발급은 됐으나 TR 이 거부 — %s" % (ch, str(e)[:120]))

    detail = "ok=%s fail=%s skip=%s" % (",".join(ok) or "-",
                                        ";".join("%s(%s)" % f for f in fail) or "-",
                                        ",".join(skipped) or "-")
    _log("ok" if not fail else "error", detail)
    print("\n  결과: 성공 %d · 실패 %d · 건너뜀 %d" % (len(ok), len(fail), len(skipped)))
    if fail:
        print("  ⚠️ 실패한 채널이 있습니다 — 오늘 그 채널의 수집은 멈춥니다.")
        for ch, why in fail:
            print("     · %s: %s" % (ch, why))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
