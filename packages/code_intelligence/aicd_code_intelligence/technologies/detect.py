"""Определение стека проекта по файлам репозитория на заданном commit (P5-07, FR-PRJ-04, AC-02).

Источник файлов — `RepoReader`: `GitCommitReader` читает дерево и blob-ы commit через
`aicd_security.git.run_git` (без checkout рабочего дерева, хуки/fsmonitor/внешние программы отключены),
содержимое репозитория — недоверенные данные: файлы только разбираются (tomllib/json/yaml.safe_load/
регулярные выражения), ничего не исполняется.

Правила:
* языки/рантаймы — из манифестов (`requires-python`, `engines.node`, `require.php`), `.python-version`,
  `.nvmrc`, `tsconfig.json`;
* библиотеки/фреймворки — **прямые** зависимости из манифестов (транзитивные записи lockfile в стек не
  попадают); версия берётся из lockfile, если он есть (точная), иначе — спецификация из манифеста;
* Docker — образы `FROM` в Dockerfile; CI — GitHub Actions / GitLab CI / Jenkins; маркеры фреймворков
  (`manage.py`, `artisan`, `next.config.*`, `angular.json`, `vite.config.*`);
* у каждой технологии — `confidence` (0..1) и `evidence` (файл, строка, пояснение); дубликаты
  (ecosystem, name) объединяются, побеждает версия с наибольшей уверенностью.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

import yaml

from aicd_security.git import GitRepositoryError, run_git

UNSPECIFIED: Final = "unspecified"
MAX_MANIFEST_BYTES: Final = 5 * 1024 * 1024
MAX_FILES_READ: Final = 300

# Уровни уверенности (документированы в отчёте /technologies через detected_from).
CONF_LOCK: Final = 0.95
CONF_PINNED: Final = 0.9
CONF_IMAGE: Final = 0.9
CONF_RANGE: Final = 0.75
CONF_MANIFEST: Final = 0.7
CONF_MARKER: Final = 0.6

_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True)
class Evidence:
    path: str
    line: int | None
    detail: str

    def ref(self) -> str:
        return self.path if self.line is None else f"{self.path}:{self.line}"


@dataclass
class DetectedTechnology:
    ecosystem: str
    name: str
    version_label: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def detected_from(self) -> str:
        """Значение `project_technologies.detected_from`: относительный путь основного свидетельства."""
        return self.evidence[0].path[:1024] if self.evidence else "unknown"


class RepoReader(Protocol):
    def list_files(self) -> list[str]: ...

    def read(self, path: str) -> str | None: ...


# --- Источники файлов ---------------------------------------------------------------------------


class GitCommitReader:
    """Дерево и blob-ы commit без checkout. Большие/бинарные файлы пропускаются."""

    def __init__(self, repo: Path, commit_sha: str) -> None:
        if not _SHA_RE.fullmatch(commit_sha):
            raise ValueError("ожидался полный commit SHA")
        self.repo = repo
        self.commit = run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{commit}}")
        self._sizes: dict[str, int] | None = None
        self._reads = 0

    def _tree(self) -> dict[str, int]:
        if self._sizes is None:
            out = run_git(self.repo, "ls-tree", "-r", "-l", "-z", "--full-tree", self.commit)
            sizes: dict[str, int] = {}
            for entry in out.split("\0"):
                if not entry or "\t" not in entry:
                    continue
                meta, path = entry.split("\t", 1)
                parts = meta.split()
                if len(parts) == 4 and parts[1] == "blob" and parts[0] != "120000":  # symlink-и не читаются
                    sizes[path] = int(parts[3]) if parts[3].isdigit() else 0
            self._sizes = sizes
        return self._sizes

    def list_files(self) -> list[str]:
        return sorted(self._tree())

    def read(self, path: str) -> str | None:
        size = self._tree().get(path)
        if size is None or size > MAX_MANIFEST_BYTES or self._reads >= MAX_FILES_READ:
            return None
        self._reads += 1
        try:
            return run_git(self.repo, "cat-file", "blob", f"{self.commit}:{path}")
        except (GitRepositoryError, UnicodeDecodeError):
            return None


class MappingReader:
    """Файлы из словаря (unit-тесты, предварительно прочитанные снимки)."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def list_files(self) -> list[str]:
        return sorted(self._files)

    def read(self, path: str) -> str | None:
        return self._files.get(path)


