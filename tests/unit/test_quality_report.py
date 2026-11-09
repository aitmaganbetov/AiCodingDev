"""P6-11: чистые правила API качества — вердикт run, content negotiation отчёта, экранирование Markdown."""

from __future__ import annotations

import pytest

from aicd_api.report_markdown import md
from aicd_api.routers.quality import _wants_markdown, aggregate_verdict


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        ([], "PENDING"),
        (["PASS", "NOT_APPLICABLE"], "PASS"),
        (["PASS", "SKIPPED"], "FAIL"),  # SKIPPED обязательного gate ≠ PASS
        (["PASS", "ERROR"], "ERROR"),
        (["ERROR", "FAIL"], "FAIL"),
        (["PASS", "RUNNING"], "RUNNING"),
        (["PASS", "PENDING"], "PENDING"),
        (["NOT_APPLICABLE"], "FAIL"),  # пустая верификация
    ],
)
def test_aggregate_verdict(statuses: list[str], verdict: str) -> None:
    assert aggregate_verdict(statuses) == verdict


@pytest.mark.parametrize(
    ("accept", "markdown"),
    [
        (None, False),
        ("*/*", False),
        ("application/json", False),
        ("text/markdown", True),
        ("text/markdown, application/json;q=0.9", True),
        ("application/json, text/markdown;q=0.5", False),
        ("text/markdown;q=0", False),
        ("text/*", True),
        ("text/markdown;q=abc", False),
    ],
)
def test_report_content_negotiation(accept: str | None, markdown: bool) -> None:
    assert _wants_markdown(accept) is markdown


def test_markdown_cell_is_redacted_and_escaped() -> None:
    cell = md("a | b\n<img src=x onerror=1> [l](javascript:x) `c` *d* token=abcdef123456")
    assert "|" not in cell.replace("\\|", "")
    assert "<" not in cell and "&lt;img" in cell
    assert "\\[l\\](javascript:x)" in cell and "\\`c\\`" in cell and "\\*d\\*" in cell
    assert "abcdef123456" not in cell and "\n" not in cell
    assert md(None) == "—" and md(True) == "yes"
