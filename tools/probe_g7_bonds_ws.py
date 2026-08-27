# -*- coding: utf-8 -*-
"""G7 국채선물이 LS Open API 로 흐르는지 WebSocket 으로 전수 판정한다.

왜 REST 마스터를 안 믿나 (이 랩이 이미 치른 수업료):
  같은 계정에서 CME 는 REST 조회가 전부 빈손이었다 — o3121 마스터 0종,
  o3106 현재가 빈 응답, o3103 차트 0봉. 그런데 실시간 WebSocket OVC 로는
  ZNU26 체결이 멀쩡히 들어왔다(2026-08-24 실측, probe_cme_feed.py).
  즉 **마스터에 없다 != 못 받는다**. 판정 축을 WS 로 옮긴다.

왜 rsp_cd 도 안 믿나 (2026-08-27 실측):
  존재하지도 않는 심볼(MJGBU26)에도 LS 는 rsp_cd=00000 "정상처리되었습니다"
  를 돌려준다. ack 는 판별력이 없다. **실제 tick 만이 증거다.**

거래시간 (tick 0 을 해석할 때 반드시 본다):
  CBOT   일~금 08:00-06:00 KST 근방(거의 24h)
  Eurex  01:10-22:00 CET  = 08:10-05:00 KST
  ICE    08:00-18:00 London = 16:00-02:00 KST
  OSE    주간 08:45-15:02 JST(=KST) · 야간 15:30-06:00
  MX(CA) 06:00-16:00 ET = 19:00-05:00 KST
  -> 전 국가를 한 번에 덮는 창은 20:00-01:00 KST.

조회 전용 — 주문 TR 은 호출하지 않는다. 자격증명은 출력하지 않는다.

  python tools/probe_g7_bonds_ws.py --seconds 120
  python tools/probe_g7_bonds_ws.py FGBLU26 JBU26     심볼 직접 지정
  python tools/probe_g7_bonds_ws.py --json out.json   결과를 파일로
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import asyncio
import json
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ls_openapi import issue_token  # noqa: E402

import websockets  # noqa: E402

WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
CHANNEL = "os_futopt"
TR = "OVC"

# LS 해외선물 심볼 = <root><월코드><연2자리> (ZNU26 · HSIU26 · LHCU26 · FGBLU26 확인).
# G7 국채선물 root 는 브로커마다 표기가 갈린다. 통용 표기를 전수로 던져 tick 으로 가린다.
G7_ROOTS = [
    ("ZT",   "US", "CBOT 2Y T-Note"),
    ("ZF",   "US", "CBOT 5Y T-Note"),
    ("ZN",   "US", "CBOT 10Y T-Note"),
    ("TN",   "US", "CBOT Ultra 10Y"),
    ("ZB",   "US", "CBOT 30Y T-Bond"),
    ("UB",   "US", "CBOT Ultra T-Bond"),

    ("FGBS", "DE", "Eurex Schatz 2Y"),
    ("FGBM", "DE", "Eurex Bobl 5Y"),
    ("FGBL", "DE", "Eurex Bund 10Y"),
    ("FGBX", "DE", "Eurex Buxl 30Y"),

    ("FOAT", "FR", "Eurex OAT 10Y"),
    ("OAT",  "FR", "Eurex OAT - 축약 root"),

    ("FBTP", "IT", "Eurex Long BTP 10Y"),
    ("FBTS", "IT", "Eurex Short BTP 2Y"),
    ("FBTM", "IT", "Eurex Mid BTP 5Y"),

    ("R",    "UK", "ICE Long Gilt - ICE root R"),
    ("FLG",  "UK", "ICE Long Gilt - FLG"),
    ("LGL",  "UK", "ICE Long Gilt - LGL"),
    ("GILT", "UK", "ICE Long Gilt - 서술형"),

    ("JB",   "JP", "OSE 10Y JGB - JB"),
    ("JGB",  "JP", "OSE 10Y JGB - JGB"),
    ("JGBL", "JP", "OSE 10Y JGB - JGBL"),
    ("JBM",  "JP", "OSE mini JGB - JBM"),
    ("JGBM", "JP", "SGX mini JGB - JGBM"),
    ("SJB",  "JP", "SGX 10Y JGB - SJB"),
    ("JPB",  "JP", "JGB - JPB"),

    ("CGB",  "CA", "Montreal 10Y Canada"),
    ("CGF",  "CA", "Montreal 5Y Canada"),
    ("CGZ",  "CA", "Montreal 2Y Canada"),
]

# 2026-08 말은 U26(9월) -> Z26(12월) 롤 구간. Eurex 는 U26 수신이 확인돼 U26 만.
MONTHS = {"DE": ["U26"], "FR": ["U26", "Z26"], "IT": ["U26", "Z26"],
          "US": ["U26", "Z26"], "UK": ["U26", "Z26"],
          "JP": ["U26", "Z26"], "CA": ["U26", "Z26"]}

CANDIDATES = [(r + m, c, "%s (%s)" % (n, m))
              for r, c, n in G7_ROOTS for m in MONTHS[c]]

CNAME = {"US": "미국", "DE": "독일", "FR": "프랑스", "IT": "이탈리아",
         "UK": "영국", "JP": "일본", "CA": "캐나다", "MANUAL": "직접지정"}
CORDER = ["US", "DE", "FR", "IT", "UK", "JP", "CA", "MANUAL"]


def now_kst() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime(
        "%Y-%m-%d %H:%M:%S")


async def run(syms, seconds: int) -> dict:
    state = {s: {"grp": g, "note": n, "rsp_cd": None, "rsp_msg": None,
                 "ticks": 0, "last_px": None, "last_tm": None}
             for s, g, n in syms}
    order = [s for s, _, _ in syms]
    cursor = [0]                      # ack 도착 순서 커서

    tok = issue_token(CHANNEL, force=True)
    print("토큰 재발급 완료 (값 미출력) - KST %s" % now_kst())

    async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
        for sym in order:
            await ws.send(json.dumps({
                "header": {"token": tok, "tr_type": "3"},
                "body": {"tr_cd": TR, "tr_key": sym.ljust(8)}}))
            await asyncio.sleep(0.12)
        print("구독 %d종 전송 - %d초 수신" % (len(order), seconds))
        print("")

        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            left = end - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(),
                                             timeout=min(10, max(1, left)))
            except asyncio.TimeoutError:
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            h = m.get("header") or {}
            b = m.get("body") or {}

            # ack: LS 는 tr_key 를 안 실어 보낸다 -> 구독 순서로 매핑.
            # (없는 심볼에도 00000 이 오므로 기록만 하고 판정엔 안 쓴다)
            if h.get("rsp_cd") is not None and "curpr" not in b:
                key = str(h.get("tr_key") or "").strip()
                if key in state:
                    tgt = key
                else:
                    tgt = order[cursor[0]] if cursor[0] < len(order) else None
                    cursor[0] += 1
                if tgt:
                    state[tgt]["rsp_cd"] = str(h.get("rsp_cd"))
                    state[tgt]["rsp_msg"] = str(h.get("rsp_msg") or "")[:40]
                continue

            sym = str(b.get("symbol") or "").strip()
            if sym in state and "curpr" in b:
                st = state[sym]
                st["ticks"] += 1
                st["last_px"] = str(b.get("curpr")).strip()
                st["last_tm"] = "%s %s" % (str(b.get("kordate") or ""),
                                           str(b.get("kortm") or ""))
                if st["ticks"] == 1:
                    print("  [tick] %-9s %-12s KST %s"
                          % (sym, st["last_px"], st["last_tm"]))
    return state


def verdict(state: dict) -> None:
    line = "=" * 82
    print("")
    print(line)
    print("판정 - tick > 0 만이 증거 (rsp_cd 는 없는 심볼에도 00000 을 준다)")
    print(line)
    live = {s: st for s, st in state.items() if st["ticks"] > 0}
    if live:
        print("%-10s %-6s %6s  %-12s %s"
              % ("심볼", "국가", "tick", "최종가", "설명"))
        print("-" * 82)
        for s, st in sorted(live.items(), key=lambda kv: -kv[1]["ticks"]):
            print("%-10s %-6s %6d  %-12s %s"
                  % (s, CNAME.get(st["grp"], st["grp"]), st["ticks"],
                     st["last_px"], st["note"]))
    else:
        print("  (수신된 심볼 없음)")

    print("")
    print("국가별 요약")
    print("-" * 82)
    for c in CORDER:
        rows = [(s, st) for s, st in state.items() if st["grp"] == c]
        if not rows:
            continue
        hit = [s for s, st in rows if st["ticks"] > 0]
        print("  [%s] %-6s 시도 %2d종 / 수신 %d종   %s"
              % ("O" if hit else "X", CNAME.get(c, c), len(rows), len(hit),
                 ", ".join(hit) if hit else "-"))
    print("")
    print("주의: tick 0 은 (a) 심볼 표기 불일치 (b) 시세 권한 없음 (c) 장 시간 밖")
    print("      세 원인을 이 프로브만으로 구분할 수 없다. 시간대를 바꿔 재실행할 것.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    syms = ([(s.upper(), "MANUAL", "직접 지정") for s in a.symbols]
            if a.symbols else list(CANDIDATES))
    print("G7 국채선물 WebSocket 프로브 - %d종 / %d초 (조회 전용)"
          % (len(syms), a.seconds))
    try:
        state = asyncio.run(run(syms, a.seconds))
    except Exception as e:
        print("실패: %s" % str(e)[:300])
        return 1
    verdict(state)
    if a.json:
        Path(a.json).write_text(
            json.dumps({"probed_at_kst": now_kst(), "result": state},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("")
        print("저장: %s" % a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
