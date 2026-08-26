# -*- coding: utf-8 -*-
"""docs/CHANGELOG.md 생성 — git 이력에서 변경사항을 자동으로 뽑는다.

왜 손으로 안 쓰나: 손으로 쓰는 변경 이력은 바쁠 때 가장 먼저 빠진다. git 이
이미 알고 있는 것(언제·무엇을·몇 줄)을 다시 타이핑할 이유가 없다. 사람이
보태야 하는 것은 **왜 그렇게 했나** 뿐인데, 그건 커밋 메시지 본문에 이미 쓴다.

날짜별로 묶고, 각 커밋의 제목 + 변경 파일 수 + 주요 경로를 남긴다.

  python tools/gen_changelog.py            docs/CHANGELOG.md 재생성
  python tools/gen_changelog.py --days 30  최근 N일만
"""
from __future__ import annotations

import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import subprocess
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = "\x1e"          # record separator
FSEP = "\x1f"         # field separator

TYPE_KO = {
    "feat": "기능", "fix": "수정", "docs": "문서", "refactor": "정리",
    "analysis": "분석", "ops": "운영", "chore": "잡무", "ui": "화면",
    "init": "시작", "verify": "검증", "finding": "발견", "sync": "동기화",
    "test": "시험", "perf": "성능",
}


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def collect(days: int | None):
    fmt = SEP + FSEP.join(["%h", "%ad", "%s", "%b"])
    args = ["log", f"--pretty={fmt}", "--date=format:%Y-%m-%d %H:%M", "--numstat"]
    if days:
        args.append(f"--since={days} days ago")
    raw = git(*args)
    out = []
    for rec in raw.split(SEP):
        rec = rec.strip("\n")
        if not rec:
            continue
        head, _, stat = rec.partition("\n")
        parts = head.split(FSEP)
        if len(parts) < 3:
            continue
        h, date, subj = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        files, adds, dels = 0, 0, 0
        paths = []
        for line in stat.splitlines():
            bits = line.split("\t")
            if len(bits) == 3:
                files += 1
                try:
                    adds += int(bits[0]); dels += int(bits[1])
                except ValueError:
                    pass
                paths.append(bits[2])
        out.append({"hash": h, "date": date, "subj": subj, "body": body.strip(),
                    "files": files, "adds": adds, "dels": dels, "paths": paths})
    return out


def top_areas(paths, k=3):
    """어느 영역을 건드렸나 — tools/ · reports/ · frontend/ 같은 상위 묶음."""
    seen = OrderedDict()
    for p in paths:
        area = p.split("/")[0] if "/" in p else p
        seen[area] = seen.get(area, 0) + 1
    return ", ".join(f"{a}({n})" for a, n in list(seen.items())[:k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()

    commits = collect(a.days)
    by_day = OrderedDict()
    for c in commits:
        by_day.setdefault(c["date"][:10], []).append(c)

    n_files = sum(c["files"] for c in commits)
    lines = [
        "# 📜 변경 이력 (CHANGELOG)",
        "",
        "> `python tools/gen_changelog.py` 로 **git 이력에서 자동 생성**합니다 — 손으로 고치지 마세요.",
        f"> 커밋 {len(commits)}건 · 변경 파일 {n_files}건 · 최신 {commits[0]['date'] if commits else '—'}",
        "",
        "각 줄의 뜻: `유형 · 제목` / `해시 · 시각 · 파일수 (+추가/−삭제)` / 건드린 영역.",
        "**왜 그렇게 했나**는 커밋 본문에 있습니다 — `git show <해시>` 로 보세요.",
        "",
    ]
    for day, cs in by_day.items():
        lines.append(f"## {day}")
        lines.append("")
        for c in cs:
            typ = c["subj"].split(":")[0].split("(")[0].strip()
            ko = TYPE_KO.get(typ, typ)
            subj = c["subj"]
            lines.append(f"- **{ko}** · {subj}")
            lines.append(f"  - `{c['hash']}` · {c['date'][11:]} · "
                         f"{c['files']}개 파일 (+{c['adds']}/−{c['dels']}) · {top_areas(c['paths'])}")
            first = next((l for l in c["body"].splitlines() if l.strip()), "")
            if first and not first.startswith("Co-Authored"):
                lines.append(f"  - {first.strip()[:160]}")
        lines.append("")

    out = ROOT / "docs" / "CHANGELOG.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[changelog] {out} · 커밋 {len(commits)}건 · {len(by_day)}일치")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