# --- Нормализация -------------------------------------------------------------------------------

_ECOSYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,63}$")


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _label(version: str | None) -> str:
    if not version:
        return UNSPECIFIED
    v = " ".join(version.split())[:100]
    return v or UNSPECIFIED


def _line_of(text: str, needle: str) -> int | None:
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i
    return None


def _is_exact(spec: str) -> bool:
    return bool(re.fullmatch(r"(==|=|v)?\s*\d+(\.[0-9A-Za-z-]+)*", spec.strip()))


class _Collector:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], DetectedTechnology] = {}

    def add(self, ecosystem: str, name: str, version: str | None, confidence: float, ev: Evidence) -> None:
        ecosystem = ecosystem.lower()
        name = name.strip()[:200]
        if not name or not _ECOSYSTEM_RE.fullmatch(ecosystem):
            return
        key = (ecosystem, name.lower())
        label = _label(version)
        current = self.items.get(key)
        if current is None:
            self.items[key] = DetectedTechnology(ecosystem, name, label, confidence, [ev])
            return
        if ev not in current.evidence:
            current.evidence.append(ev)
        better = confidence > current.confidence or (current.version_label == UNSPECIFIED and label != UNSPECIFIED)
        if better and not (label == UNSPECIFIED and current.version_label != UNSPECIFIED):
            current.version_label = label
            current.confidence = max(confidence, current.confidence)
            current.evidence.remove(ev)
            current.evidence.insert(0, ev)

    def result(self) -> list[DetectedTechnology]:
        return sorted(self.items.values(), key=lambda t: (t.ecosystem, t.name.lower()))


# --- Python -------------------------------------------------------------------------------------

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")


def _split_requirement(req: str) -> tuple[str, str] | None:
    m = _REQ_RE.match(req)
    if not m:
        return None
    return m.group(1), m.group(3).strip()


def _python_lock_versions(reader: RepoReader, files: set[str], base: str) -> tuple[dict[str, str], str | None]:
    for lock in ("uv.lock", "poetry.lock", "pdm.lock"):
        path = f"{base}{lock}"
        if path in files and (text := reader.read(path)) is not None:
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            versions = {
                _norm_py(str(p["name"])): str(p["version"])
                for p in data.get("package", [])
                if isinstance(p, dict) and "name" in p and "version" in p
            }
            return versions, path
    path = f"{base}Pipfile.lock"
    if path in files and (text := reader.read(path)) is not None:
        try:
            data = json.loads(text)
        except ValueError:
            return {}, None
        versions = {}
        for section in ("default", "develop"):
            for name, info in (data.get(section) or {}).items():
                if isinstance(info, dict) and isinstance(info.get("version"), str):
                    versions[_norm_py(name)] = info["version"].lstrip("=")
        return versions, path
    return {}, None


def _add_python_dep(
    col: _Collector, name: str, spec: str, path: str, text: str, locks: dict[str, str], lock_path: str | None
) -> None:
    key = _norm_py(name)
    if key in ("python", "pip", "setuptools", "wheel"):
        return
    if key in locks and lock_path:
        col.add("python", key, locks[key], CONF_LOCK, Evidence(lock_path, None, f"{name} из lockfile"))
        col.add("python", key, locks[key], CONF_LOCK, Evidence(path, _line_of(text, name), "прямая зависимость"))
        return
    conf = CONF_PINNED if spec.startswith("==") else (CONF_RANGE if spec else CONF_MANIFEST)
    version = spec.removeprefix("==") if spec.startswith("==") else spec
    col.add("python", key, version or None, conf, Evidence(path, _line_of(text, name), "прямая зависимость"))


