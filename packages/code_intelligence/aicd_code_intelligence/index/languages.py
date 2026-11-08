"""Определение языка файла по имени (детерминированно, без чтения содержимого).

Языки с извлечением символов — OQ-09: python, javascript, typescript, tsx, php, sql, yaml, json, markdown.
Остальные известные расширения только помечаются языком (Repository Explorer, статистика стека).
Значение совместимо с CHECK `code_files.language ~ '^[a-z0-9_+#-]{1,32}$'`.
"""

from __future__ import annotations

from pathlib import PurePosixPath

SYMBOL_LANGUAGES: frozenset[str] = frozenset(
    {"python", "javascript", "typescript", "tsx", "php", "sql", "yaml", "json", "markdown"}
)
TREE_SITTER_LANGUAGES: frozenset[str] = frozenset({"python", "javascript", "typescript", "tsx", "php"})

_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    # Только метка языка (без символов в v1.0, OQ-09: Go/Java — P2).
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c#",
    ".sh": "shell",
    ".bash": "shell",
    ".css": "css",
    ".scss": "scss",
    ".html": "html",
    ".htm": "html",
    ".vue": "vue",
    ".svelte": "svelte",
    ".toml": "toml",
    ".ini": "ini",
    ".xml": "xml",
    ".twig": "twig",
    ".txt": "text",
    ".rst": "rst",
}

_BY_NAME: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "containerfile": "dockerfile",
}


def detect_language(path: str) -> str | None:
    p = PurePosixPath(path)
    name = p.name.lower()
    if name in _BY_NAME:
        return _BY_NAME[name]
    if name.startswith("dockerfile."):
        return "dockerfile"
    if name.endswith(".d.ts"):
        return "typescript"
    return _BY_EXTENSION.get(p.suffix.lower())
