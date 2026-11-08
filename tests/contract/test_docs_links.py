"""P1-17: относительные ссылки в документации ведут на существующие файлы."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_relative_markdown_links_resolve() -> None:
    broken = []
    files = [*ROOT.joinpath("docs").rglob("*.md"), ROOT / "README.md", ROOT / "apps" / "web" / "README.md"]
    for md in files:
        text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"`[^`\n]*`", "", text)  # inline-код (regex, SQL) — не ссылки
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if path and not (md.parent / path).exists():
                broken.append(f"{md.relative_to(ROOT)} → {target}")
    assert broken == []
