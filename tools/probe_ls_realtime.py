# -*- coding: utf-8 -*-
"""LS 실시간 WebSocket — tick 이 실제로 들어오는지 판정한다.

배경: PLANC_RMS_v7 의 '실시간·MTM' 메뉴는 LS 실시간 TR 구독으로 체결 tick 을
받아 포지션 MTM 을 재계산한다. 그런데 같은 계정의 조회 API 는 국내 파생에서
야간 세션 봉만, 해외(CME)는 아무것도 주지 않는 것이 실측되었다. 실시간
채널도 같은 벽에 막히는지 확인한다.

프로토콜 (PLANC_RMS_v7 app/ls_ws_raw.py 와 동일):
  wss://openapi.ls-sec.co.kr:9443/websocket
  {"header": {"token": <access>, "tr_type": "3"},
   "body":   {"tr_cd": <TR>, "tr_key": <symbol, 우측 공백 패딩>}}
  tr_type 3=시세구독 · 2=해제

조회 전용이다 — 주문 이벤트 TR(SC0/SC1/FC1/TC1..)은 구독하지 않는다.

  python tools/probe_ls_realtime.py [관측초]
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

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ls_openapi import issue_token  # noqa: E402

WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"

# (채널, TR, 심볼, 설명) — 시세 계열만. 심볼 길이는 LS 규격대로 패딩한다.
TARGETS = [
    ("kr_futopt", "FC0", "A0569000", "국내 KOSPI200 선물 체결 (구 TR)"),
    ("kr_futopt", "FC9", "A0569000", "국내 KOSPI200 선물 체결 (신 TR)"),
    ("kr_futopt", "FH9", "A0569000", "국내 KOSPI200 선물 호가"),
    ("kr_futopt", "FC9", "A6569000", "KTB3 선물 체결"),
    ("kr_futopt", "FC9", "A6769000", "KTB10 선물 체결"),
    ("os_futopt", "OVC", "ZNU26",    "해외선물 체결 (CME 미국채 10Y)"),
    ("os_futopt", "OVH", "ZNU26",    "해외선물 호가 (CME 미국채 10Y)"),
    ("os_futopt", "OVC", "HSIQ26",   "해외선물 체결 (HKEX — 대조군)"),
]
PAD = {"FC0": 8, "FC9": 8, "FH0": 8, "FH9": 8, "OC0": 8, "OH0": 8,
       "OVC": 8, "OVH": 8, "S3_": 6}


def pad(tr: str, key: str) -> str:
    n = PAD.get(tr.upper(), 0)
    return key.ljust(n) if n else key


async def run_channel(channel: str, subs: list[tuple[str, str, str]], secs: int):
    try:
        import websockets
    except ImportError:
        print("  websockets 미설치 — pip install websockets")
        return {}
    token = issue_token(channel)
    got = Counter()
    errs = []
    try:
        async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
            for tr, key, desc in subs:
                await ws.send(json.dumps({
                    "header": {"token": token, "tr_type": "3"},
                    "body": {"tr_cd": tr.upper(), "tr_key": pad(tr, key)}}))
                await asyncio.sleep(0.25)
            print("  [%s] 구독 %d건 전송 · %d초 관측" % (channel, len(subs), secs))
            end = asyncio.get_event_loop().time() + secs
            while asyncio.get_event_loop().time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1, end - asyncio.get_event_loop().time()))
                except asyncio.TimeoutError:
                    break
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                h = m.get("header") or {}
                b = m.get("body") or {}
                tr = str(h.get("tr_cd") or b.get("tr_cd") or "?")
                rc = str(h.get("rsp_cd") or m.get("rsp_cd") or "")
                msg = str(h.get("rsp_msg") or m.get("rsp_msg") or "")
                if msg or (rc and rc not in ("00000", "0")):
                    errs.append("%s rsp_cd=%s %s" % (tr, rc or "-", msg[:80]))
                # 심볼까지 함께 센다 — 어느 거래소가 살아 있는지 갈라야 한다
                sym = str(b.get("symbol") or b.get("shcode") or b.get("futcode")
                          or h.get("tr_key") or b.get("tr_key") or "?").strip()
                if b and any(k for k in b if k not in ("tr_cd", "tr_key")):
                    got["%s/%s" % (tr, sym)] += 1
    except Exception as e:
        print("  [%s] 연결 실패: %s" % (channel, str(e)[:140]))
        return {}
    return {"ticks": got, "msgs": errs}


async def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print("LS 실시간 WebSocket 진단 · 관측 %d초/채널\n" % secs)
    for channel in ("kr_futopt", "os_futopt"):
        subs = [(tr, k, d) for ch, tr, k, d in TARGETS if ch == channel]
        r = await run_channel(channel, subs, secs)
        if not r:
            continue
        ticks = r["ticks"]
        print("    수신 tick: %s" % (dict(ticks) if ticks else "없음"))
        seen = set()
        for m in r["msgs"]:
            if m not in seen:
                seen.add(m)
                print("    응답: %s" % m)
        print()
    print("판정 기준 — 야간 세션(18:00~) 중이면 국내 선물 FC9 에 tick 이 와야 정상.")
    print("           해외(OVC/OVH)는 CME 유료시세 미신청이면 등록 자체가 거부된다.")


if __name__ == "__main__":
    asyncio.run(main())
