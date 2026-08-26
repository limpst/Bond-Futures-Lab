# -*- coding: utf-8 -*-
"""FX 다리 소스 탐색 — CME 원/달러 선물(6K)을 LS WebSocket 으로 받을 수 있나.

배경: LS 해외선물 마스터(o3121)에는 통화 상품이 CNH 하나뿐이고 원화가 없다.
그런데 CME 채권선물(ZN 등)도 마스터에 없는데 WebSocket(OVC)으로는 정상 수신된다.
즉 **마스터 목록과 실시간 구독 가능 목록이 다를 수 있다.** 그러니 6K 를 직접
구독해 보고 판단한다 — 이것이 FX 소스 1순위(같은 계정·같은 파이프라인).

  python tools/probe_fx_symbol.py                  기본 90초 청취
  python tools/probe_fx_symbol.py --sec 150 --syms 6KU26,6KZ26,KRWU26
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(ROOT / "tools"))
from ls_openapi import issue_token, load_env  # noqa: E402

WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
CHANNEL = "os_futopt"
TR = "OVC"


async def listen(syms, sec):
    import websockets
    load_env()
    tok = issue_token(CHANNEL)
    got = {s: 0 for s in syms}
    sample = {}
    other = {}
    async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
        for s in syms:
            await ws.send(json.dumps({
                "header": {"token": tok, "tr_type": "3"},
                "body": {"tr_cd": TR, "tr_key": s.ljust(8)}}))
            await asyncio.sleep(0.2)
        print(f"구독 요청: {', '.join(syms)} — {sec}초 청취")
        end = asyncio.get_event_loop().time() + sec
        while asyncio.get_event_loop().time() < end:
            left = end - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(15, max(1, left)))
            except asyncio.TimeoutError:
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            h = m.get("header") or {}
            b = m.get("body") or {}
            sym = str(b.get("symbol") or "").strip()
            if not sym:
                rsp = str(h.get("rsp_msg") or "")
                if rsp:
                    other[rsp] = other.get(rsp, 0) + 1
                continue
            if sym in got:
                got[sym] += 1
                if sym not in sample:
                    sample[sym] = {k: b.get(k) for k in
                                   ("curpr", "kordate", "kortm", "totq", "trdp") if k in b}
            else:
                other[f"other:{sym}"] = other.get(f"other:{sym}", 0) + 1
    return got, sample, other


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=int, default=90)
    ap.add_argument("--syms", default="6KU26,6KZ26,6EU26")
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(",") if s.strip()]
    got, sample, other = asyncio.run(listen(syms, a.sec))
    print("\n[결과]")
    for s in syms:
        n = got.get(s, 0)
        print(f"  {s:<8} tick {n:>5}  {'🟢 수신됨' if n else '🔴 무응답'}"
              f"  {sample.get(s, '')}")
    if other:
        print("  기타 메시지:", dict(list(other.items())[:6]))
    live = [s for s in syms if got.get(s)]
    print("판정:", f"🟢 {', '.join(live)} 구독 가능 — FX 다리를 LS 로 받을 수 있다"
          if live else
          "🔴 전부 무응답 — LS 로는 원화 선물 수신 불가 (다른 소스 필요)")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