def _detect_python(reader: RepoReader, files: set[str], col: _Collector) -> None:
    manifests = [f for f in files if PurePosixPath(f).name in ("pyproject.toml", "setup.cfg", "Pipfile")]
    manifests += [f for f in files if re.fullmatch(r"requirements[\w.-]*\.txt", PurePosixPath(f).name)]
    for path in sorted(manifests):
        text = reader.read(path)
        if text is None:
            continue
        base = path[: -len(PurePosixPath(path).name)]
        locks, lock_path = _python_lock_versions(reader, files, base)
        name = PurePosixPath(path).name
        col.add("lang", "python", None, CONF_MANIFEST, Evidence(path, None, "манифест Python"))
        if name == "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            project = data.get("project") or {}
            if isinstance(rp := project.get("requires-python"), str):
                col.add(
                    "lang",
                    "python",
                    rp,
                    CONF_RANGE,
                    Evidence(path, _line_of(text, "requires-python"), "requires-python"),
                )
            deps: list[str] = [d for d in project.get("dependencies") or [] if isinstance(d, str)]
            for group in (project.get("optional-dependencies") or {}).values():
                deps += [d for d in group if isinstance(d, str)]
            for group in (data.get("dependency-groups") or {}).values():
                deps += [d for d in group if isinstance(d, str)]
            for dep in deps:
                if parsed := _split_requirement(dep):
                    _add_python_dep(col, parsed[0], parsed[1], path, text, locks, lock_path)
            poetry = (data.get("tool") or {}).get("poetry") or {}
            for section in ("dependencies", "dev-dependencies"):
                for dep_name, spec in (poetry.get(section) or {}).items():
                    if dep_name.lower() == "python" and isinstance(spec, str):
                        col.add(
                            "lang", "python", spec, CONF_RANGE, Evidence(path, _line_of(text, "python ="), "poetry")
                        )
                        continue
                    version = (
                        spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
                    )
                    _add_python_dep(col, dep_name, str(version), path, text, locks, lock_path)
        elif name == "setup.cfg":
            in_requires = False
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("python_requires"):
                    col.add(
                        "lang",
                        "python",
                        stripped.split("=", 1)[-1].strip(),
                        CONF_RANGE,
                        Evidence(path, i, "python_requires"),
                    )
                if stripped.startswith("install_requires"):
                    in_requires = True
                    continue
                if in_requires:
                    if line and not line[0].isspace():
                        in_requires = False
                    elif stripped and (parsed := _split_requirement(stripped)):
                        _add_python_dep(col, parsed[0], parsed[1], path, text, locks, lock_path)
        elif name == "Pipfile":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            for section in ("packages", "dev-packages"):
                for dep_name, spec in (data.get(section) or {}).items():
                    version = spec if isinstance(spec, str) else ""
                    _add_python_dep(col, dep_name, "" if version == "*" else version, path, text, locks, lock_path)
        else:  # requirements*.txt
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith(("#", "-")):
                    continue
                if parsed := _split_requirement(stripped):
                    _add_python_dep(col, parsed[0], parsed[1], path, text, locks, lock_path)
    for path in sorted(f for f in files if PurePosixPath(f).name == ".python-version"):
        if (text := reader.read(path)) and (v := text.strip().splitlines()[0].strip() if text.strip() else ""):
            col.add("lang", "python", v, CONF_PINNED, Evidence(path, 1, ".python-version"))


# --- Node / TypeScript --------------------------------------------------------------------------


def _npm_lock_versions(reader: RepoReader, files: set[str], base: str) -> tuple[dict[str, str], str | None]:
    path = f"{base}package-lock.json"
    if path in files and (text := reader.read(path)) is not None:
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        versions: dict[str, str] = {}
        for key, info in (data.get("packages") or {}).items():
            top_level = key.startswith("node_modules/") and "/node_modules/" not in key
            if top_level and isinstance(info, dict) and isinstance(info.get("version"), str):
                versions[key.removeprefix("node_modules/")] = info["version"]
        for name, info in (data.get("dependencies") or {}).items():
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                versions.setdefault(name, info["version"])
        return versions, path
    path = f"{base}pnpm-lock.yaml"
    if path in files and (text := reader.read(path)) is not None:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
        versions = {}
        importers = data.get("importers") or {".": data}
        root = importers.get(".") or {}
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, info in (root.get(section) or {}).items():
                raw = info.get("version") if isinstance(info, dict) else info
                if isinstance(raw, str | int | float):
                    versions[name] = str(raw).split("(", 1)[0]
        return versions, path
    path = f"{base}yarn.lock"
    if path in files and (text := reader.read(path)) is not None:
        versions = {}
        current: list[str] = []
        for line in text.splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                current = []
                for spec in line.rstrip(":").split(","):
                    spec = spec.strip().strip('"')
                    name = spec.rsplit("@", 1)[0] if spec.rfind("@") > 0 else spec
                    current.append(name)
            elif current and (m := re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)):
                for name in current:
                    versions.setdefault(name, m.group(1))
                current = []
        return versions, path
    return {}, None


