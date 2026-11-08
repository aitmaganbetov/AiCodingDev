"""P5-03: golden-тесты извлечения символов для языков OQ-09 (Python, JS, TS/TSX, PHP, SQL, YAML, JSON, Markdown).

Фикстуры — `tests/fixtures/code/<язык>/`, эталон — `expected.json` рядом. Пересоздать эталоны
(после осознанного изменения алгоритма и увеличения INDEX_VERSION): `AICD_UPDATE_GOLDEN=1 pytest …`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from aicd_code_intelligence.index.chunks import build_chunks, split_identifiers
from aicd_code_intelligence.index.languages import detect_language
from aicd_code_intelligence.index.policy import IndexPolicy, SkipReason, has_generated_marker, is_binary, is_test_path
from aicd_code_intelligence.index.symbols import SYMBOL_KINDS, analyze_file, sha256_hex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "code"
LANG_DIRS = ["python", "javascript", "typescript", "php", "sql", "yaml", "json", "markdown"]


def _analyze_dir(lang_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in sorted(p for p in lang_dir.iterdir() if p.is_file() and p.name != "expected.json"):
        language = detect_language(path.name)
        assert language is not None, path
        analysis = analyze_file(path.name, language, path.read_bytes(), is_test=is_test_path(path.name))
        assert not analysis.parse_errors, path
        result[path.name] = {
            "language": language,
            "symbols": [s.to_json() for s in analysis.symbols],
            "imports": [[i.target, i.line] for i in analysis.imports],
        }
    return result


@pytest.mark.parametrize("lang", LANG_DIRS)
def test_golden_symbols(lang: str) -> None:
    lang_dir = FIXTURES / lang
    actual = _analyze_dir(lang_dir)
    expected_path = lang_dir / "expected.json"
    if os.environ.get("AICD_UPDATE_GOLDEN") == "1":
        expected_path.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert actual == expected


def test_symbols_are_deterministic_and_valid() -> None:
    for lang in LANG_DIRS:
        for path in sorted((FIXTURES / lang).iterdir()):
            if path.name == "expected.json":
                continue
            content = path.read_bytes()
            language = detect_language(path.name)
            assert language
            first = analyze_file(path.name, language, content)
            second = analyze_file(path.name, language, content)
            assert first == second
            assert first.symbols[0].kind == "module"
            assert first.symbols[0].content_hash == sha256_hex(content)
            for sym in first.symbols:
                assert sym.kind in SYMBOL_KINDS
                assert 1 <= sym.start_line <= sym.end_line
                assert sym.signature is None or len(sym.signature) <= 300
                assert len(sym.content_hash) == 64


def test_sql_ignores_comments_strings_and_bodies() -> None:
    analysis = analyze_file("s.sql", "sql", (FIXTURES / "sql" / "schema.sql").read_bytes())
    names = {s.name for s in analysis.symbols}
    assert not names & {"in_comment", "in_block_comment", "in_string", "in_body"}


def test_yaml_tags_are_not_constructed() -> None:
    # compose строит только граф узлов: тег python/object не исполняется и не мешает разбору
    analysis = analyze_file("c.yaml", "yaml", b"a: !!python/object/apply:os.system ['id']\nb: 1\n")
    assert [s.name for s in analysis.symbols[1:]] == ["a", "b"]


def test_broken_sources_do_not_raise() -> None:
    for lang, src in [
        ("python", b"def broken(:\n  class\n"),
        ("typescript", b"export class { ;;; \n"),
        ("php", b"<?php function ( {"),
        ("json", b'{"a": '),
        ("yaml", b"a: [1, 2\nb: : :"),
        ("sql", b"CREATE TABLE"),
        ("markdown", b"```\n# not heading"),
    ]:
        analysis = analyze_file(f"x.{lang}", lang, src)
        assert analysis.symbols[0].kind == "module"


def test_empty_file_has_module_symbol() -> None:
    analysis = analyze_file("pkg/__init__.py", "python", b"")
    assert [(s.kind, s.qualified_name, s.start_line, s.end_line) for s in analysis.symbols] == [("module", "pkg", 1, 1)]


def test_language_detection() -> None:
    assert detect_language("a/b.py") == "python"
    assert detect_language("x.d.ts") == "typescript"
    assert detect_language("App.TSX") == "tsx"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("x.yml") == "yaml"
    assert detect_language("LICENSE") is None


def test_policy_rules() -> None:
    policy = IndexPolicy(max_file_bytes=100, exclude_globs=("data/*.csv",))
    assert policy.skip_reason("node_modules/x/index.js", "100644", 10) is SkipReason.VENDORED
    assert policy.skip_reason("a/vendor/autoload.php", "100644", 10) is SkipReason.VENDORED
    assert policy.skip_reason("big.py", "100644", 101) is SkipReason.OVERSIZED
    assert policy.skip_reason("link", "120000", 5) is SkipReason.SYMLINK
    assert policy.skip_reason("sub", "160000", None) is SkipReason.SUBMODULE
    assert policy.skip_reason("data/x.csv", "100644", 1) is SkipReason.EXCLUDED
    assert policy.skip_reason("src/vendor.py", "100644", 1) is None  # имя файла, не каталог
    assert policy.is_generated_name("web/app.min.js")
    assert policy.is_generated_name("uv.lock")
    assert not policy.is_generated_name("app.js")
    assert has_generated_marker(b"// Code generated by protoc. DO NOT EDIT.\n")
    assert has_generated_marker(b"# @generated\n")
    assert not has_generated_marker(b"print('hello')\n")
    assert is_binary(b"\x89PNG\x00\x00")
    assert is_binary(b"\xff\xfe broken utf8")
    assert is_binary(b"text" * 5000 + b"\x00")
    assert not is_binary("привет".encode())
    assert is_test_path("tests/unit/helpers.py")
    assert is_test_path("src/foo.test.ts")
    assert is_test_path("app/UserTest.php")
    assert is_test_path("pkg/test_x.py")
    assert not is_test_path("src/testing_utils.py")


def test_chunks_reference_git_lines() -> None:
    content = "".join(f"line {i}\n" for i in range(1, 251)).encode()
    analysis = analyze_file("big.py", "python", content)
    chunks = build_chunks(
        path="big.py",
        language="python",
        content=content,
        symbols=analysis.symbols,
        chunk_lines=120,
        max_symbol_chunk_lines=400,
    )
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 120), (121, 240), (241, 250)]
    assert chunks[0].text_ref("a" * 40) == f"git:{'a' * 40}#L1-L120"
    assert all(c.source == "code_file" for c in chunks)
    py = b"class UserAccount:\n    def getFullName(self):\n        return 1\n"
    analysis = analyze_file("m.py", "python", py)
    chunks = build_chunks(
        path="m.py", language="python", content=py, symbols=analysis.symbols, chunk_lines=120, max_symbol_chunk_lines=2
    )
    method = [c for c in chunks if c.metadata.get("symbol") == "getFullName"]
    assert [(c.start_line, c.end_line, c.chunk_index) for c in method] == [(2, 3, 0)]
    cls = [c for c in chunks if c.metadata.get("symbol") == "UserAccount"]
    assert [(c.start_line, c.end_line, c.chunk_index) for c in cls] == [(1, 2, 0), (3, 3, 1)]
    assert "full" in method[0].fts_text.split()


def test_split_identifiers() -> None:
    assert split_identifiers("getUserName HTTPServer snake_case_name plain") == "get user name http server"
