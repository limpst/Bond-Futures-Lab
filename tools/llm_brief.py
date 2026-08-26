# -*- coding: utf-8 -*-
"""LLM 마이크로 브리핑 — 로컬 ollama 로 현재 상태를 말로 풀어준다.

왜: 화면에 z-score·OBI·half-life 를 늘어놓아도 "그래서 지금 뭘 하라는 건가" 가
안 보인다. 숫자를 사람 말로 옮기는 층이 필요하다.

★ 설계 원칙 — LLM 이 숫자를 지어내지 못하게 한다
  1) 모든 수치는 이 스크립트가 DB 에서 계산해 프롬프트에 넣는다. 모델은 해석만 한다.
  2) 프롬프트에 "주어진 숫자 외에 어떤 값도 만들어내지 말 것" 을 명시한다.
  3) ollama 가 없거나 실패하면 **규칙 기반 브리핑으로 자동 대체**한다 —
     화면이 비는 것보다 낫고, 무엇으로 만들었는지 출처를 함께 남긴다.
  4) 투자 권유 문구를 만들지 않도록 지시한다. 이 프로젝트는 연구용이다.

  python tools/llm_brief.py                    KTB10-ZN 브리핑
  python tools/llm_brief.py --pair KTB3 KTB10
  python tools/llm_brief.py --model gemma4:latest
  python tools/llm_brief.py --no-llm           규칙 기반만
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.6:latest"


def gather(a, b, instr):
    """브리핑에 쓸 사실만 모은다. 여기서 계산한 것 외에는 프롬프트에 넣지 않는다."""
    import econ_pair as EP
    import microstructure as MS
    facts = {"asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pair": "%s-%s" % (a, b)}
    r = EP.analyse(a, b)
    facts["pair_stats"] = {
        "상태": r.get("status"), "봉수": r.get("n_bars"), "세션수": r.get("n_sessions"),
        "spread_현재": r.get("spread_now"), "spread_평균": r.get("spread_mean"),
        "spread_표준편차": r.get("spread_std"), "z": r.get("z_full"),
        "half_life_분": r.get("half_life_min"),
        "ECM_t_HAC10": r.get("ecm_t_hac10"), "ADF_p": r.get("adf_p"),
    }
    rows = MS.load_quotes(instr)
    if len(rows) >= 20:
        S = MS.compute(rows)
        if S:
            L = S[-1]
            facts["orderbook"] = {
                "종목": instr, "호가건수": len(rows),
                "mid": L["mid"], "OBI": L["obi"], "가중OBI": L["obiw"],
                "spread_bps": L["spr_bps"], "깊이_1pct": L["depth"],
                "VOI": L["voi"], "MLOFI": L["mlofi"],
                "유동성비용_bps_50계약": MS.liquidity_cost(rows[-1], 50),
            }
            pr = MS.predictive(S, 60)
            facts["orderbook"]["예측력_60초_상관"] = {k: (None if c is None else round(c, 3))
                                                for k, (c, n) in pr.items()}
    return facts


def rule_brief(f):
    """규칙 기반 브리핑 — LLM 없이도 같은 결론이 나와야 한다."""
    p = f.get("pair_stats", {})
    o = f.get("orderbook", {})
    L = []
    z = p.get("z")
    if z is None:
        L.append("아직 판단할 표본이 모자랍니다.")
    else:
        L.append("현재 스프레드는 평균에서 %.2f 표준편차 떨어져 있습니다." % z
                 + (" 꽤 벌어진 편입니다." if abs(z) >= 2 else " 평소 범위 안입니다."))
    hl = p.get("half_life_분")
    if hl:
        L.append("벌어진 거리의 절반이 돌아오는 데 약 %.0f분 걸리는 것으로 추정됩니다." % hl
                 + (" 하루 안에 여러 번 기회가 올 수 있는 속도입니다."
                    if hl < 120 else " 되돌림이 느려 오래 들고 있어야 합니다."))
    t = p.get("ECM_t_HAC10"); adf = p.get("ADF_p")
    if t is not None and t == t:
        if abs(t) > 1.96:
            L.append("되돌아온다는 통계적 근거(ECM t=%.2f)는 나왔습니다." % t)
        else:
            L.append("되돌아온다는 통계적 근거는 아직 부족합니다(ECM t=%.2f)." % t)
    if adf is not None:
        L.append("정상성 검정(ADF)은 p=%.3f 로 %s."
                 % (adf, "통과했습니다" if adf < 0.05 else "아직 통과하지 못했습니다"))
    if o:
        obi = o.get("OBI")
        if obi is not None:
            L.append("호가창은 %s쪽으로 %s 기울어 있습니다(OBI %+.2f)."
                     % ("매수" if obi > 0 else "매도", "크게" if abs(obi) > 0.3 else "약간", obi))
        lc = o.get("유동성비용_bps_50계약")
        if lc is not None:
            L.append("50계약을 지금 체결하면 왕복 약 %.2f bps 의 비용이 듭니다." % lc)
    L.append("표본이 이틀치 수준이라 어떤 수치도 확정으로 읽으면 안 됩니다. 연구용입니다.")
    return " ".join(L)


def llm_brief(f, model, timeout=120):
    import requests
    prompt = (
        "너는 채권 선물 스프레드 트레이딩을 감시하는 리서처다.\n"
        "아래 JSON 은 방금 계산한 실측값이다. 이 값만 근거로 한국어 브리핑을 써라.\n\n"
        "규칙:\n"
        "1. 주어진 숫자 외에 어떤 값도 만들어내지 마라. 없는 값은 '측정되지 않음' 이라고 써라.\n"
        "2. 유치원생도 이해할 수 있게 쉬운 말로 쓰되, 숫자는 그대로 인용해라.\n"
        "3. 5~7문장. 순서: 지금 상태 → 되돌림 성질이 증명됐는지 → 호가창 상태 → 주의점.\n"
        "4. 매수/매도 권유를 하지 마라. 이건 연구용이고 투자 권유가 아니다.\n"
        "5. ECM t 의 절대값이 1.96 을 넘어야 통계적으로 유의하다. ADF p 는 0.05 미만이어야 통과다.\n"
        "6. 표본이 작으면 그 사실을 반드시 언급해라.\n\n"
        "JSON:\n" + json.dumps(f, ensure_ascii=False, indent=1, default=str)
    )
    r = requests.post(OLLAMA + "/api/generate", timeout=timeout,
                      json={"model": model, "prompt": prompt, "stream": False,
                            "options": {"temperature": 0.2}})
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, default=["KTB10", "ZN"])
    ap.add_argument("--instr", default="KTB10", help="호가 지표를 볼 종목")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    f = gather(a.pair[0], a.pair[1], a.instr)
    print("=== 사실 (DB 에서 계산) ===")
    print(json.dumps(f, ensure_ascii=False, indent=1, default=str)[:1200])

    src, text = "rule", rule_brief(f)
    if not a.no_llm:
        try:
            t0 = dt.datetime.now()
            out = llm_brief(f, a.model)
            if out:
                src, text = a.model, out
                print("\n(LLM %s · %.0f초)" % (a.model, (dt.datetime.now() - t0).total_seconds()))
        except Exception as e:
            print("\n[LLM 실패 — 규칙 기반으로 대체] %s" % str(e)[:120])

    print("\n=== 브리핑 (출처: %s) ===" % src)
    print(text)
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"asof": f["asof"], "source": src, "brief": text, "facts": f},
            ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
