"""P4-03: корпус prompt-injection как недоверенные данные не меняет policy-блок и не выходит из ограждения.

Каждый образец `tests/fixtures/prompt_injection/*.txt` подставляется во все недоверенные поля промпта
(objective, instructions, критерии, все разделы Context Pack, атрибуты path/ref/commit). Проверяются
структурные инварианты сборки (AGENT_ROLES §3.3, ACP §8, SC-INJ-01).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from aicd_orchestration.agents.profiles import PROFILES
from aicd_orchestration.agents.prompt import (
    SECTION_PRIORITY,
    AssembledPrompt,
    TaskInput,
    UntrustedFragment,
    build_policy_block,
    build_prompt,
    new_nonce,
)

CORPUS = sorted((Path(__file__).parents[1] / "fixtures" / "prompt_injection").glob("*.txt"))
HIDDEN = re.compile("[\U000e0000-\U000e007f‪-‮⁦-⁩​-‏  ﻿\x00\x1b\r]")


def _load(path: Path, nonce: str) -> str:
    return path.read_bytes().decode("utf-8").replace("NONCE_HERE", nonce).replace("PLACEHOLDER", nonce)


def _assemble(role: str, payload: str, nonce: str) -> AssembledPrompt:
    task = TaskInput(
        task_key="main",
        mode="DEVELOP",
        complexity=3,
        risk_level="medium",
        title=payload[:500],
        objective=payload,
        instructions=payload,
        acceptance_criteria=(payload, "Обычный критерий"),
        base_commit="a" * 40,
    )
    fragments = [
        UntrustedFragment(kind=kind, content=payload, ref=f"artifact:{payload[:40]}", path=payload[:80], commit=payload[:20])
        for kind in SECTION_PRIORITY
    ]
    return build_prompt(PROFILES[role], task, fragments, nonce=nonce)


def _marker_lines(text: str, nonce: str) -> list[str]:
    n = re.escape(nonce)
    allowed = re.compile(
        rf"^<<<(BLOCK [1-5] [A-Z_]+ nonce={n}|END BLOCK [1-5] nonce={n}"
        rf"|UNTRUSTED nonce={n} seq=\d+ kind=[A-Za-z0-9._%-]+( [a-z_]+=[A-Za-z0-9._/:@+%-]+)*|END nonce={n} seq=\d+)>>>$"
    )
    lines = [line for line in text.split("\n") if "<<<" in line]
    for line in lines:
        assert allowed.match(line), f"последовательность <<< вне маркера платформы: {line[:200]!r}"
    return lines


def test_corpus_size_and_diversity() -> None:
    assert len(CORPUS) >= 20
    names = " ".join(p.stem for p in CORPUS)
    for kind in ("ignore_previous", "fence", "roleplay", "unicode", "bidi", "policy", "base64", "nonce", "self_approval"):
        assert kind in names, f"в корпусе нет образцов типа {kind}"


@pytest.mark.parametrize("sample", CORPUS, ids=lambda p: p.stem)
@pytest.mark.parametrize("role", ["developer", "reviewer"])
def test_injection_does_not_alter_policy_or_escape_fence(sample: Path, role: str) -> None:
    nonce = new_nonce()
    payload = _load(sample, nonce)
    prompt = _assemble(role, payload, nonce)
    clean = build_prompt(
        PROFILES[role],
        TaskInput(task_key="main", mode="DEVELOP", complexity=3, risk_level="medium", title="t", objective="o"),
        nonce=nonce,
    )
    text = prompt.text

    # 1. Блок политики — первый, неизменный и совпадает с эталоном без недоверенных данных.
    reference = build_policy_block(PROFILES[role], nonce)
    assert prompt.policy_block == reference == clean.policy_block
    assert text.startswith(reference + "\n\n")
    assert text.count(f"<<<BLOCK 1 SYSTEM_POLICY nonce={nonce}>>>") == 1

    # 2. Блоки 2 и 5 (инструкции роли, контракт вывода) тоже не зависят от данных.
    for no in (2, 5):
        block = re.search(rf"<<<BLOCK {no} \w+ nonce={nonce}>>>.*?<<<END BLOCK {no} nonce={nonce}>>>", text, re.S)
        ref = re.search(rf"<<<BLOCK {no} \w+ nonce={nonce}>>>.*?<<<END BLOCK {no} nonce={nonce}>>>", clean.text, re.S)
        assert block and ref and block.group(0) == ref.group(0)

    # 3. `<<<` встречается только в маркерах платформы; блоки идут в фиксированном порядке.
    markers = _marker_lines(text, nonce)
    blocks = [m for m in markers if m.startswith("<<<BLOCK")]
    assert [b.split()[1] for b in blocks] == ["1", "2", "3", "4", "5"]
    assert text.count("<<<") == len(markers)
    assert unicodedata.normalize("NFKC", text).count("<<<") == len(markers), "lookalike-скобки не экранированы"

    # 4. Каждый фрагмент закрыт своим маркером; nonce внутри данных не встречается; вложенности нет.
    opened: int | None = None
    seqs: list[int] = []
    for line in text.split("\n"):
        if line.startswith("<<<UNTRUSTED "):
            assert opened is None, "вложенное ограждение"
            opened = int(re.search(r"seq=(\d+)", line).group(1))  # type: ignore[union-attr]
            seqs.append(opened)
        elif line.startswith("<<<END nonce="):
            assert opened is not None and line == f"<<<END nonce={nonce} seq={opened}>>>"
            opened = None
        elif opened is not None:
            assert nonce not in line.lower(), "nonce в недоверенных данных"
        elif line.startswith("<<<"):
            continue
    assert opened is None and seqs == list(range(1, len(seqs) + 1))

    # 5. Скрытые символы (Unicode tags, bidi, zero-width, ESC, CR, NUL) в промпт не попадают.
    assert not HIDDEN.search(text)
    entries = prompt.manifest["fragments"]
    if "\x00" in payload:
        assert all(e.get("excluded") == "binary" for e in entries)
    else:
        assert len(seqs) == len(entries) == 4 + len(SECTION_PRIORITY)


@pytest.mark.parametrize(
    "stem",
    ["01_ignore_previous_en", "02_ignore_previous_ru", "03_closing_fence_forgery", "06_fullwidth_fence",
     "07_unicode_tags_hidden", "08_bidi_override", "20_base64_payload"],
)  # fmt: skip
def test_suspected_injection_flagged(stem: str) -> None:
    nonce = new_nonce()
    payload = _load(next(p for p in CORPUS if p.stem == stem), nonce)
    prompt = _assemble("developer", payload, nonce)
    assert prompt.manifest["suspected_injection"] is True
