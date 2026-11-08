"""Граф зависимостей и test links (P5-05).

Узлы графа — файлы (в БД — символы `module` файлов). Рёбра:

* `imports` — импорт файла; `target_ref` всегда хранит сырую ссылку (`a.b.c`, `./util`, `App\\Models\\User`,
  `file:./x.php`), `target_path` — разрешённый файл снимка или `None` (внешний пакет/неразрешённое).
  Сырая ссылка позволяет при инкрементальной переиндексации заново разрешить импорты неизменённых файлов
  без их разбора (появившийся файл может сделать импорт разрешимым);
* `tests` — тестовый файл → проверяемый файл: по импортам (тест импортирует нетестовый файл) и по
  соглашениям об именах (`test_foo.py`/`foo_test.py` → `foo.py`, `foo.test.ts`/`foo.spec.tsx` → `foo.ts`,
  `FooTest.php` → `Foo.php`).

Разрешение:

* Python — абсолютные импорты по индексу модулей (корень пакета = первый каталог вверх без `__init__.py`;
  также полный путь от корня репозитория), относительные — по каталогам; при неудаче отбрасывается
  последний компонент (`from a.b import name` → модуль `a.b`);
* JS/TS — относительные спецификаторы (`./`, `../`) с расширениями и `index.*`, `.js` → `.ts` (ESM-TS);
  «голые» спецификаторы — внешние пакеты (tsconfig `paths` в v1 не поддерживаются);
* PHP — `use` по индексу объявленных классов/интерфейсов/функций (FQCN → файл); `require/include` строкой
  относительно файла (и корня репозитория для путей без `./`).
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

_JS_LANGS = frozenset({"javascript", "typescript", "tsx"})
_JS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json")
_JS_REWRITE = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_CODE_LANGS = frozenset({"python", "javascript", "typescript", "tsx", "php"})
_MAX_CONVENTION_TARGETS = 3


@dataclass(frozen=True)
class GraphFile:
    path: str
    language: str | None
    is_test: bool
    imports: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()  # PHP: FQCN объявленных классов/интерфейсов/функций


@dataclass(frozen=True)
class Edge:
    source_path: str
    target_path: str | None
    target_ref: str | None
    edge_type: str


def python_module_names(paths: Iterable[str]) -> dict[str, str]:
    """Имя модуля для каждого `.py`: от корня пакета (первый каталог вверх без `__init__.py`)."""
    py = [p for p in paths if p.endswith((".py", ".pyi"))]
    packages = {posixpath.dirname(p) for p in py if posixpath.basename(p) in ("__init__.py", "__init__.pyi")}
    names: dict[str, str] = {}
    for path in py:
        p = PurePosixPath(path)
        parts = list(p.parent.parts)
        root_len = len(parts)
        while root_len > 0 and "/".join(parts[:root_len]) in packages:
            root_len -= 1
        rel = parts[root_len:]
        mod = [*rel] if p.stem == "__init__" else [*rel, p.stem]
        names[path] = ".".join(mod) if mod else p.stem
    return names


class _Resolver:
    def __init__(self, files: Mapping[str, GraphFile]) -> None:
        self.paths = set(files)
        self.py_index: dict[str, str] = {}
        module_names = python_module_names(self.paths)
        for path in sorted(module_names):
            full = path.rsplit(".", 1)[0].replace("/", ".")
            if full.endswith(".__init__"):
                full = full.removesuffix(".__init__")
            for name in (module_names[path], full):
                current = self.py_index.get(name)
                # __init__.py и модуль с тем же именем: предпочтение модулю; иначе — первый по пути
                if current is None or (current.endswith("__init__.py") and not path.endswith("__init__.py")):
                    self.py_index[name] = path
        self.php_index: dict[str, str] = {}
        for path in sorted(files):
            for fqcn in files[path].declared:
                self.php_index.setdefault(fqcn.lower(), path)

    def resolve(self, source: GraphFile, ref: str) -> str | None:
        lang = source.language
        if lang == "python":
            return self._python(source.path, ref)
        if lang in _JS_LANGS:
            return self._js(source.path, ref)
        if lang == "php":
            return self._php(source.path, ref)
        return None

    def _python(self, path: str, ref: str) -> str | None:
        if ref.startswith("."):
            level = len(ref) - len(ref.lstrip("."))
            rest = ref[level:]
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                if not base:
                    return None
                base = posixpath.dirname(base)
            parts = [x for x in rest.split(".") if x]
            for candidate in (parts, parts[:-1]):
                found = self._py_path(base, candidate)
                if found is not None:
                    return found
            return None
        parts = ref.split(".")
        for candidate in (parts, parts[:-1]):
            if candidate:
                hit = self.py_index.get(".".join(candidate))
                if hit is not None:
                    return hit
        return None

    def _py_path(self, base: str, parts: list[str]) -> str | None:
        if parts:
            stem = posixpath.join(base, *parts)
            options = [f"{stem}.py", f"{stem}.pyi", posixpath.join(stem, "__init__.py")]
        else:
            options = [posixpath.join(base, "__init__.py")]
        for option in options:
            norm = posixpath.normpath(option)
            if norm in self.paths:
                return norm
        return None

    def _js(self, path: str, ref: str) -> str | None:
        if not ref.startswith(("./", "../")) and ref not in (".", ".."):
            return None
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), ref))
        if joined.startswith("../") or joined == "..":
            return None
        candidates = [joined]
        stem, ext = posixpath.splitext(joined)
        candidates.extend(stem + new for new in _JS_REWRITE.get(ext, ()))
        candidates.extend(joined + e for e in _JS_EXTENSIONS)
        candidates.extend(posixpath.join(joined, "index" + e) for e in _JS_EXTENSIONS)
        for c in candidates:
            if c in self.paths:
                return c
        return None

    def _php(self, path: str, ref: str) -> str | None:
        if ref.startswith("file:"):
            spec = ref.removeprefix("file:")
            if spec.startswith("/"):
                return None
            bases = [posixpath.dirname(path)] + ([""] if not spec.startswith(("./", "../")) else [])
            for base in bases:
                norm = posixpath.normpath(posixpath.join(base, spec))
                if not norm.startswith("../") and norm in self.paths:
                    return norm
            return None
        return self.php_index.get(ref.lstrip("\\").lower())


def build_edges(files: Mapping[str, GraphFile]) -> list[Edge]:
    """Все рёбра снимка (детерминированный порядок: по пути источника, затем по ссылке)."""
    resolver = _Resolver(files)
    edges: list[Edge] = []
    import_targets: dict[str, set[str]] = {}
    for path in sorted(files):
        f = files[path]
        if f.language not in _CODE_LANGS:
            continue
        for ref in f.imports:
            target = resolver.resolve(f, ref)
            if target == path:
                continue
            edges.append(Edge(path, target, ref, "imports"))
            if target is not None:
                import_targets.setdefault(path, set()).add(target)
    edges.extend(_test_links(files, import_targets))
    return edges


def _test_subject_names(path: str, language: str | None) -> list[str]:
    name = PurePosixPath(path).name
    if language == "python":
        stem = name.rsplit(".", 1)[0]
        if stem.startswith("test_"):
            return [stem.removeprefix("test_")]
        if stem.endswith("_test"):
            return [stem.removesuffix("_test")]
        return []
    if language in _JS_LANGS:
        for marker in (".test.", ".spec."):
            if marker in name:
                return [name.split(marker, 1)[0]]
        return []
    if language == "php" and name.endswith("Test.php"):
        return [name.removesuffix("Test.php")]
    return []


def _stem(path: str) -> str:
    name = PurePosixPath(path).name
    if name.endswith(".d.ts"):
        return name.removesuffix(".d.ts")
    return name.rsplit(".", 1)[0] if "." in name else name


def _family(language: str | None) -> str | None:
    if language in _JS_LANGS:
        return "js"
    return language


def _shared_dirs(a: str, b: str) -> int:
    pa, pb = PurePosixPath(a).parent.parts, PurePosixPath(b).parent.parts
    shared = len(set(pa) & set(pb))
    return shared + (10 if pa == pb else 0)


def _test_links(files: Mapping[str, GraphFile], import_targets: Mapping[str, set[str]]) -> list[Edge]:
    by_stem: dict[tuple[str | None, str], list[str]] = {}
    for path, f in files.items():
        if not f.is_test and f.language in _CODE_LANGS:
            by_stem.setdefault((_family(f.language), _stem(path)), []).append(path)
    edges: list[Edge] = []
    for path in sorted(files):
        f = files[path]
        if not f.is_test or f.language not in _CODE_LANGS:
            continue
        targets = {t for t in import_targets.get(path, ()) if not files[t].is_test and files[t].language in _CODE_LANGS}
        for subject in _test_subject_names(path, f.language):
            candidates = by_stem.get((_family(f.language), subject), [])
            if not candidates:
                continue
            ranked = sorted(candidates, key=lambda c: (-_shared_dirs(path, c), c))
            best = _shared_dirs(path, ranked[0])
            targets.update(c for c in ranked[:_MAX_CONVENTION_TARGETS] if _shared_dirs(path, c) == best)
        edges.extend(Edge(path, target, None, "tests") for target in sorted(targets))
    return edges
