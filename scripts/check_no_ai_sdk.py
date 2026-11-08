#!/usr/bin/env python3
"""Deny-gate: в зависимостях нет SDK AI-провайдеров (ТЗ §1, ADR-0003, NFR-SEC-01).

Проверяет lock-файлы Python (uv.lock) и JS (pnpm-lock.yaml), включая транзитивные зависимости.
Код возврата 1 и список нарушений, если найдено. Импорты дополнительно запрещены import-linter.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PY = re.compile(
    r"^(openai|anthropic|google-generativeai|google-genai|google-cloud-aiplatform|vertexai|mistralai|cohere|"
    r"ollama|litellm|groq|together|replicate|huggingface-hub|langchain(-.*)?|llama-index(-.*)?|boto3-bedrock)$"
)
FORBIDDEN_JS = re.compile(
    r"^(openai|@anthropic-ai/.*|@google/generative-ai|@google/genai|@mistralai/.*|cohere-ai|ollama|"
    r"@langchain/.*|langchain|ai|@ai-sdk/.*|groq-sdk|replicate)$"
)


def python_packages(lock: str) -> set[str]:
    return set(re.findall(r'^\[\[package\]\]\nname = "([^"]+)"', lock, re.M))


def js_packages(lock: str) -> set[str]:
    names = set()
    for key in re.findall(r"^  '?(@?[^@\s:'][^\s:']*?)@\d", lock, re.M):
        names.add(key)
    return names


def violations(py_lock: str, js_lock: str) -> list[str]:
    found = [f"python: {n}" for n in sorted(python_packages(py_lock)) if FORBIDDEN_PY.match(n.lower())]
    found += [f"js: {n}" for n in sorted(js_packages(js_lock)) if FORBIDDEN_JS.match(n.lower())]
    return found


def main() -> int:
    py = (ROOT / "uv.lock").read_text(encoding="utf-8")
    js_path = ROOT / "apps" / "web" / "pnpm-lock.yaml"
    js = js_path.read_text(encoding="utf-8") if js_path.exists() else ""
    if not python_packages(py):
        print("uv.lock не разобран — проверка невозможна", file=sys.stderr)
        return 2
    bad = violations(py, js)
    if bad:
        print("Запрещённые SDK AI-провайдеров (ТЗ §1, ADR-0003):", *bad, sep="\n  ", file=sys.stderr)
        return 1
    print(f"OK: {len(python_packages(py))} python- и {len(js_packages(js))} js-пакетов без SDK AI-провайдеров")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
