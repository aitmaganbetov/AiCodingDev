"""Шаг `prepare` — зависимости проекта для gates (QUALITY_GATES §4.3, OQ-43 (b)).

Gates `pytest`/`mypy`/`coverage`/`python -m build` требуют зависимостей **проверяемого проекта**, а
gate-контейнеры работают с `network none` и checkout только для чтения. Поэтому зависимости ставит
отдельный эфемерный контейнер **до** gates задачи:

* образ toolchain по digest, non-root, `cap-drop ALL`, read-only rootfs, лимиты (как у gates);
* вход — checkout задачи **только для чтения** (`/workspace`) + lock-файл; без lock-файла установка
  недетерминирована и запрещена: `ERROR(PREPARE_NO_LOCKFILE)`. Исключение — проект, в корне которого нет
  и манифеста зависимостей (`pyproject.toml`, `requirements*.txt`, …): зависимостей у него нет, ставятся
  только инструменты платформы с фиксированными версиями (`mode = tools-only`);
* выход — каталог кэша `aicd-prep-<sha256>` (ключ по содержимому: workspace + образ + lock + манифест),
  монтируется в gate-контейнеры **только для чтения** как `/deps`;
* сеть — единственный шаг верификации с egress (имя docker-сети задаёт оператор; при заданном
  `proxy_url` — только через egress-proxy с allowlist реестров). Шаги gates остаются `network none`;
* install-скрипты пакетов запрещены (`--no-install-project` для Python: build-backend проверяемого
  проекта в prepare не исполняется);
* отказ prepare → зависимые gates `ERROR(DEPENDENCIES_UNAVAILABLE)`; никогда `PASS` (§5.1).

Отличия от дизайна §4.3, сделанные осознанно:

1. **Каталог вместо именованного тома.** Docker создаёт именованный том с владельцем `root:root`
   (`VolumeOptions.NoCopy = true`, обязательный по политике контейнеров, запрещает инициализацию
   содержимым образа), поэтому non-root процесс prepare в него писать не может. Кэш — bind-mount
   каталога под отдельным корнем `prepare_root`, который создаёт воркер; владелец каталога — uid
   воркера, он же uid контейнера prepare (как `AICD_CLI_RUNNER_USER` для runner'а CLI). В gates
   каталог монтируется ro, поэтому gate не может отравить кэш. Имя каталога — тот же ключ
   `aicd-prep-<sha256>`.
2. **Без строки `quality_gates`.** `prepare` не создаёт собственную строку gate: `gate_type = BUILD`
   сделал бы её обязательным BUILD-гейтом в плане (`profiles.plan_gates`, матрица §2.2 — BUILD `R`
   во всех профилях). Результат prepare попадает в `evidence.environment.prepare` каждого зависимого
   gate (команда, код выхода, образ, digest, sha256 lock-файлов, ключ кэша, попадание в кэш).

Модуль чистый: ничего не исполняет и не обращается к Docker — только описывает спецификации,
считает ключ кэша и собирает окружение gates.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

#: Каталог зависимостей внутри контейнеров (prepare — rw, gates — ro).
DEPS_DIR = "/deps"
#: Метка `aicd.kind` контейнера prepare: по ней политика контейнеров разрешает rw-bind на `/deps`.
PREPARE_KIND = "verification-prepare"
#: Префикс ключа кэша (и имени каталога под `prepare_root`).
CACHE_PREFIX = "aicd-prep-"
#: PATH gate-контейнера: каталог venv зависимостей впереди системного PATH образа.
BASE_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
#: Маркер успешной подготовки внутри каталога кэша (JSON с описанием прогона).
MARKER_NAME = ".aicd-prepare.json"
DEFAULT_TIMEOUT_SECONDS = 900

_SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class PrepareConfigError(ValueError):
    """Спецификация prepare не может быть выполнена безопасно (ключ кэша, корень кэша, uid)."""


@dataclass(frozen=True)
class PrepareSpec:
    """Как ставить зависимости одного toolchain."""

    toolchain: str
    image_key: str  # ключ таблицы образов (images.py)
    #: Кандидаты lock-файла в корне репозитория; первый найденный определяет команду.
    lockfiles: tuple[str, ...]
    #: Дополнительные файлы в ключе кэша (манифест: pyproject.toml / package.json).
    manifests: tuple[str, ...]
    argv: tuple[str, ...]
    #: Дополнительные команды платформы после основной установки: инструменты сборки, которых нет в
    #: lock-файле проекта (их версии фиксирует платформа, а не репозиторий).
    extra_argv: tuple[tuple[str, ...], ...] = ()
    probe: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    #: Подкаталоги кэша, которые создаёт сам prepare (контейнер не может создать их заранее).
    scratch_dirs: tuple[str, ...] = ()
    #: Файлы и шаблоны имён в корне репозитория, объявляющие зависимости проекта. Если нет ни lock-файла,
    #: ни одного из них, зависимостей у проекта нет — ставятся только инструменты платформы (`bare_*`).
    dependency_manifests: tuple[str, ...] = ()
    #: Создание пустого окружения для проекта без объявленных зависимостей (пусто — режим не поддержан).
    bare_argv: tuple[str, ...] = ()
    #: Инструменты платформы для такого проекта (обычно приходят из lock-файла проекта); версии фиксированы.
    bare_extra_argv: tuple[tuple[str, ...], ...] = ()


PYTHON_SPEC = PrepareSpec(
    toolchain="python",
    image_key="prepare-python",
    lockfiles=("uv.lock",),
    manifests=("pyproject.toml",),
    # `--frozen` — lock-файл не обновляется (сеть только за пакетами), `--no-install-project` —
    # build-backend проверяемого проекта не исполняется, `--python` фиксирует интерпретатор образа
    # (путь совпадает с gate-образом python, поэтому symlink venv разрешается и в gate-контейнере).
    argv=(
        "uv",
        "sync",
        "--frozen",
        "--no-install-project",
        "--no-progress",
        "--python",
        "/usr/local/bin/python3.12",
    ),
    # `python -m build --no-isolation` требует и frontend `build`, и build-backend проверяемого
    # проекта (`[build-system].requires`): в lock-файле зависимостей их нет — это инструменты
    # платформы, и версии фиксирует платформа. Устанавливаются в тот же venv после `uv sync`.
    extra_argv=(
        (
            "uv",
            "pip",
            "install",
            "--python",
            f"{DEPS_DIR}/venv/bin/python3",
            "--no-progress",
            "build==1.3.0",
            "hatchling==1.27.0",
            "setuptools==80.9.0",
            "wheel==0.45.1",
            "poetry-core==2.2.1",
            "flit-core==3.12.0",
        ),
    ),
    probe=("uv", "--version"),
    env={
        "UV_PROJECT_ENVIRONMENT": f"{DEPS_DIR}/venv",
        "UV_CACHE_DIR": f"{DEPS_DIR}/cache/uv",
        # hardlink между кэшем и venv внутри одного каталога возможен, но copy надёжнее при
        # повторном использовании каталога другим контейнером.
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
        # Интерпретатор берётся только из образа: загрузка сборок Python из сети запрещена.
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_COMPILE_BYTECODE": "0",
        "UV_HTTP_TIMEOUT": "120",
    },
    scratch_dirs=("cache/uv",),
    dependency_manifests=("pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg", "Pipfile"),
    bare_argv=("uv", "venv", "--no-progress", "--python", "/usr/local/bin/python3.12", f"{DEPS_DIR}/venv"),
    bare_extra_argv=(
        (
            "uv",
            "pip",
            "install",
            "--python",
            f"{DEPS_DIR}/venv/bin/python3",
            "--no-progress",
            "pytest==9.2.0",
            "pytest-cov==7.1.0",
            "coverage==7.17.2",
        ),
    ),
)

#: toolchain → спецификация. Node/PHP (`pnpm install --ignore-scripts`, `composer install --no-scripts`)
#: дизайном §4.3 предусмотрены, но здесь не зарегистрированы: без них gates JS/PHP ведут себя как
#: раньше (probe инструмента падает → `ERROR(TOOL_UNAVAILABLE)`, никогда `PASS`).
SPECS: Mapping[str, PrepareSpec] = {PYTHON_SPEC.toolchain: PYTHON_SPEC}


@dataclass(frozen=True)
class PrepareResult:
    """Результат подготовки зависимостей одного toolchain для задачи."""

    toolchain: str
    ok: bool
    cache_key: str
    error_code: str | None = None
    message: str = ""
    record: Mapping[str, object] = field(default_factory=dict)
    #: Окружение, добавляемое в gate-контейнер (PATH/PYTHONPATH/VIRTUAL_ENV …).
    gate_env: Mapping[str, str] = field(default_factory=dict)
    #: Каталог кэша на хосте (монтируется в gates как `/deps` только для чтения).
    cache_dir: Path | None = None


def spec_for(toolchain: str) -> PrepareSpec | None:
    return SPECS.get(toolchain)


def find_lockfile(repo: Path, spec: PrepareSpec) -> str | None:
    for name in spec.lockfiles:
        if (repo / name).is_file():
            return name
    return None


def declares_dependencies(repo: Path, spec: PrepareSpec) -> bool:
    """В корне репозитория есть манифест зависимостей (lock-файл проверяется отдельно)."""
    return any(any(p.is_file() for p in repo.glob(pattern)) for pattern in spec.dependency_manifests)


def bare_spec(spec: PrepareSpec) -> PrepareSpec | None:
    """Рецепт для проекта без объявленных зависимостей: пустое окружение + инструменты платформы.

    Установка остаётся детерминированной (версии фиксирует платформа), поэтому правило «без lock-файла
    зависимости не ставятся» не нарушается: зависимостей проекта здесь нет.
    """
    if not spec.bare_argv:
        return None
    return replace(spec, argv=spec.bare_argv, extra_argv=(*spec.bare_extra_argv, *spec.extra_argv))


def cache_key(
    *,
    workspace_id: str,
    spec: PrepareSpec,
    image: str,
    repo: Path,
    files: Sequence[str],
) -> tuple[str, dict[str, str]]:
    """Ключ кэша по содержимому и sha256 учтённых файлов.

    Ключ включает `workspace_id` (изоляция кэша между тенантами — нет отравления между workspace),
    ссылку на образ с digest, версию формата и sha256 каждого учтённого файла.
    """
    digests: dict[str, str] = {}
    h = hashlib.sha256()
    h.update(b"aicd-prepare/1\0")
    h.update(workspace_id.encode() + b"\0")
    h.update(spec.toolchain.encode() + b"\0")
    h.update(image.encode() + b"\0")
    h.update(b"\0".join(a.encode() for a in spec.argv) + b"\0")
    for name in files:
        path = repo / name
        if not path.is_file():
            continue
        file_hash = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                file_hash.update(chunk)
        digest = file_hash.hexdigest()
        digests[name] = digest
        h.update(name.encode() + b"=" + digest.encode() + b"\0")
    key = CACHE_PREFIX + h.hexdigest()[:32]
    if not _SAFE_KEY_RE.fullmatch(key):  # pragma: no cover — хеш всегда [0-9a-f]
        raise PrepareConfigError(f"unsafe cache key {key!r}")
    return key, digests


# --- Корни исходников проекта для PYTHONPATH ------------------------------------------------------
#
# `--no-install-project` не ставит проверяемый проект (его build-backend — недоверенный код, в
# prepare он не исполняется), поэтому first-party пакеты импортируются из checkout по PYTHONPATH.
# Корни берутся **только из объявленной конфигурации** репозитория (детерминированно, без исполнения
# кода проекта): `tool.pytest.ini_options.pythonpath`, `tool.mypy.mypy_path`,
# `tool.hatch.build.targets.wheel.packages` (каталог-родитель пакета) и каталог `src/`.

_MAX_SOURCE_ROOTS = 64


def _relative_dirs(repo: Path, candidates: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in candidates:
        value = str(raw).strip()
        if not value or value.startswith(("/", "~")):
            continue
        p = PurePosixPath(value.replace("\\", "/"))
        if ".." in p.parts or any(ch in value for ch in ("*", "?", "\0")):
            continue
        rel = str(p)
        if rel in (".", ""):
            rel = "."
        if (repo / rel).is_dir() and rel not in out:
            out.append(rel)
    return out


def source_roots(repo: Path) -> tuple[str, ...]:
    """Объявленные корни исходников проекта (относительные пути внутри checkout)."""
    candidates: list[str] = []
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, OSError, ValueError):
            data = {}
        tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
        pytest_cfg = (tool.get("pytest") or {}).get("ini_options") or {} if isinstance(tool, dict) else {}
        if isinstance(pytest_cfg, dict):
            value = pytest_cfg.get("pythonpath")
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(str(v) for v in value)
        mypy_cfg = (tool.get("mypy") or {}) if isinstance(tool, dict) else {}
        if isinstance(mypy_cfg, dict):
            mypy_path = mypy_cfg.get("mypy_path")
            if isinstance(mypy_path, str):
                candidates.extend(mypy_path.split(":"))
            elif isinstance(mypy_path, list):
                candidates.extend(str(v) for v in mypy_path)
        hatch = (tool.get("hatch") or {}) if isinstance(tool, dict) else {}
        wheel = (
            (((hatch.get("build") or {}).get("targets") or {}).get("wheel") or {}) if isinstance(hatch, dict) else {}
        )
        packages = wheel.get("packages") if isinstance(wheel, dict) else None
        if isinstance(packages, list):
            for item in packages:
                parent = str(PurePosixPath(str(item)).parent)
                candidates.append(parent if parent not in ("", ".") else ".")
    candidates.append("src")
    return tuple(_relative_dirs(repo, candidates)[:_MAX_SOURCE_ROOTS])


def python_gate_env(workdir: str, roots: Sequence[str]) -> dict[str, str]:
    """Окружение gate-контейнера Python: venv зависимостей + корни исходников проекта."""
    venv = f"{DEPS_DIR}/venv"
    pythonpath = [workdir if r == "." else f"{workdir.rstrip('/')}/{r}" for r in roots]
    env = {
        "PATH": f"{venv}/bin:{BASE_PATH}",
        "VIRTUAL_ENV": venv,
        "PYTHONNOUSERSITE": "1",
    }
    if pythonpath:
        env["PYTHONPATH"] = ":".join(pythonpath)
    return env


# Корни исходников в `PYTHONPATH` видит только сам процесс gate: дочерние процессы, которые тесты
# проекта запускают с очищенным окружением (`env=`, `-E`, `-I`), их теряют и падают с
# `ModuleNotFoundError`. Поэтому prepare дополнительно кладёт в site-packages venv файл `.pth` с теми
# же корнями — так их видит любой запуск интерпретатора из `/deps/venv`, как после `pip install -e`.
# Скрипт фиксированный (его sha256 попадает в evidence), корни передаются только аргументами argv.
SRC_PTH_NAME = "aicd-prepare-src.pth"
SRC_PTH_DRIVER = f"""import pathlib, sys, sysconfig
roots = [r for r in sys.argv[1:] if r]
target = pathlib.Path(sysconfig.get_paths()["purelib"]) / "{SRC_PTH_NAME}"
target.write_text("".join(r + chr(10) for r in roots), encoding="utf-8")
print("aicd-prepare-src", target, len(roots))
"""
SRC_PTH_DRIVER_SHA256 = hashlib.sha256(SRC_PTH_DRIVER.encode()).hexdigest()


def src_pth_argv(workdir: str, roots: Sequence[str]) -> tuple[str, ...]:
    """argv записи `.pth` с корнями исходников (пути внутри gate-контейнера)."""
    paths = [workdir if r == "." else f"{workdir.rstrip('/')}/{r}" for r in roots]
    return (f"{DEPS_DIR}/venv/bin/python3", "-c", SRC_PTH_DRIVER, *paths)


def src_pth_display(workdir: str, roots: Sequence[str]) -> tuple[str, ...]:
    argv = src_pth_argv(workdir, roots)
    return (argv[0], "-c", f"<aicd-prepare-src-pth sha256:{SRC_PTH_DRIVER_SHA256}>", *argv[3:])


GATE_ENV_BUILDERS = {"python": python_gate_env}


def gate_env(toolchain: str, workdir: str, roots: Sequence[str]) -> dict[str, str]:
    builder = GATE_ENV_BUILDERS.get(toolchain)
    return builder(workdir, roots) if builder is not None else {}