_NODE_FRAMEWORKS_MARKERS: Final = {
    "next.config.js": "next",
    "next.config.mjs": "next",
    "next.config.ts": "next",
    "angular.json": "@angular/core",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
    "nuxt.config.ts": "nuxt",
    "svelte.config.js": "svelte",
}


def _detect_node(reader: RepoReader, files: set[str], col: _Collector) -> None:
    for path in sorted(f for f in files if PurePosixPath(f).name == "package.json" and "node_modules/" not in f):
        text = reader.read(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        base = path[: -len("package.json")]
        locks, lock_path = _npm_lock_versions(reader, files, base)
        lang = (
            "typescript"
            if "typescript" in {**(data.get("devDependencies") or {}), **(data.get("dependencies") or {})}
            else "javascript"
        )
        col.add("lang", lang, None, CONF_MANIFEST, Evidence(path, None, "package.json"))
        engines = data.get("engines") or {}
        if isinstance(engines, dict) and isinstance(engines.get("node"), str):
            col.add(
                "runtime", "node", engines["node"], CONF_RANGE, Evidence(path, _line_of(text, '"node"'), "engines.node")
            )
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            deps = data.get(section) or {}
            if not isinstance(deps, dict):
                continue
            for name, spec in deps.items():
                if not isinstance(name, str):
                    continue
                ev = Evidence(path, _line_of(text, f'"{name}"'), section)
                if name in locks and lock_path:
                    col.add("npm", name, locks[name], CONF_LOCK, Evidence(lock_path, None, f"{name} из lockfile"))
                    col.add("npm", name, locks[name], CONF_LOCK, ev)
                    continue
                spec_s = spec if isinstance(spec, str) else ""
                conf = CONF_PINNED if _is_exact(spec_s) else (CONF_RANGE if spec_s else CONF_MANIFEST)
                col.add("npm", name, spec_s or None, conf, ev)
                if name == "typescript":
                    col.add("lang", "typescript", spec_s or None, conf, ev)
            if "typescript" in locks and lock_path and "typescript" in deps:
                col.add("lang", "typescript", locks["typescript"], CONF_LOCK, Evidence(lock_path, None, "typescript"))
    for path in sorted(f for f in files if PurePosixPath(f).name == "tsconfig.json"):
        col.add("lang", "typescript", None, CONF_MARKER, Evidence(path, None, "tsconfig.json"))
    for path in sorted(f for f in files if PurePosixPath(f).name == ".nvmrc"):
        if (text := reader.read(path)) and text.strip():
            col.add(
                "runtime", "node", text.strip().splitlines()[0].lstrip("v"), CONF_PINNED, Evidence(path, 1, ".nvmrc")
            )
    for path in sorted(files):
        marker = _NODE_FRAMEWORKS_MARKERS.get(PurePosixPath(path).name)
        if marker:
            col.add("npm", marker, None, CONF_MARKER, Evidence(path, None, "маркер фреймворка"))


# --- PHP ----------------------------------------------------------------------------------------


def _detect_php(reader: RepoReader, files: set[str], col: _Collector) -> None:
    for path in sorted(f for f in files if PurePosixPath(f).name == "composer.json" and "vendor/" not in f):
        text = reader.read(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        base = path[: -len("composer.json")]
        locks: dict[str, str] = {}
        lock_path = f"{base}composer.lock"
        if lock_path in files and (lock_text := reader.read(lock_path)) is not None:
            try:
                lock = json.loads(lock_text)
            except ValueError:
                lock = {}
            for section in ("packages", "packages-dev"):
                for pkg in lock.get(section) or []:
                    if (
                        isinstance(pkg, dict)
                        and isinstance(pkg.get("name"), str)
                        and isinstance(pkg.get("version"), str)
                    ):
                        locks[pkg["name"].lower()] = pkg["version"].lstrip("v")
        col.add("lang", "php", None, CONF_MANIFEST, Evidence(path, None, "composer.json"))
        for section in ("require", "require-dev"):
            deps = data.get(section) or {}
            if not isinstance(deps, dict):
                continue
            for name, spec in deps.items():
                ev = Evidence(path, _line_of(text, f'"{name}"'), section)
                spec_s = spec if isinstance(spec, str) else ""
                if name.lower() == "php":
                    col.add("lang", "php", spec_s or None, CONF_RANGE, ev)
                    continue
                if name.lower().startswith("ext-"):
                    continue
                if name.lower() in locks:
                    col.add(
                        "composer",
                        name.lower(),
                        locks[name.lower()],
                        CONF_LOCK,
                        Evidence(lock_path, None, f"{name} из lockfile"),
                    )
                    col.add("composer", name.lower(), locks[name.lower()], CONF_LOCK, ev)
                    continue
                conf = CONF_PINNED if _is_exact(spec_s) else (CONF_RANGE if spec_s else CONF_MANIFEST)
                col.add("composer", name.lower(), spec_s or None, conf, ev)
    for path in sorted(f for f in files if PurePosixPath(f).name == "artisan"):
        col.add("composer", "laravel/framework", None, CONF_MARKER, Evidence(path, None, "маркер Laravel"))


# --- Python-маркеры, Docker, CI -----------------------------------------------------------------

_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?([^\s]+)", re.IGNORECASE)


def _detect_docker(reader: RepoReader, files: set[str], col: _Collector) -> None:
    dockerfiles = [
        f
        for f in files
        if re.fullmatch(r"(Dockerfile|Containerfile)([.-][\w.-]+)?|[\w.-]+\.Dockerfile", PurePosixPath(f).name)
    ]
    stages: set[str] = set()
    for path in sorted(dockerfiles):
        text = reader.read(path)
        if text is None:
            continue
        col.add("tool", "docker", None, CONF_MARKER, Evidence(path, None, "Dockerfile"))
        for i, line in enumerate(text.splitlines(), start=1):
            m = _FROM_RE.match(line)
            if not m:
                continue
            alias = re.search(r"\bAS\s+(\S+)", line, re.IGNORECASE)
            image = m.group(1)
            if alias:
                stages.add(alias.group(1).lower())
            if image.lower() in stages or image.lower() == "scratch" or "$" in image:
                continue
            ref = image.split("@", 1)[0]
            name, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "")
            col.add("docker", name or ref, tag or "latest", CONF_IMAGE, Evidence(path, i, f"FROM {image}"))
    for path in sorted(files):
        name = PurePosixPath(path).name
        if path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
            col.add("ci", "github-actions", None, CONF_PINNED, Evidence(path, None, "workflow"))
        elif name == ".gitlab-ci.yml":
            col.add("ci", "gitlab-ci", None, CONF_PINNED, Evidence(path, None, "pipeline"))
        elif name == "Jenkinsfile":
            col.add("ci", "jenkins", None, CONF_PINNED, Evidence(path, None, "pipeline"))
        elif name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            col.add("tool", "docker-compose", None, CONF_PINNED, Evidence(path, None, "compose"))
        elif name == "manage.py" and (text := reader.read(path)) and "django" in text.lower():
            col.add("python", "django", None, CONF_MARKER, Evidence(path, None, "маркер Django"))


_DETECTORS: Final[tuple[Callable[[RepoReader, set[str], _Collector], None], ...]] = (
    _detect_python,
    _detect_node,
    _detect_php,
    _detect_docker,
)

_SKIP_DIRS: Final = ("node_modules/", "vendor/", ".venv/", "venv/", "site-packages/", "dist/", "build/")


def detect_stack(reader: RepoReader) -> list[DetectedTechnology]:
    files = {f for f in reader.list_files() if not any(f.startswith(d) or f"/{d}" in f for d in _SKIP_DIRS)}
    col = _Collector()
    for detector in _DETECTORS:
        detector(reader, files, col)
    return col.result()


def detect_at_commit(repo: Path, commit_sha: str) -> list[DetectedTechnology]:
    return detect_stack(GitCommitReader(repo, commit_sha))


def summarize(items: Iterable[DetectedTechnology]) -> list[dict[str, Any]]:
    """Сериализуемое представление (журналы/evidence)."""
    return [
        {
            "ecosystem": t.ecosystem,
            "name": t.name,
            "version": t.version_label,
            "confidence": round(t.confidence, 2),
            "evidence": [{"path": e.path, "line": e.line, "detail": e.detail} for e in t.evidence],
        }
        for t in items
    ]
