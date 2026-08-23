# -*- coding: utf-8 -*-
"""LS증권 Open API 공통 계층 — 토큰 발급·캐시 + TR 요청 헬퍼.

★ 조회(시세·마스터) 전용이다. 주문/체결 API 는 이 모듈에 만들지 않는다
  (실거래 트랙 규율: 자동 매매 실행은 범위 밖 — 2026-08-23 확정).

자격증명은 저장소 루트의 `.env.ls` (gitignore 대상) 에서 읽는다:
  LS_KR_FUTOPT_APPKEY / LS_KR_FUTOPT_APPSECRET   국내 선물옵션 (KTB 선물)
  LS_OS_FUTOPT_APPKEY / LS_OS_FUTOPT_APPSECRET   해외 선물옵션 (CME 채권 선물)
  LS_GENERAL_APPKEY   / LS_GENERAL_APPSECRET     종합매매
값은 어떤 로그·출력에도 찍지 않는다.

토큰: OAuth2 client_credentials — POST {BASE}/oauth2/token.
  만료(expires_in) 전까지 tools/logs/.ls_token_<channel>.json 에 캐시하고
  재사용한다. 캐시 파일도 .gitignore(tools/logs) 아래라 커밋되지 않는다.

요청: POST {BASE}{path}, 헤더 tr_cd/tr_cont — LS OpenAPI 표준. 초당 1건
  스로틀(보수적)로 레이트리밋을 지킨다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.ls"
TOKEN_DIR = ROOT / "tools" / "logs"
BASE = "https://openapi.ls-sec.co.kr:8080"

CHANNELS = {
    "kr_futopt": ("LS_KR_FUTOPT_APPKEY", "LS_KR_FUTOPT_APPSECRET"),
    "os_futopt": ("LS_OS_FUTOPT_APPKEY", "LS_OS_FUTOPT_APPSECRET"),
    "general":   ("LS_GENERAL_APPKEY",   "LS_GENERAL_APPSECRET"),
}

_MIN_INTERVAL = 1.05          # 초당 1건 (보수적)
_last_call = 0.0


def load_env() -> dict[str, str]:
    """`.env.ls` 를 읽는다. os.environ 값이 있으면 그것이 우선."""
    import os
    out: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    for k in {k for pair in CHANNELS.values() for k in pair}:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def _cache_path(channel: str) -> Path:
    return TOKEN_DIR / f".ls_token_{channel}.json"


def issue_token(channel: str, env: dict[str, str] | None = None,
                force: bool = False) -> str:
    """채널별 access token — 캐시가 유효하면 재사용, 아니면 새로 발급."""
    env = env or load_env()
    kkey, ksec = CHANNELS[channel]
    if not (env.get(kkey) and env.get(ksec)):
        raise RuntimeError(f"{channel}: .env.ls 에 {kkey}/{ksec} 가 없습니다")

    cache = _cache_path(channel)
    if not force and cache.is_file():
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("expires_at", 0) - time.time() > 120:      # 2분 여유
                return c["access_token"]
        except (ValueError, KeyError):
            pass

    r = requests.post(
        f"{BASE}/oauth2/token",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "appkey": env[kkey], "appsecretkey": env[ksec],
              "scope": "oob"},
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"{channel}: 토큰 응답에 access_token 없음 "
                           f"(keys={sorted(j.keys())})")
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "access_token": j["access_token"],
        "expires_at": time.time() + float(j.get("expires_in", 0)),
    }), encoding="utf-8")
    return j["access_token"]


def call_tr(channel: str, path: str, tr_cd: str, body: dict,
            tr_cont: str = "N", tr_cont_key: str = "") -> dict:
    """TR 조회 호출 (초당 1건 스로틀). 실패 시 본문 일부를 담아 예외."""
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    tok = issue_token(channel)
    r = requests.post(
        f"{BASE}{path}",
        headers={"content-type": "application/json; charset=UTF-8",
                 "authorization": f"Bearer {tok}",
                 "tr_cd": tr_cd, "tr_cont": tr_cont,
                 "tr_cont_key": tr_cont_key},
        json=body, timeout=30,
    )
    _last_call = time.time()
    if r.status_code != 200:
        raise RuntimeError(f"{tr_cd} HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if str(j.get("rsp_cd", "00000")) not in ("00000", "0"):
        raise RuntimeError(f"{tr_cd} rsp_cd={j.get('rsp_cd')} "
                           f"msg={j.get('rsp_msg', '')[:200]}")
    return j
