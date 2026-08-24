# -*- coding: utf-8 -*-
"""LS Open API 해외선물옵션 채널 — CME 시세가 실제로 흐르는지 진단한다.

배경: 계좌에 CME 시세를 신청해 두었으나 "API 유니버스 반영 대기" 상태였다.
반영 여부는 마스터(o3121)에 CME 상품이 나오는지, 그리고 현재가(o3106)와
차트(o3103)가 값을 주는지로 판정된다. 이 스크립트가 그 판정을 한다.

조회 전용이다 — 주문 TR 은 호출하지 않는다.
자격증명 값은 어떤 출력에도 찍지 않는다.

  python tools/probe_cme_feed.py [심볼 ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ls_openapi import CHANNELS, call_tr, issue_token, load_env  # noqa: E402

CH = "os_futopt"
MKT = "/overseas-futureoption/market-data"
CHART = "/overseas-futureoption/chart"

# 채권 선물 위주 + 지수·원자재 대표. U=9월, Z=12월물.
DEFAULT_SYMS = ["ZNU26", "ZNZ26", "ZBU26", "ZBZ26", "ZFU26", "ZTU26",
                "TNU26", "ESU26", "ESZ26", "CLV26"]


def hr(t):
    print("\n" + "─" * 4 + " " + t + " " + "─" * max(4, 66 - len(t)))


def probe_token():
    hr("1. 토큰 발급")
    env = load_env()
    k, s = CHANNELS[CH]
    if not (env.get(k) and env.get(s)):
        print("  ✗ .env.ls 에 %s / %s 가 없습니다" % (k, s))
        return False
    print("  자격증명: 존재 (값은 출력하지 않음)")
    try:
        tok = issue_token(CH)
        print("  ✓ access token 발급 성공 (길이 %d)" % len(tok))
        return True
    except Exception as e:
        print("  ✗ 토큰 발급 실패: %s" % str(e)[:200])
        return False


def probe_master():
    """o3121 — 계정에 반영된 해외선물 상품 목록. CME 계열이 보이면 시세 반영됨."""
    hr("2. 상품 마스터 (o3121) — 계정에 어떤 거래소가 열려 있나")
    try:
        j = call_tr(CH, MKT, "o3121", {"o3121InBlock": {"MktGb": "F", "BscGdsCd": ""}})
    except Exception as e:
        print("  ✗ 조회 실패: %s" % str(e)[:250])
        return {}
    rows = j.get("o3121OutBlock") or []
    if isinstance(rows, dict):
        rows = [rows]
    print("  응답 행수: %d" % len(rows))
    if not rows:
        print("  → 상품이 하나도 없습니다. 계정에 해외선물 시세가 아직 반영되지 않은 상태.")
        return {}
    exch: dict[str, list] = {}
    for r in rows:
        exch.setdefault(str(r.get("ExchCd") or "?"), []).append(r)
    print("  거래소별 상품 수:")
    for e, rs in sorted(exch.items(), key=lambda kv: -len(kv[1])):
        cds = sorted({str(x.get("BscGdsCd") or "") for x in rs})
        print("    %-8s %4d행 · 상품 %d종  %s"
              % (e, len(rs), len(cds), " ".join(cds[:14]) + (" …" if len(cds) > 14 else "")))
    cme = {e: rs for e, rs in exch.items()
           if e.upper() in ("CME", "CBOT", "NYMEX", "COMEX", "CBT")}
    print("  CME 계열 거래소: %s" % (", ".join(sorted(cme)) if cme else "없음 ← 시세 미반영"))
    return exch


def probe_quote(syms):
    """o3106 — 심볼별 현재가. 값이 오면 그 심볼은 시세가 열려 있다."""
    hr("3. 현재가 (o3106) — 심볼별 수신 여부")
    ok = []
    for s in syms:
        try:
            j = call_tr(CH, MKT, "o3106", {"o3106InBlock": {"symbol": s}})
        except Exception as e:
            print("  %-8s ✗ %s" % (s, str(e)[:90]))
            continue
        b = j.get("o3106OutBlock") or {}
        if not b:
            print("  %-8s ✗ 응답 블록 없음" % s)
            continue
        px = b.get("price") or b.get("curpr") or b.get("close")
        vol = b.get("volume")
        if px in (None, "", 0, "0"):
            print("  %-8s △ 응답은 왔으나 가격이 비어 있음 (필드: %s)"
                  % (s, ",".join(list(b)[:8])))
            continue
        print("  %-8s ✓ price=%s volume=%s" % (s, px, vol))
        ok.append(s)
    return ok


def probe_chart(sym):
    """o3103 — 분봉. 백필에 실제로 쓸 수 있는지 최종 확인."""
    hr("4. 분봉 차트 (o3103) — 백필 가능 여부  [%s]" % sym)
    body = {"o3103InBlock": {"shcode": sym, "ncnt": 1, "qrycnt": 20,
                             "cts_date": "", "cts_time": ""}}
    try:
        j = call_tr(CH, CHART, "o3103", body)
    except Exception as e:
        print("  ✗ 조회 실패: %s" % str(e)[:250])
        return
    rows = j.get("o3103OutBlock1") or []
    if isinstance(rows, dict):
        rows = [rows]
    print("  봉 수: %d" % len(rows))
    for r in rows[:5]:
        print("    %s %s  O=%s H=%s L=%s C=%s V=%s"
              % (r.get("date"), r.get("time"), r.get("open"), r.get("high"),
                 r.get("low"), r.get("close"), r.get("volume")))
    if not rows:
        print("  → 봉이 0개. 시세 미반영이거나 휴장/심볼 불일치.")


def main():
    syms = sys.argv[1:] or DEFAULT_SYMS
    print("LS Open API 해외선물옵션(os_futopt) 채널 진단")
    if not probe_token():
        return 1
    exch = probe_master()
    live = probe_quote(syms)
    probe_chart(live[0] if live else syms[0])
    hr("판정")
    cme_open = any(e.upper() in ("CME", "CBOT", "NYMEX", "COMEX", "CBT") for e in exch)
    print("  마스터에 CME 계열: %s" % ("있음 ✓" if cme_open else "없음 ✗"))
    print("  현재가 수신 심볼 : %d/%d  %s" % (len(live), len(syms), " ".join(live)))
    if cme_open and live:
        print("  → CME 시세가 API 에 반영되어 있습니다. 백필 착수 가능.")
    elif exch and not cme_open:
        print("  → 계정은 살아 있으나 CME 계열이 유니버스에 없습니다. 시세 신청 반영 대기.")
    else:
        print("  → 해외선물 시세 자체가 열려 있지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
