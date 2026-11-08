"""P5-05: golden-тест графа зависимостей и test links на мини-репозитории `tests/fixtures/code/graph/repo`.

Эталон — `tests/fixtures/code/graph/expected_edges.json` (`AICD_UPDATE_GOLDEN=1` пересоздаёт).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aicd_code_intelligence.index.builder import PHP_DECLARED_KINDS
from aicd_code_intelligence.index.graph import GraphFile, build_edges, python_module_names
from aicd_code_intelligence.index.languages import detect_language
from aicd_code_intelligence.index.policy import is_test_path
from aicd_code_intelligence.index.symbols import analyze_file

GRAPH = Path(__file__).resolve().parents[1] / "fixtures" / "code" / "graph"


def _graph_files(root: Path) -> dict[str, GraphFile]:
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    modules = python_module_names(paths)
    files: dict[str, GraphFile] = {}
    for rel in paths:
        language = detect_language(rel)
        if language is None:
            continue
        analysis = analyze_file(
            rel, language, (root / rel).read_bytes(), module_name=modules.get(rel), is_test=is_test_path(rel)
        )
        files[rel] = GraphFile(
            path=rel,
            language=language,
            is_test=is_test_path(rel),
            imports=tuple(i.target for i in analysis.imports),
            declared=tuple(
                s.qualified_name for s in analysis.symbols if s.kind in PHP_DECLARED_KINDS and s.qualified_name
            )
            if language == "php"
            else (),
        )
    return files


def test_golden_dependency_graph() -> None:
    edges = build_edges(_graph_files(GRAPH / "repo"))
    actual = [[e.edge_type, e.source_path, e.target_path, e.target_ref] for e in edges]
    expected_path = GRAPH / "expected_edges.json"
    if os.environ.get("AICD_UPDATE_GOLDEN") == "1":
        body = ",\n".join("  " + json.dumps(edge, ensure_ascii=False) for edge in actual)
        expected_path.write_text(f"[\n{body}\n]\n", encoding="utf-8")
    assert actual == json.loads(expected_path.read_text(encoding="utf-8"))


def test_python_module_names_follow_packages() -> None:
    names = python_module_names(
        [
            "packages/security/aicd_security/__init__.py",
            "packages/security/aicd_security/git.py",
            "scripts/tool.py",
            "tests/unit/test_x.py",
        ]
    )
    assert names == {
        "packages/security/aicd_security/__init__.py": "aicd_security",
        "packages/security/aicd_security/git.py": "aicd_security.git",
        "scripts/tool.py": "tool",
        "tests/unit/test_x.py": "test_x",
    }


def test_resolution_edge_cases() -> None:
    files = {
        "a/__init__.py": GraphFile("a/__init__.py", "python", False, ("..outside", ".")),
        "a/b.py": GraphFile("a/b.py", "python", False, ("a.b", "a.c.func", "os")),
        "a/c.py": GraphFile("a/c.py", "python", False, ()),
        "web/x.ts": GraphFile("web/x.ts", "typescript", False, ("../../etc/passwd", "./y", "/abs")),
        "web/y.tsx": GraphFile("web/y.tsx", "tsx", False, ()),
        "p/i.php": GraphFile("p/i.php", "php", False, ("file:/etc/passwd", "file:../../x.php", "\\Lib\\Thing")),
        "p/t.php": GraphFile("p/t.php", "php", False, (), ("Lib\\Thing",)),
    }
    edges = {(e.source_path, e.target_ref): e.target_path for e in build_edges(files)}
    assert edges[("a/__init__.py", "..outside")] is None  # выход за корень не разрешается
    assert ("a/b.py", "a.b") not in edges  # самоимпорт отброшен
    assert edges[("a/b.py", "a.c.func")] == "a/c.py"
    assert edges[("a/b.py", "os")] is None
    assert edges[("web/x.ts", "../../etc/passwd")] is None
    assert edges[("web/x.ts", "./y")] == "web/y.tsx"
    assert edges[("web/x.ts", "/abs")] is None
    assert edges[("p/i.php", "file:/etc/passwd")] is None
    assert edges[("p/i.php", "file:../../x.php")] is None
    assert edges[("p/i.php", "\\Lib\\Thing")] == "p/t.php"
