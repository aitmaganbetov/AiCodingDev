"""Извлечение символов и импортов (P5-03, P5-05; OQ-09).

* Python, JavaScript, TypeScript/TSX, PHP — обход синтаксического дерева tree-sitter;
* SQL — собственный токенизатор (комментарии, строки, `$tag$`-тела пропускаются), имена `CREATE …`;
* Markdown — заголовки (ATX/Setext, вне fenced-блоков и front matter);
* YAML — ключи верхнего уровня через `yaml.compose` (только граф узлов, без конструирования объектов);
* JSON — ключи верхнего уровня собственным сканером (после проверки `json.loads`).

Результат детерминирован: символы в порядке исходного текста, первый — символ `module` файла
(конец рёбер графа зависимостей). Строки 1-based, включительно. `content_hash` — sha256 байтов символа.
Код из репозитория не исполняется.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from typing import Any

import yaml
from tree_sitter import Language, Node, Parser

from aicd_code_intelligence.index.languages import TREE_SITTER_LANGUAGES

SIGNATURE_MAX = 300
DOC_MAX = 300
NAME_MAX = 1024
QUALIFIED_MAX = 4096
MAX_SYMBOLS_PER_FILE = 5000

SYMBOL_KINDS: frozenset[str] = frozenset(
    {"module", "class", "interface", "function", "method", "variable", "constant", "type", "enum", "test", "route", "other"}
)

_UPPER_CONST = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ExtractedSymbol:
    name: str
    qualified_name: str | None
    kind: str
    start_line: int
    end_line: int
    signature: str | None
    doc: str | None
    content_hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "doc": self.doc,
        }


@dataclass(frozen=True)
class ImportRef:
    """Сырая ссылка импорта; разрешается в файл модулем `graph` (формат зависит от языка)."""

    target: str
    line: int


@dataclass(frozen=True)
class FileAnalysis:
    language: str
    symbols: tuple[ExtractedSymbol, ...]
    imports: tuple[ImportRef, ...]
    parse_errors: bool = False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_count(content: bytes) -> int:
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def default_module_name(path: str, language: str) -> str:
    """Имя модуля по пути: Python — точечное (`pkg/mod.py` → `pkg.mod`), остальные — путь."""
    if language == "python":
        p = PurePosixPath(path)
        parts = [*p.parent.parts, p.stem] if p.stem != "__init__" else list(p.parent.parts)
        return ".".join(parts) or p.stem
    return path


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


class _Builder:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.symbols: list[ExtractedSymbol] = []
        self.imports: list[ImportRef] = []

    def add(
        self,
        *,
        name: str,
        qualified: str | None,
        kind: str,
        start_line: int,
        end_line: int,
        start_byte: int,
        end_byte: int,
        signature: str | None = None,
        doc: str | None = None,
    ) -> None:
        if len(self.symbols) >= MAX_SYMBOLS_PER_FILE or not name:
            return
        self.symbols.append(
            ExtractedSymbol(
                name=_truncate(name, NAME_MAX),
                qualified_name=_truncate(qualified, QUALIFIED_MAX) if qualified else None,
                kind=kind,
                start_line=start_line,
                end_line=max(end_line, start_line),
                signature=_truncate(_collapse(signature), SIGNATURE_MAX) if signature else None,
                doc=_truncate(_collapse(doc), DOC_MAX) if doc else None,
                content_hash=sha256_hex(self.content[start_byte:end_byte]),
            )
        )

    def add_import(self, target: str, line: int) -> None:
        target = target.strip()
        if target and len(target) <= 1024 and "\x00" not in target:
            self.imports.append(ImportRef(target=target, line=line))


# ---------------------------------------------------------------------------------------------
# tree-sitter
# ---------------------------------------------------------------------------------------------


@cache
def _language(language: str) -> Language:
    if language == "python":
        import tree_sitter_python as mod

        return Language(mod.language())
    if language == "javascript":
        import tree_sitter_javascript as js

        return Language(js.language())
    if language in ("typescript", "tsx"):
        import tree_sitter_typescript as ts

        return Language(ts.language_tsx() if language == "tsx" else ts.language_typescript())
    if language == "php":
        import tree_sitter_php as php

        return Language(php.language_php())
    raise ValueError(f"нет грамматики tree-sitter для {language}")


def _text(content: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return content[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _signature(content: bytes, node: Node, body: Node | None) -> str:
    end = body.start_byte if body is not None else node.end_byte
    raw = content[node.start_byte : end].decode("utf-8", "replace")
    if body is None:
        raw = raw.split("\n", 1)[0]
    return raw.rstrip().rstrip(":{").rstrip()


def _block_comment_doc(content: bytes, node: Node) -> str | None:
    prev = node.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    if prev.end_point[0] + 1 < node.start_point[0]:
        return None  # комментарий отделён пустой строкой
    raw = _text(content, prev)
    if not raw.startswith("/**"):
        return None
    lines = []
    for line in raw.removeprefix("/**").removesuffix("*/").splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("@"):
            break
        lines.append(line)
    text = " ".join(x for x in lines if x)
    return text or None


def _string_value(content: bytes, node: Node) -> str | None:
    """Значение строкового литерала без интерполяции (JS `string`/шаблон без подстановок, PHP строки)."""
    if node.type in ("string", "encapsed_string", "template_string"):
        parts = []
        for child in node.named_children:
            if child.type in ("string_fragment", "string_content", "string_value"):
                parts.append(_text(content, child))
            elif child.type == "escape_sequence":
                return None
            else:
                return None  # интерполяция — значение не статично
        return "".join(parts)
    return None


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _end_line(node: Node) -> int:
    row, col = node.end_point
    return row + 1 if col > 0 or row == node.start_point[0] else row


# --- Python ----------------------------------------------------------------------------------


def _py_docstring(content: bytes, body: Node | None) -> str | None:
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement" or not first.named_children:
        return None
    string = first.named_children[0]
    if string.type != "string":
        return None
    parts = [_text(content, c) for c in string.named_children if c.type == "string_content"]
    text = "".join(parts).strip()
    return text.split("\n\n", 1)[0] if text else None


def _python(content: bytes, root: Node, module: str, is_test: bool, b: _Builder) -> None:
    def walk(node: Node, scope: list[str], in_class: bool) -> None:
        for child in node.named_children:
            outer = child
            target = child
            if child.type == "decorated_definition":
                definition = child.child_by_field_name("definition")
                if definition is None:
                    continue
                target = definition
            if target.type in ("class_definition", "function_definition"):
                name = _text(content, target.child_by_field_name("name"))
                body = target.child_by_field_name("body")
                if target.type == "class_definition":
                    kind = "class"
                elif is_test and name.startswith("test"):
                    kind = "test"
                else:
                    kind = "method" if in_class else "function"
                b.add(
                    name=name,
                    qualified=".".join([module, *scope, name]),
                    kind=kind,
                    start_line=_line(outer),
                    end_line=_end_line(outer),
                    start_byte=outer.start_byte,
                    end_byte=outer.end_byte,
                    signature=_signature(content, target, body),
                    doc=_py_docstring(content, body),
                )
                if target.type == "class_definition" and body is not None:
                    walk(body, [*scope, name], True)
            elif child.type == "expression_statement" and child.named_children:
                assign = child.named_children[0]
                if assign.type != "assignment":
                    continue
                left = assign.child_by_field_name("left")
                if left is None or left.type != "identifier":
                    continue
                name = _text(content, left)
                if _UPPER_CONST.match(name):
                    b.add(
                        name=name,
                        qualified=".".join([module, *scope, name]),
                        kind="constant",
                        start_line=_line(child),
                        end_line=_end_line(child),
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_text(content, child).split("\n", 1)[0],
                    )

    walk(root, [], False)
    for node in _iter_nodes(root, {"import_statement", "import_from_statement"}):
        if node.type == "import_statement":
            for name_node in node.children_by_field_name("name"):
                dotted = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                b.add_import(_text(content, dotted), _line(node))
            continue
        module_node = node.child_by_field_name("module_name")
        base = _text(content, module_node).replace(" ", "")
        names = [
            n.child_by_field_name("name") if n.type == "aliased_import" else n
            for n in node.children_by_field_name("name")
        ]
        if not names:  # `from x import *`
            b.add_import(base, _line(node))
            continue
        for n in names:
            sub = _text(content, n)
            b.add_import(f"{base}{sub}" if base.endswith(".") else f"{base}.{sub}", _line(node))


def _iter_nodes(root: Node, types: set[str]) -> Iterator[Node]:
    cursor = root.walk()
    visited_children = False
    while True:
        node = cursor.node
        if not visited_children:
            if node is not None and node.type in types:
                yield node
            if cursor.goto_first_child():
                continue
        if cursor.goto_next_sibling():
            visited_children = False
            continue
        if not cursor.goto_parent():
            return
        visited_children = True


# --- JavaScript / TypeScript -----------------------------------------------------------------

_JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_JS_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_JS_TEST_CALLS = frozenset({"describe", "it", "test", "suite"})


def _javascript(content: bytes, root: Node, path: str, is_test: bool, b: _Builder) -> None:
    def qn(scope: list[str], name: str) -> str:
        return f"{path}#{'.'.join([*scope, name])}"

    def add_decl(node: Node, outer: Node, scope: list[str], exported: bool) -> None:
        t = node.type
        doc = _block_comment_doc(content, outer)
        if t in ("function_declaration", "generator_function_declaration", "function_signature"):
            name = _text(content, node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            b.add(
                name=name,
                qualified=qn(scope, name),
                kind="function",
                start_line=_line(outer),
                end_line=_end_line(outer),
                start_byte=outer.start_byte,
                end_byte=outer.end_byte,
                signature=_signature(content, node, body),
                doc=doc,
            )
        elif t in _JS_CLASSES:
            name_node = node.child_by_field_name("name")
            name = _text(content, name_node) if name_node is not None else "default"
            body = node.child_by_field_name("body")
            b.add(
                name=name,
                qualified=qn(scope, name),
                kind="class",
                start_line=_line(outer),
                end_line=_end_line(outer),
                start_byte=outer.start_byte,
                end_byte=outer.end_byte,
                signature=_signature(content, node, body),
                doc=doc,
            )
            if body is not None:
                for member in body.named_children:
                    if member.type in ("method_definition", "abstract_method_signature", "method_signature"):
                        mname = _text(content, member.child_by_field_name("name"))
                        mbody = member.child_by_field_name("body")
                        b.add(
                            name=mname,
                            qualified=qn([*scope, name], mname),
                            kind="method",
                            start_line=_line(member),
                            end_line=_end_line(member),
                            start_byte=member.start_byte,
                            end_byte=member.end_byte,
                            signature=_signature(content, member, mbody),
                            doc=_block_comment_doc(content, member),
                        )
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            name = _text(content, node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            kind = {"interface_declaration": "interface", "type_alias_declaration": "type"}.get(t, "enum")
            b.add(
                name=name,
                qualified=qn(scope, name),
                kind=kind,
                start_line=_line(outer),
                end_line=_end_line(outer),
                start_byte=outer.start_byte,
                end_byte=outer.end_byte,
                signature=_signature(content, node, body if kind != "type" else None),
                doc=doc,
            )
        elif t in ("lexical_declaration", "variable_declaration"):
            is_const = t == "lexical_declaration" and _text(content, node).startswith("const")
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None or name_node.type != "identifier":
                    continue
                name = _text(content, name_node)
                value = declarator.child_by_field_name("value")
                if value is not None and value.type in _JS_FUNCTION_VALUES:
                    kind = "function"
                    signature = _signature(content, node, value.child_by_field_name("body"))
                elif is_const and _UPPER_CONST.match(name):
                    kind, signature = "constant", _text(content, node).split("\n", 1)[0]
                elif exported:
                    kind = "constant" if is_const else "variable"
                    signature = _text(content, node).split("\n", 1)[0]
                else:
                    continue
                b.add(
                    name=name,
                    qualified=qn(scope, name),
                    kind=kind,
                    start_line=_line(outer),
                    end_line=_end_line(outer),
                    start_byte=outer.start_byte,
                    end_byte=outer.end_byte,
                    signature=signature,
                    doc=doc,
                )

    def test_call(node: Node, scope: list[str]) -> None:
        if node.type != "expression_statement" or not node.named_children:
            return
        call = node.named_children[0]
        if call.type != "call_expression":
            return
        fn = call.child_by_field_name("function")
        if fn is None:
            return
        callee = _text(content, fn)
        if callee.split(".", 1)[0] not in _JS_TEST_CALLS:
            return
        args = call.child_by_field_name("arguments")
        if args is None or not args.named_children:
            return
        title = _string_value(content, args.named_children[0])
        if title is None:
            return
        b.add(
            name=title,
            qualified=f"{path}#{' > '.join([*scope, title])}",
            kind="test",
            start_line=_line(node),
            end_line=_end_line(node),
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            signature=callee,
        )
        if callee.split(".", 1)[0] in ("describe", "suite"):
            for cb in args.named_children[1:]:
                if cb.type in _JS_FUNCTION_VALUES:
                    cb_body = cb.child_by_field_name("body")
                    if cb_body is not None:
                        for stmt in cb_body.named_children:
                            test_call(stmt, [*scope, title])

    for child in root.named_children:
        if child.type == "export_statement":
            decl = child.child_by_field_name("declaration")
            if decl is None:
                decl = next((c for c in child.named_children if c.type in _JS_CLASSES or c.type in _JS_FUNCTION_VALUES), None)
                if decl is not None and decl.type in _JS_FUNCTION_VALUES:
                    b.add(
                        name="default",
                        qualified=qn([], "default"),
                        kind="function",
                        start_line=_line(child),
                        end_line=_end_line(child),
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        signature=_signature(content, child, decl.child_by_field_name("body")),
                        doc=_block_comment_doc(content, child),
                    )
                    continue
            if decl is not None:
                add_decl(decl, child, [], True)
        elif is_test and child.type == "expression_statement":
            test_call(child, [])
        else:
            add_decl(child, child, [], False)

    for node in _iter_nodes(root, {"import_statement", "export_statement", "call_expression"}):
        if node.type in ("import_statement", "export_statement"):
            source = node.child_by_field_name("source")
            if source is not None:
                value = _string_value(content, source)
                if value is not None:
                    b.add_import(value, _line(node))
            continue
        fn = node.child_by_field_name("function")
        if fn is None or not (fn.type == "import" or (fn.type == "identifier" and _text(content, fn) == "require")):
            continue
        args = node.child_by_field_name("arguments")
        if args is not None and len(args.named_children) == 1:
            value = _string_value(content, args.named_children[0])
            if value is not None:
                b.add_import(value, _line(node))


# --- PHP -------------------------------------------------------------------------------------

_PHP_INCLUDES = frozenset({"require_expression", "require_once_expression", "include_expression", "include_once_expression"})


def _php(content: bytes, root: Node, path: str, is_test: bool, b: _Builder) -> None:
    def ns_join(ns: str, name: str) -> str:
        return f"{ns}\\{name}" if ns else name

    def members(body: Node, owner: str) -> None:
        for member in body.named_children:
            if member.type == "method_declaration":
                name = _text(content, member.child_by_field_name("name"))
                mbody = member.child_by_field_name("body")
                kind = "test" if is_test and name.startswith("test") else "method"
                b.add(
                    name=name,
                    qualified=f"{owner}::{name}",
                    kind=kind,
                    start_line=_line(member),
                    end_line=_end_line(member),
                    start_byte=member.start_byte,
                    end_byte=member.end_byte,
                    signature=_signature(content, member, mbody),
                    doc=_block_comment_doc(content, member),
                )
            elif member.type == "const_declaration":
                consts(member, owner, "::")

    def consts(decl: Node, owner: str, sep: str) -> None:
        for element in decl.named_children:
            if element.type != "const_element":
                continue
            name_node = next((c for c in element.named_children if c.type == "name"), None)
            if name_node is None:
                continue
            name = _text(content, name_node)
            b.add(
                name=name,
                qualified=f"{owner}{sep}{name}" if owner else name,
                kind="constant",
                start_line=_line(decl),
                end_line=_end_line(decl),
                start_byte=decl.start_byte,
                end_byte=decl.end_byte,
                signature=_text(content, decl).split("\n", 1)[0],
            )

    def walk(node: Node, ns: str) -> str:
        for child in node.named_children:
            t = child.type
            if t == "namespace_definition":
                name = _text(content, child.child_by_field_name("name"))
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, name)
                else:
                    ns = name
            elif t == "namespace_use_declaration":
                group = child.child_by_field_name("body")
                prefix_node = next((c for c in child.named_children if c.type == "namespace_name"), None)
                clauses = group.named_children if group is not None else child.named_children
                prefix = _text(content, prefix_node) if group is not None and prefix_node is not None else ""
                for clause in clauses:
                    if clause.type != "namespace_use_clause":
                        continue
                    target = next(
                        (c for c in clause.named_children if c.type in ("qualified_name", "name", "namespace_name")),
                        None,
                    )
                    if target is not None:
                        full = ns_join(prefix, _text(content, target)) if prefix else _text(content, target)
                        b.add_import(full.lstrip("\\"), _line(child))
            elif t == "function_definition":
                name = _text(content, child.child_by_field_name("name"))
                b.add(
                    name=name,
                    qualified=ns_join(ns, name),
                    kind="function",
                    start_line=_line(child),
                    end_line=_end_line(child),
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    signature=_signature(content, child, child.child_by_field_name("body")),
                    doc=_block_comment_doc(content, child),
                )
            elif t in ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"):
                name = _text(content, child.child_by_field_name("name"))
                body = child.child_by_field_name("body")
                kind = {"interface_declaration": "interface", "enum_declaration": "enum"}.get(t, "class")
                owner = ns_join(ns, name)
                b.add(
                    name=name,
                    qualified=owner,
                    kind=kind,
                    start_line=_line(child),
                    end_line=_end_line(child),
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    signature=_signature(content, child, body),
                    doc=_block_comment_doc(content, child),
                )
                if body is not None:
                    members(body, owner)
            elif t == "const_declaration":
                consts(child, ns, "\\")
        return ns

    walk(root, "")
    for node in _iter_nodes(root, set(_PHP_INCLUDES)):
        arg = node.named_children[0] if node.named_children else None
        if arg is None:
            continue
        if arg.type == "parenthesized_expression" and arg.named_children:
            arg = arg.named_children[0]
        value = _string_value(content, arg)
        if value is not None:
            b.add_import(f"file:{value}", _line(node))
        elif arg.type == "binary_expression":
            left, right = arg.child_by_field_name("left"), arg.child_by_field_name("right")
            if left is not None and right is not None and _text(content, left) == "__DIR__":
                tail = _string_value(content, right)
                if tail is not None and tail.startswith("/"):
                    b.add_import(f"file:.{tail}", _line(node))


def _tree_sitter(language: str, content: bytes, path: str, module: str, is_test: bool, b: _Builder) -> bool:
    parser = Parser(_language(language))
    tree = parser.parse(content)
    root = tree.root_node
    if language == "python":
        _python(content, root, module, is_test, b)
    elif language == "php":
        _php(content, root, path, is_test, b)
    else:
        _javascript(content, root, path, is_test, b)
    return bool(root.has_error)


# ---------------------------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------------------------


@dataclass
class _SqlToken:
    kind: str  # word | quoted | punct
    text: str
    line: int
    start: int
    end: int


def _sql_tokens(text: str) -> Iterator[_SqlToken]:
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            i = j
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and text.startswith("''", j):
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            j = min(j + 1, n)
            line += text.count("\n", i, j)
            i = j
        elif ch == '"':
            j = text.find('"', i + 1)
            j = n if j < 0 else j + 1
            yield _SqlToken("quoted", text[i + 1 : j - 1], line, i, j)
            line += text.count("\n", i, j)
            i = j
        elif ch == "$" and (m := re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", text[i : i + 66])):
            tag = m.group(0)
            j = text.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            yield _SqlToken("punct", "$body$", line, i, j)
            line += text.count("\n", i, j)
            i = j
        elif ch.isalnum() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            yield _SqlToken("word", text[i:j], line, i, j)
            i = j
        else:
            yield _SqlToken("punct", ch, line, i, i + 1)
            i += 1


_SQL_OBJECTS: dict[str, str] = {
    "TABLE": "type",
    "VIEW": "type",
    "FUNCTION": "function",
    "PROCEDURE": "function",
    "TYPE": "type",
    "INDEX": "other",
    "TRIGGER": "other",
    "SEQUENCE": "other",
    "SCHEMA": "module",
    "DOMAIN": "type",
}
_SQL_MODIFIERS = frozenset(
    {"OR", "REPLACE", "TEMP", "TEMPORARY", "UNLOGGED", "MATERIALIZED", "RECURSIVE", "UNIQUE", "GLOBAL", "LOCAL"}
)


def _sql(content: bytes, b: _Builder) -> None:
    text = content.decode("utf-8", "replace")
    # байтовые смещения символов (для content_hash)
    statements: list[list[_SqlToken]] = [[]]
    for tok in _sql_tokens(text):
        if tok.kind == "punct" and tok.text == ";":
            statements[-1].append(tok)
            statements.append([])
        else:
            statements[-1].append(tok)
    for stmt in statements:
        words = stmt
        if len(words) < 3 or words[0].kind != "word" or words[0].text.upper() != "CREATE":
            continue
        i = 1
        while i < len(words) and words[i].kind == "word" and words[i].text.upper() in _SQL_MODIFIERS:
            i += 1
        if i >= len(words) or words[i].kind != "word" or words[i].text.upper() not in _SQL_OBJECTS:
            continue
        obj = words[i].text.upper()
        modifiers = " ".join(w.text.upper() for w in words[1:i])
        i += 1
        if obj == "INDEX" and i < len(words) and words[i].text.upper() == "CONCURRENTLY":
            i += 1
        if i + 2 < len(words) and [w.text.upper() for w in words[i : i + 3]] == ["IF", "NOT", "EXISTS"]:
            i += 3
        name_parts: list[str] = []
        while i < len(words) and words[i].kind in ("word", "quoted"):
            name_parts.append(words[i].text)
            if i + 1 < len(words) and words[i + 1].text == ".":
                i += 2
                continue
            i += 1
            break
        if not name_parts or (obj == "INDEX" and name_parts[0].upper() == "ON"):
            continue
        name = ".".join(name_parts)
        kind = _SQL_OBJECTS[obj]
        if obj == "TYPE" and any(w.kind == "word" and w.text.upper() == "ENUM" for w in words[i : i + 3]):
            kind = "enum"
        first, last = stmt[0], stmt[-1]
        start_byte = len(text[: first.start].encode("utf-8"))
        end_byte = len(text[: last.end].encode("utf-8"))
        b.add(
            name=name,
            qualified=name,
            kind=kind,
            start_line=first.line,
            end_line=last.line,
            start_byte=start_byte,
            end_byte=end_byte,
            signature=_collapse(f"CREATE {modifiers} {obj} {name}"),
        )


# ---------------------------------------------------------------------------------------------
# Markdown / YAML / JSON
# ---------------------------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def _line_offsets(content: bytes) -> list[int]:
    offsets = [0]
    for m in re.finditer(rb"\n", content):
        offsets.append(m.end())
    return offsets


def _markdown(content: bytes, b: _Builder) -> None:
    lines = content.decode("utf-8", "replace").split("\n")
    offsets = _line_offsets(content)
    total = line_count(content)
    headings: list[tuple[int, int, str]] = []  # (line, level, title)
    fence: str | None = None
    start = 0
    if lines and lines[0].strip() == "---":  # front matter
        for k in range(1, len(lines)):
            if lines[k].strip() in ("---", "..."):
                start = k + 1
                break
    prev_text: str | None = None
    for idx in range(start, len(lines)):
        line = lines[idx]
        m_fence = _FENCE.match(line)
        if fence is not None:
            if m_fence and m_fence.group(1)[0] == fence[0] and len(m_fence.group(1)) >= len(fence):
                fence = None
            prev_text = None
            continue
        if m_fence:
            fence = m_fence.group(1)
            prev_text = None
            continue
        m = _ATX.match(line)
        if m:
            title = (m.group(2) or "").strip()
            if title:
                headings.append((idx + 1, len(m.group(1)), title))
            prev_text = None
            continue
        m_set = _SETEXT.match(line)
        if m_set and prev_text:
            headings.append((idx, 1 if m_set.group(1)[0] == "=" else 2, prev_text))
            prev_text = None
            continue
        stripped = line.strip()
        prev_text = stripped if stripped and not line.startswith("    ") else None
    stack: list[tuple[int, str]] = []
    for pos, (line_no, level, title) in enumerate(headings):
        end = total
        for later_line, later_level, _ in headings[pos + 1 :]:
            if later_level <= level:
                end = later_line - 1
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = max(end, line_no)
        b.add(
            name=title,
            qualified=" > ".join(t for _, t in stack),
            kind="other",
            start_line=line_no,
            end_line=end,
            start_byte=offsets[line_no - 1],
            end_byte=offsets[end] if end < len(offsets) else len(content),
            signature="#" * level + " " + title,
        )


def _yaml(content: bytes, b: _Builder) -> None:
    offsets = _line_offsets(content)
    try:
        documents = list(yaml.compose_all(content.decode("utf-8", "replace"), Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return
    for doc in documents:
        if not isinstance(doc, yaml.MappingNode):
            continue
        for key, value in doc.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            start = key.start_mark.line + 1
            end_mark = value.end_mark
            end = end_mark.line + 1 if end_mark.column > 0 else max(end_mark.line, start)
            b.add(
                name=str(key.value),
                qualified=str(key.value),
                kind="variable",
                start_line=start,
                end_line=end,
                start_byte=offsets[start - 1],
                end_byte=offsets[end] if end < len(offsets) else len(content),
                signature=f"{key.value}:",
            )


def _json(content: bytes, b: _Builder) -> None:
    text = content.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return
    if not isinstance(parsed, dict):
        return
    offsets = _line_offsets(content)
    for name, start_line, end_line in _json_top_keys(text):
        b.add(
            name=name,
            qualified=name,
            kind="variable",
            start_line=start_line,
            end_line=end_line,
            start_byte=offsets[start_line - 1],
            end_byte=offsets[end_line] if end_line < len(offsets) else len(content),
            signature=f'"{name}":',
        )


def _json_top_keys(text: str) -> list[tuple[str, int, int]]:
    """Ключи верхнего уровня валидного JSON-объекта: (имя, первая строка, последняя строка значения)."""
    result: list[tuple[str, int, int]] = []
    depth = 0
    line = 1
    i, n = 0, len(text)
    expecting_key = False
    current: tuple[str, int] | None = None
    last_value_line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if depth == 1 and expecting_key:
                current = (json.loads(text[i : j + 1]), line)
                expecting_key = False
            last_value_line = line
            i = j + 1
            continue
        elif ch in "{[":
            depth += 1
            if depth == 1 and ch == "{":
                expecting_key = True
        elif ch in "}]":
            if depth == 1 and current is not None:
                result.append((current[0], current[1], line if ch == "}" and last_value_line < line else last_value_line))
                current = None
            depth -= 1
            last_value_line = line
        elif ch == "," and depth == 1:
            if current is not None:
                result.append((current[0], current[1], last_value_line))
                current = None
            expecting_key = True
        elif not ch.isspace() and ch != ":":
            last_value_line = line
        i += 1
    return result


# ---------------------------------------------------------------------------------------------


_STRUCTURED: dict[str, Callable[[bytes, _Builder], None]] = {
    "sql": _sql,
    "markdown": _markdown,
    "yaml": _yaml,
    "json": _json,
}


def analyze_file(
    path: str, language: str, content: bytes, *, module_name: str | None = None, is_test: bool = False
) -> FileAnalysis:
    """Символы и импорты файла. Первый символ — `module` (весь файл)."""
    module = module_name or default_module_name(path, language)
    b = _Builder(content)
    lines = max(line_count(content), 1)
    b.add(
        name=PurePosixPath(path).name,
        qualified=module,
        kind="module",
        start_line=1,
        end_line=lines,
        start_byte=0,
        end_byte=len(content),
    )
    errors = False
    if language in TREE_SITTER_LANGUAGES:
        errors = _tree_sitter(language, content, path, module, is_test, b)
    elif language in _STRUCTURED:
        _STRUCTURED[language](content, b)
    for sym in b.symbols:
        if sym.kind not in SYMBOL_KINDS:  # pragma: no cover - инвариант
            raise AssertionError(sym.kind)
    seen: dict[str, ImportRef] = {}
    for ref in b.imports:
        seen.setdefault(ref.target, ref)  # без дублей, порядок и первая строка сохранены
    imports = tuple(seen.values())
    return FileAnalysis(language=language, symbols=tuple(b.symbols), imports=imports, parse_errors=errors)
