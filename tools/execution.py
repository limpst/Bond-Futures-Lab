# -*- coding: utf-8 -*-
"""OMS (order management) — ★ 기본 dry-run. 실계좌 주문은 3중 안전장치 뒤에 있다.

설계 (2026-08-23 지시: "일단은 dry-run 주문, 글로벌 스위치로 실계좌 전환 가능하게,
나중에 RMS(증거금·leverage·margin ratio·스트레스) 연결"):

  1) 글로벌 스위치  execution_mode.json {"mode": "dry"|"live"}
       기본 dry. 전환은 사람이 CLI 로 직접:  python tools/execution.py --arm-live
       (확인 문자열 입력 필요) · 해제: --disarm
  2) RMS 게이트     live 주문은 rms_check() 를 통과해야 한다. RMS(증거금·MR·
       leverage·스트레스 테스트)가 아직 연결되지 않았으므로 **live 는 무조건
       차단**된다 — RMS 를 붙이기 전에는 스위치를 켜도 주문이 나가지 않는다.
  3) 주문 TR 미구현  실주문 TR(국내 CFOAT00100 계열·해외 CIDBT 계열)은 LS 문서
       대조·모의 검증 전이라 body 를 채우지 않았다 — live 경로는 구조만 있고
       NotImplemented 로 멈춘다.

dry 주문은 data/minbars.db 의 oms_order 테이블에 기록된다 (사후 감사 가능).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "minbars.db"
MODE_FILE = ROOT / "execution_mode.json"
RMS_FILE = ROOT / "rms_limits.json"          # RMS 연결 전의 한도 선언(문서용)

DEFAULT_RMS = {
    "connected": False,                       # ← RMS 실연결 전에는 live 전면 차단
    "max_margin_ratio": 0.30,                 # 증거금/예수금 상한
    "max_leverage": 3.0,
    "max_contracts_per_pair": 1,              # 소액 원칙
    "require_stress_pass": True,              # 스트레스 테스트 통과 요구
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS oms_order(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  mode TEXT NOT NULL,                -- dry · live
  pair TEXT NOT NULL,                -- 예: KTB3-KTB10
  action TEXT NOT NULL,              -- enter_long_spread · enter_short_spread · exit
  leg1_instr TEXT, leg1_side TEXT, leg1_qty INTEGER, leg1_ref_price REAL,
  leg2_instr TEXT, leg2_side TEXT, leg2_qty INTEGER, leg2_ref_price REAL,
  reason TEXT,                       -- 시그널 근거 (z, iqr, ecm)
  status TEXT NOT NULL,              -- logged(dry) · blocked_rms · error
  detail TEXT
);
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


def get_mode() -> str:
    if MODE_FILE.is_file():
        try:
            return json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode", "dry")
        except ValueError:
            pass
    return "dry"


def set_mode(mode: str):
    MODE_FILE.write_text(json.dumps(
        {"mode": mode, "changed_utc": f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}"},
        ensure_ascii=False, indent=1), encoding="utf-8")


def rms_limits() -> dict:
    if RMS_FILE.is_file():
        try:
            return {**DEFAULT_RMS, **json.loads(RMS_FILE.read_text(encoding="utf-8"))}
        except ValueError:
            pass
    RMS_FILE.write_text(json.dumps(DEFAULT_RMS, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return dict(DEFAULT_RMS)


def rms_check(order: dict) -> tuple[bool, str]:
    """live 주문 사전 점검. RMS 미연결이면 무조건 불통과."""
    lim = rms_limits()
    if not lim.get("connected"):
        return False, "RMS 미연결 — 증거금/MR/leverage/스트레스 점검 불가, live 차단"
    if max(order.get("leg1_qty", 0), order.get("leg2_qty", 0)) > lim["max_contracts_per_pair"]:
        return False, f"계약 수 한도 초과 (>{lim['max_contracts_per_pair']})"
    # TODO(RMS): 실계좌 증거금·margin ratio·leverage·스트레스 결과 조회 후 판정
    return False, "RMS 세부 점검 미구현 — 안전측 차단"


def place_pair_order(pair: str, action: str, legs: list[dict], reason: str) -> str:
    """스프레드 양다리 주문 한 건. mode 에 따라 dry 기록 또는 live 게이트."""
    con = _sqlite_connect_safe(DB, timeout=60)
    con.executescript(SCHEMA)
    mode = get_mode()
    row = {
        "ts_utc": f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}",
        "mode": mode, "pair": pair, "action": action, "reason": reason,
        "leg1_instr": legs[0]["instr"], "leg1_side": legs[0]["side"],
        "leg1_qty": legs[0]["qty"], "leg1_ref_price": legs[0].get("ref_price"),
        "leg2_instr": legs[1]["instr"], "leg2_side": legs[1]["side"],
        "leg2_qty": legs[1]["qty"], "leg2_ref_price": legs[1].get("ref_price"),
    }
    if mode == "live":
        ok, why = rms_check(row)
        if not ok:
            row["status"], row["detail"] = "blocked_rms", why
        else:
            # 실주문 TR 은 LS 문서 대조·모의 검증 전 — 의도적으로 미구현.
            row["status"], row["detail"] = "error", "live order TR not implemented"
    else:
        row["status"], row["detail"] = "logged", "dry-run"
    cols = ",".join(row)
    con.execute(f"INSERT INTO oms_order({cols}) VALUES({','.join('?'*len(row))})",
                list(row.values()))
    con.commit()
    con.close()
    return row["status"]


def main() -> int:
    ap = argparse.ArgumentParser(description="OMS — 기본 dry, live 는 3중 안전장치")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--arm-live", action="store_true", help="실계좌 모드 켜기 (확인 필요)")
    ap.add_argument("--disarm", action="store_true", help="dry 모드로 복귀")
    ap.add_argument("--test-dry-order", action="store_true",
                    help="dry 주문 1건 기록 (파이프라인 시험)")
    a = ap.parse_args()

    if a.arm_live:
        print("⚠️  실계좌 주문 모드를 켜려 합니다. RMS 미연결 상태에서는 켜도 주문이 차단됩니다.")
        ans = input("정확히 'LIVE ORDERS' 를 입력하십시오: ")
        if ans.strip() == "LIVE ORDERS":
            set_mode("live")
            print("[mode] live — 단, RMS 게이트가 모든 주문을 점검/차단합니다.")
        else:
            print("[mode] 변경 취소")
        return 0
    if a.disarm:
        set_mode("dry")
        print("[mode] dry")
        return 0
    if a.test_dry_order:
        st = place_pair_order(
            "KTB3-KTB10", "enter_long_spread",
            [{"instr": "KTB3", "side": "BUY", "qty": 1, "ref_price": None},
             {"instr": "KTB10", "side": "SELL", "qty": 1, "ref_price": None}],
            reason="pipeline smoke test")
        print(f"[test] dry order status={st}")
        return 0

    lim = rms_limits()
    print(f"[status] mode={get_mode()} · RMS connected={lim['connected']} "
          f"· MR≤{lim['max_margin_ratio']} · lev≤{lim['max_leverage']} "
          f"· pair당 {lim['max_contracts_per_pair']}계약")
    if DB.is_file():
        con = _sqlite_connect_safe(DB, timeout=60)
        try:
            n = con.execute("SELECT COUNT(*) FROM oms_order").fetchone()[0]
            last = con.execute("SELECT ts_utc,mode,pair,action,status FROM oms_order "
                               "ORDER BY id DESC LIMIT 3").fetchall()
            print(f"[status] oms_order {n}건 · 최근: {last}")
        except sqlite3.OperationalError:
            print("[status] oms_order 기록 없음")
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
