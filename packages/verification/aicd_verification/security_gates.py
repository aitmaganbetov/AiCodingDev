"""Security, performance и supply-chain gates (P6-04, P6-06, P6-07; QUALITY_GATES §2–§5, ТЗ §14–§15).

Gates (имя → gate_type, инструмент, образ из `tools.images`, закреплён по digest):

| gate                | type    | инструмент                                     | сеть/данные                         |
|---------------------|---------|------------------------------------------------|-------------------------------------|
| `semgrep-sast`      | SAST    | Semgrep, ruleset платформы `tools/rules`       | none; `--metrics=off`, без registry |
| `gitleaks-secrets`  | SECRETS | Gitleaks `dir`, конфигурация платформы         | none                                |
| `trivy-vuln`        | SCA     | Trivy fs `--scanners vuln`                     | none; офлайн-БД в `/aicd-db` (ro)   |
| `osv-scanner-sca`   | SCA     | OSV-Scanner `--offline`                        | none; офлайн-БД в `/aicd-db` (ro)   |
| `trivy-config`      | IAC     | Trivy fs `--scanners misconfig,secret`         | none; встроенные checks             |
| `checkov-iac`       | IAC     | Checkov                                        | none; `--skip-download`             |
| `conftest-policy`   | POLICY  | conftest (OPA) по `*.rego` репозитория         | none                                |
| `bench-perf`        | PERF    | `tools/bench_runner.py` (python, stdlib)       | none; base checkout в `/baseline`   |
| `syft-sbom`         | SBOM    | Syft → CycloneDX JSON                          | none                                |
| `cosign-verify`     | SIGN    | Cosign, ключ из политики платформы             | none; без Rekor (`--insecure-ignore-tlog`) |

Контракт исполнения: фиксированный argv без shell, репозиторий смонтирован read-only в
`/workspace`, вход платформы (ruleset, конфигурации, пустые ignore-файлы, ключи) — read-only в
`/aicd-input`, отчёт инструмента (JSON/CycloneDX) пишется на **stdout**; хост сохраняет его в staging
output dir как сырой отчёт (redacted, sha256) и разбирает в находки. Записываемых bind-mount'ов у
контейнера нет. Конфигурации подавления из репозитория (`.gitleaks.toml`, `.gitleaksignore`,
`.trivyignore`, `trivy.yaml`, `.checkov.yaml`, `osv-scanner.toml`, `.syft.yaml`) перекрываются явными
путями к файлам платформы; `nosemgrep` отключён (`--disable-nosem`). Изменение в diff файлов, которые
инструмент всё же читает из репозитория (`.semgrepignore`, политики `*.rego`), даёт находку `high`
`aicd.gate-config-changed` (FP-07).

Вердикт: инструмент отсутствует/упал/timeout/вывод не разобран → ERROR; находки ≥ порога профиля →
FAIL; иначе PASS (evidence: отчёт + сводка); NOT_APPLICABLE только детектором с причиной.
Офлайн-БД уязвимостей (Trivy, OSV) готовит отдельный одобренный и аудируемый шаг обновления (вне
этого модуля, см. `OfflineDbStatus`); нет БД или она устарела → ERROR (`DB_UNAVAILABLE`), никогда PASS.

Интеграция с движком: `SECURITY_GATE_SPECS` — `GateSpec` для реестра; `SECURITY_GATES` — описание
дополнительных потребностей (файлы входа, монтирование БД/baseline); `run_security_gate` —
эталонное исполнение через любой `SandboxRunner` (используется docker-тестами).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Final

from aicd_platform.redaction import redact_text
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits, SandboxRunner
from aicd_verification.findings import Finding, at_or_above, make_finding, severity_counts
from aicd_verification.gates import (
    CONTAINER_INPUT_DIR,
    CONTAINER_WORKDIR,
    Applicability,
    GateContext,
    GateProvider,
    GateSpec,
    RepoFacts,
    StackInfo,
    Verdict,
)
from aicd_verification.profiles import thresholds
from aicd_verification.tools import images, parsers, perf

CONTAINER_DB_DIR: Final = "/aicd-db"
CONTAINER_BASELINE_DIR: Final = "/baseline"
CONTAINER_HOME: Final = "/home/verify"
_TOOLS_DIR = Path(__file__).with_name("tools")

SEMGREP_RULES_FILE: Final = "semgrep.yml"
GITLEAKS_CONFIG_FILE: Final = "gitleaks.toml"
TRIVY_CONFIG_FILE: Final = "trivy.yaml"
TRIVY_IGNORE_FILE: Final = "trivyignore"
OSV_CONFIG_FILE: Final = "osv-scanner.toml"
CHECKOV_CONFIG_FILE: Final = "checkov.yaml"
SYFT_CONFIG_FILE: Final = "syft.yaml"
BENCH_SPEC_FILE: Final = "bench.json"
COSIGN_KEY_FILE: Final = "cosign.pub"

SEMGREP_RULES: Final = (_TOOLS_DIR / "rules" / "semgrep.yml").read_bytes()
SEMGREP_RULES_SHA256: Final = hashlib.sha256(SEMGREP_RULES).hexdigest()
GITLEAKS_CONFIG: Final = (_TOOLS_DIR / "rules" / "gitleaks.toml").read_bytes()
BENCH_SOURCE: Final = (_TOOLS_DIR / "bench_runner.py").read_text(encoding="utf-8")
BENCH_SHA256: Final = hashlib.sha256(BENCH_SOURCE.encode()).hexdigest()
# Пустые конфигурации платформы: перекрывают одноимённые файлы репозитория.
_EMPTY_TRIVY_CONFIG = b"# aicd: platform-controlled trivy config (no repository overrides)\n"
_EMPTY_IGNORE = b"# aicd: no suppressions\n"
_CHECKOV_CONFIG = b"# aicd: platform-controlled checkov config\nquiet: true\ncompact: true\n"
_SYFT_CONFIG = b"# aicd: platform-controlled syft config\ncheck-for-app-update: false\n"

DEFAULT_SECURITY_TIMEOUT_SECONDS: Final = 900
DEFAULT_DB_MAX_AGE: Final = timedelta(days=7)
GATE_CONFIG_CHANGED_RULE: Final = "aicd.gate-config-changed"

# --- Пороги ---------------------------------------------------------------------------------------


def security_threshold(gate_type: str, profile: str) -> str:
    """Минимальная блокирующая severity: SECRETS — любая находка; POLICY — deny (high) блокирует,
    warn (low) нет; остальное — порог профиля (`basic`: critical, `strict`: high, `critical`: medium)."""
    if gate_type == "SECRETS":
        return "info"
    if gate_type == "POLICY":
        return "medium"
    return thresholds(profile).finding_block_severity


# --- Применимость ---------------------------------------------------------------------------------

SAST_EXTENSIONS: Final = frozenset({".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".php"})
LOCKFILES: Final = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "pdm.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "composer.lock",
        "go.sum",
        "cargo.lock",
        "gemfile.lock",
        "packages.lock.json",
        "pubspec.lock",
        "mix.lock",
    }
)
_REQUIREMENTS_RE = re.compile(r"(^|/)requirements[^/]*\.txt$", re.IGNORECASE)
MANIFESTS: Final = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "package.json", "composer.json", "go.mod", "cargo.toml", "gemfile"}
)
_IAC_RE = re.compile(
    r"(^|/)(dockerfile[^/]*|[^/]*\.dockerfile|containerfile|docker-compose[^/]*\.ya?ml|compose\.ya?ml|"
    r"[^/]*\.tf|[^/]*\.tfvars|chart\.yaml|kustomization\.ya?ml)$"
    r"|(^|/)(k8s|kubernetes|helm|charts|manifests|deploy)/[^/]*\.(ya?ml|json)$"
    r"|^\.github/workflows/[^/]*\.ya?ml$",
    re.IGNORECASE,
)
_EXCLUDED_DIRS: Final = frozenset({"node_modules", ".git", "vendor", ".venv", "venv", "dist", "build", "__pycache__"})
_CONFTEST_EXTENSIONS: Final = frozenset({".yaml", ".yml", ".json", ".toml", ".tf", ".hcl"})
MAX_CONFTEST_INPUTS: Final = 2000


def _excluded(path: str) -> bool:
    return any(part in _EXCLUDED_DIRS for part in PurePosixPath(path).parts[:-1])


def _basename(path: str) -> str:
    return PurePosixPath(path).name.lower()


def sast_sources(files: Iterable[str]) -> list[str]:
    return sorted(p for p in files if PurePosixPath(p).suffix.lower() in SAST_EXTENSIONS and not _excluded(p))


def lockfiles(files: Iterable[str]) -> list[str]:
    return sorted(p for p in files if (_basename(p) in LOCKFILES or _REQUIREMENTS_RE.search(p)) and not _excluded(p))


def manifests(files: Iterable[str]) -> list[str]:
    return sorted(p for p in files if _basename(p) in MANIFESTS and not _excluded(p))


def iac_files(files: Iterable[str]) -> list[str]:
    return sorted(p for p in files if _IAC_RE.search(p) and not _excluded(p))


def rego_policies(files: Iterable[str]) -> list[str]:
    return sorted(p for p in files if p.endswith(".rego") and not p.endswith("_test.rego") and not _excluded(p))


def policy_dirs(files: Iterable[str]) -> list[str]:
    return sorted({str(PurePosixPath(p).parent) for p in rego_policies(files)})


def conftest_inputs(files: Iterable[str], policies: Sequence[str]) -> list[str]:
    """Конфигурационные файлы для conftest: поддерживаемые парсером расширения и Dockerfile, вне
    каталогов политик, зависимостей и helm-шаблонов (`templates/` — не YAML до рендеринга)."""
    out: list[str] = []
    for p in sorted(files):
        pp = PurePosixPath(p)
        if _excluded(p) or "templates" in pp.parts[:-1] or "fixtures" in pp.parts[:-1]:
            continue
        if any(p.startswith(d.rstrip("/") + "/") for d in policies if d != "."):
            continue
        if pp.suffix.lower() in _CONFTEST_EXTENSIONS or pp.name == "Dockerfile":
            out.append(p)
    return out[:MAX_CONFTEST_INPUTS]


@dataclass(frozen=True)
class SignatureSubject:
    """Объект проверки подписи. `kind=image` — OCI layout (`cosign save` одобренного шага prefetch)
    в каталоге входа; `kind=blob` — файл релиза в репозитории и его bundle (`*.sigstore.json`)."""

    kind: str  # image | blob
    name: str
    path: str  # image: относительный путь OCI layout во входе; blob: путь файла в репозитории
    bundle: str | None = None  # blob: путь bundle в репозитории


@dataclass(frozen=True)
class SigningPolicy:
    """Политика подписей платформы (не из репозитория): публичный ключ и проверяемые объекты."""

    public_key_pem: str
    subjects: tuple[SignatureSubject, ...]


def detect_security_applicability(
    facts: RepoFacts,
    *,
    claims: Iterable[Any] = (),
    declared_outputs: Iterable[Any] = (),
    perf_definition_text: str | None = None,
    signing_policy: SigningPolicy | None = None,
) -> dict[str, Applicability]:
    """Детерминированная применимость security/perf/supply-chain gates по файлам commit."""
    files = facts.files
    result: dict[str, Applicability] = {}
    src = sast_sources(files)
    result["semgrep-sast"] = (
        Applicability(True, f"{len(src)} source files in languages covered by the platform ruleset", ("*.py", "*.js"))
        if src
        else Applicability(
            False, "no source files in languages covered by the platform ruleset", tuple(sorted(SAST_EXTENSIONS))
        )
    )
    result["gitleaks-secrets"] = (
        Applicability(True, f"{len(files)} tracked files", ("*",))
        if files
        else Applicability(False, "repository has no tracked files", ("*",))
    )
    locks = lockfiles(files)
    lock_markers = ("requirements*.txt", *sorted(LOCKFILES))
    for name, tool in (("trivy-vuln", "Trivy"), ("osv-scanner-sca", "OSV-Scanner")):
        result[name] = (
            Applicability(True, f"{len(locks)} dependency lockfiles for {tool}", lock_markers)
            if locks
            else Applicability(False, "no dependency lockfiles", lock_markers)
        )
    iac = iac_files(files)
    iac_markers = ("Dockerfile*", "*.tf", "docker-compose*", "k8s/**", "helm/**", ".github/workflows/*")
    for name in ("trivy-config", "checkov-iac"):
        result[name] = (
            Applicability(True, f"{len(iac)} IaC/container configuration files", iac_markers)
            if iac
            else Applicability(False, "no IaC/container configuration files", iac_markers)
        )
    rego = rego_policies(files)
    result["conftest-policy"] = (
        Applicability(True, f"{len(rego)} rego policies in {len(policy_dirs(files))} directories", ("*.rego",))
        if rego and conftest_inputs(files, policy_dirs(files))
        else Applicability(False, "no rego policies" if not rego else "no configuration files to evaluate", ("*.rego",))
    )
    perf_claims = perf.speedup_claims(claims, declared_outputs)
    if not perf_claims:
        result["bench-perf"] = Applicability(False, "agent made no speed-up claim", ("claims", "declared_outputs"))
    else:
        result["bench-perf"] = Applicability(
            True,
            f"{len(perf_claims)} speed-up claim(s)"
            + ("" if perf_definition_text is not None else f"; no {perf.PERF_DEFINITION_PATH} at base commit"),
            ("claims", perf.PERF_DEFINITION_PATH),
        )
    deps = sorted({*locks, *manifests(files)})
    result["syft-sbom"] = (
        Applicability(True, f"{len(deps)} dependency manifests/lockfiles", ("pyproject.toml", "package.json", "*.lock"))
        if deps or iac
        else Applicability(False, "no dependency manifests, lockfiles or container files", ("manifests", "lockfiles"))
    )
    result["cosign-verify"] = (
        Applicability(True, f"signing policy lists {len(signing_policy.subjects)} subject(s)", ("signing_policy",))
        if signing_policy is not None and signing_policy.subjects
        else Applicability(False, "no signing policy requires signatures for this project", ("signing_policy",))
    )
    return result


# --- Офлайн-БД уязвимостей ------------------------------------------------------------------------


@dataclass(frozen=True)
class OfflineDbStatus:
    """Состояние офлайн-БД, подготовленной одобренным шагом обновления (вне sandbox gate).

    Шаг обновления (отдельный аудируемый job с egress allowlist: `mirror.gcr.io` для Trivy,
    `osv-vulnerabilities.storage.googleapis.com` для OSV) выкладывает БД в каталог только для чтения,
    подключаемый gate как `/aicd-db`. Здесь проверяется только наличие и свежесть."""

    ready: bool
    reason: str
    updated_at: datetime | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            **dict(self.details),
        }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def trivy_db_status(db_dir: Path | None, *, now: datetime, max_age: timedelta = DEFAULT_DB_MAX_AGE) -> OfflineDbStatus:
    """`<db_dir>/db/trivy.db` + `metadata.json` (UpdatedAt) — формат `trivy --download-db-only`."""
    if db_dir is None:
        return OfflineDbStatus(False, "no offline trivy DB is configured")
    meta_path, db_path = db_dir / "db" / "metadata.json", db_dir / "db" / "trivy.db"
    if not db_path.is_file() or not meta_path.is_file():
        return OfflineDbStatus(False, f"trivy DB is missing under {db_dir}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OfflineDbStatus(False, "trivy DB metadata.json is unreadable")
    updated = _parse_ts(meta.get("UpdatedAt")) if isinstance(meta, dict) else None
    details = {"schema_version": meta.get("Version") if isinstance(meta, dict) else None, "db": "trivy-db"}
    if updated is None:
        return OfflineDbStatus(False, "trivy DB metadata has no UpdatedAt", None, details)
    if now - updated > max_age:
        return OfflineDbStatus(False, f"trivy DB is stale (updated {updated.isoformat()})", updated, details)
    return OfflineDbStatus(True, "trivy DB is present and fresh", updated, details)


OSV_MANIFEST_FILE: Final = "aicd-osv-db.json"


def osv_db_status(db_dir: Path | None, *, now: datetime, max_age: timedelta = DEFAULT_DB_MAX_AGE) -> OfflineDbStatus:
    """`<db_dir>/osv-scalibr/<Ecosystem>/all.zip` (формат локальной БД OSV-Scanner v2) и манифест
    шага обновления `aicd-osv-db.json` (`{"updated_at": ISO, "ecosystems": [...]}`)."""
    if db_dir is None:
        return OfflineDbStatus(False, "no offline OSV DB is configured")
    zips = sorted(db_dir.glob("osv-scalibr/*/all.zip"))
    if not zips:
        return OfflineDbStatus(False, f"OSV DB is missing under {db_dir}")
    try:
        manifest = json.loads((db_dir / OSV_MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OfflineDbStatus(False, f"OSV DB manifest {OSV_MANIFEST_FILE} is missing or unreadable")
    updated = _parse_ts(manifest.get("updated_at")) if isinstance(manifest, dict) else None
    details = {"ecosystems": [z.parent.name for z in zips], "db": "osv"}
    if updated is None:
        return OfflineDbStatus(False, "OSV DB manifest has no updated_at", None, details)
    if now - updated > max_age:
        return OfflineDbStatus(False, f"OSV DB is stale (updated {updated.isoformat()})", updated, details)
    return OfflineDbStatus(True, "OSV DB is present and fresh", updated, details)


# --- argv ------------------------------------------------------------------------------------------


def _inp(name: str) -> str:
    return f"{CONTAINER_INPUT_DIR}/{name}"


def semgrep_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        "semgrep",
        "scan",
        "--config",
        _inp(SEMGREP_RULES_FILE),
        "--metrics=off",
        "--disable-version-check",
        "--disable-nosem",
        "--json",
        "--quiet",
        "--no-git-ignore",
        "--timeout=60",
        "--jobs=2",
        CONTAINER_WORKDIR,
    )


def gitleaks_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        "gitleaks",
        "dir",
        CONTAINER_WORKDIR,
        "--config",
        _inp(GITLEAKS_CONFIG_FILE),
        "--gitleaks-ignore-path",
        CONTAINER_INPUT_DIR,
        "--no-banner",
        "--no-color",
        "--redact",
        "--exit-code",
        "1",
        "--report-format",
        "json",
        "--report-path",
        "-",
        "--log-level",
        "warn",
    )


def _trivy_common() -> tuple[str, ...]:
    return (
        "--config",
        _inp(TRIVY_CONFIG_FILE),
        "--ignorefile",
        _inp(TRIVY_IGNORE_FILE),
        "--skip-db-update",
        "--skip-java-db-update",
        "--skip-check-update",
        "--offline-scan",
        "--disable-telemetry",
        "--format",
        "json",
        "--exit-code",
        "0",
        "--no-progress",
        "--quiet",
    )


def trivy_vuln_argv(_ctx: GateContext) -> tuple[str, ...]:
    # БД только для чтения: кэш анализа — в памяти, БД — из /aicd-db/db/trivy.db.
    return (
        "trivy",
        "fs",
        "--scanners",
        "vuln",
        "--cache-dir",
        CONTAINER_DB_DIR,
        "--cache-backend",
        "memory",
        *_trivy_common(),
        CONTAINER_WORKDIR,
    )


def trivy_config_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        "trivy",
        "fs",
        "--scanners",
        "misconfig,secret",
        "--cache-dir",
        "/tmp/trivy-cache",  # noqa: S108 — tmpfs контейнера
        *_trivy_common(),
        CONTAINER_WORKDIR,
    )


OSV_BINARY: Final = "/usr/local/bin/osv-scanner"


def osv_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        OSV_BINARY,
        "scan",
        "source",
        "--offline",
        "--config",
        _inp(OSV_CONFIG_FILE),
        "--format",
        "json",
        "--verbosity",
        "warn",
        "-r",
        CONTAINER_WORKDIR,
    )


def checkov_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        "checkov",
        "--config-file",
        _inp(CHECKOV_CONFIG_FILE),
        "-d",
        CONTAINER_WORKDIR,
        "-o",
        "json",
        "--skip-download",
        "--quiet",
        "--compact",
    )


CONFTEST_BINARY: Final = "/conftest"


def conftest_argv(ctx: GateContext) -> tuple[str, ...]:
    files = ctx.facts.files if ctx.facts is not None else frozenset()
    dirs = policy_dirs(files)
    argv: list[str] = [CONFTEST_BINARY, "test", "--all-namespaces", "--no-color", "--output", "json"]
    for d in dirs:
        argv += ["--policy", f"{CONTAINER_WORKDIR}/{d}" if d != "." else CONTAINER_WORKDIR]
    argv += [f"{CONTAINER_WORKDIR}/{p}" for p in conftest_inputs(files, dirs)]
    return tuple(argv)


SYFT_BINARY: Final = "/syft"


def syft_argv(_ctx: GateContext) -> tuple[str, ...]:
    return (
        SYFT_BINARY,
        "scan",
        "-c",
        _inp(SYFT_CONFIG_FILE),
        f"dir:{CONTAINER_WORKDIR}",
        "--source-name",
        "workspace",
        "-o",
        "cyclonedx-json",
        "-q",
    )


COSIGN_BINARY: Final = "/ko-app/cosign"


def cosign_subject_argv(subject: SignatureSubject) -> tuple[str, ...]:
    """Офлайн-проверка ключом политики: без Rekor/Fulcio (`--insecure-ignore-tlog=true`), сеть none.
    Подпись привязана только к ключу платформы — прозрачность (tlog) в sandbox не проверяется."""
    key = _inp(COSIGN_KEY_FILE)
    if subject.kind == "blob":
        if not subject.bundle:
            raise ValueError(f"blob subject {subject.name!r} has no bundle")
        return (
            COSIGN_BINARY,
            "verify-blob",
            "--key",
            key,
            "--bundle",
            f"{CONTAINER_WORKDIR}/{_safe_rel(subject.bundle)}",
            "--insecure-ignore-tlog=true",
            f"{CONTAINER_WORKDIR}/{_safe_rel(subject.path)}",
        )
    if subject.kind == "image":
        return (
            COSIGN_BINARY,
            "verify",
            "--key",
            key,
            "--insecure-ignore-tlog=true",
            "--local-image",
            _inp(f"images/{_safe_rel(subject.path)}"),
        )
    raise ValueError(f"unknown signature subject kind {subject.kind!r}")


def _safe_rel(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts or "\x00" in path:
        raise ValueError(f"unsafe relative path {path!r}")
    return str(p)


def _subject_from_config(ctx: GateContext) -> SignatureSubject | None:
    raw = ctx.config.get("subject") if ctx.config else None
    if not isinstance(raw, Mapping):
        return None
    try:
        return SignatureSubject(
            str(raw["kind"]), str(raw.get("name") or raw["path"]), str(raw["path"]), raw.get("bundle")
        )
    except KeyError:
        return None


def cosign_argv(ctx: GateContext) -> tuple[str, ...]:
    subject = _subject_from_config(ctx)
    if subject is None:
        return (
            COSIGN_BINARY,
            "verify-blob",
            "--key",
            _inp(COSIGN_KEY_FILE),
            "--bundle",
            "/nonexistent",
            "/nonexistent",
        )
    return cosign_subject_argv(subject)


def bench_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", BENCH_SOURCE, _inp(BENCH_SPEC_FILE))


def bench_display(_ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", f"<aicd-bench-runner sha256:{BENCH_SHA256}>", _inp(BENCH_SPEC_FILE))


# --- Интерпретация --------------------------------------------------------------------------------

# Файлы конфигурации, которые инструмент читает из репозитория (подавление/охват) — их изменение в diff
# ослабляет gate (FP-07). Файлы, перекрытые явными путями платформы, сюда не входят.
_REPO_CONTROLLED: Mapping[str, Callable[[str], bool]] = {
    "semgrep-sast": lambda p: _basename(p) == ".semgrepignore",
    "conftest-policy": lambda p: p.endswith(".rego") or _basename(p) == "conftest.toml",
    "checkov-iac": lambda p: _basename(p) in (".checkov.yaml", ".checkov.yml"),
}


def _changed(ctx: GateContext) -> tuple[str, ...]:
    if ctx.changed_files:
        return ctx.changed_files
    return ctx.facts.changed_paths if ctx.facts is not None else ()


def gate_config_findings(gate: str, ctx: GateContext) -> list[Finding]:
    check = _REPO_CONTROLLED.get(gate)
    if check is None:
        return []
    return [
        make_finding(
            gate,
            GATE_CONFIG_CHANGED_RULE,
            "high",
            f"Change to a file that controls the {gate} gate (suppression/policy) requires independent review",
            file_path=p,
            snippet=f"{GATE_CONFIG_CHANGED_RULE}|{p}",
        )
        for p in _changed(ctx)
        if check(p)
    ]


@dataclass(frozen=True)
class ExitPolicy:
    ok: frozenset[int]
    findings: frozenset[int] = frozenset()  # код «найдены нарушения»


def _scanner_verdict(
    gate: str,
    gate_type: str,
    code: int,
    parsed: parsers.ParsedReport,
    ctx: GateContext,
    exits: ExitPolicy,
) -> Verdict:
    if code not in exits.ok | exits.findings:
        hint = f"; {parsed.problem}" if parsed.problem else ""
        return Verdict("ERROR", "INFRA", f"{gate}: tool error (exit {code}){hint}")
    if parsed.problem:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"{gate}: {parsed.problem}")
    findings = (*parsed.findings, *gate_config_findings(gate, ctx))
    if code in exits.findings and not parsed.findings:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"{gate}: exit {code} signals findings but the report has none")
    threshold = security_threshold(gate_type, ctx.profile)
    block = [f for f in findings if at_or_above(f.severity, threshold)]
    counts = severity_counts(findings)
    brief = ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "none"
    if block:
        top = "; ".join(f"{f.rule_id} {f.file_path or ''}:{f.line or ''}".rstrip(":") for f in block[:5])
        return Verdict(
            "FAIL",
            "THRESHOLD",
            f"{len(block)} finding(s) at or above '{threshold}' (profile {ctx.profile}): {top}",
            findings=findings,
        )
    return Verdict("PASS", message=f"no findings at or above '{threshold}' (findings: {brief})", findings=findings)


def evaluate_semgrep(code: int, output: str, ctx: GateContext) -> Verdict:
    parsed = parsers.parse_semgrep(output, rule_prefix=_semgrep_rule_prefix())
    return _scanner_verdict("semgrep-sast", "SAST", code, parsed, ctx, ExitPolicy(frozenset({0})))


def _semgrep_rule_prefix() -> str:
    # Semgrep добавляет к id правила путь к файлу конфигурации без расширения: `aicd-input.aicd.…`.
    return CONTAINER_INPUT_DIR.strip("/").replace("/", ".") + "."


def evaluate_gitleaks(code: int, output: str, ctx: GateContext) -> Verdict:
    return _scanner_verdict(
        "gitleaks-secrets",
        "SECRETS",
        code,
        parsers.parse_gitleaks(output),
        ctx,
        ExitPolicy(frozenset({0}), frozenset({1})),
    )


def evaluate_trivy_vuln(code: int, output: str, ctx: GateContext) -> Verdict:
    # Без офлайн-БД trivy завершается с ошибкой («--skip-db-update cannot be specified on the first
    # run») → exit ≠ 0 → ERROR: SCA без БД никогда не PASS.
    return _scanner_verdict(
        "trivy-vuln", "SCA", code, parsers.parse_trivy(output, gate="trivy-vuln"), ctx, ExitPolicy(frozenset({0}))
    )


def evaluate_trivy_config(code: int, output: str, ctx: GateContext) -> Verdict:
    return _scanner_verdict(
        "trivy-config", "IAC", code, parsers.parse_trivy(output, gate="trivy-config"), ctx, ExitPolicy(frozenset({0}))
    )


def evaluate_osv(code: int, output: str, ctx: GateContext) -> Verdict:
    if code == 128:
        return Verdict("ERROR", "REPORT_UNPARSABLE", "osv-scanner found no package sources although lockfiles exist")
    return _scanner_verdict(
        "osv-scanner-sca", "SCA", code, parsers.parse_osv(output), ctx, ExitPolicy(frozenset({0}), frozenset({1}))
    )


def evaluate_checkov(code: int, output: str, ctx: GateContext) -> Verdict:
    return _scanner_verdict(
        "checkov-iac", "IAC", code, parsers.parse_checkov(output), ctx, ExitPolicy(frozenset({0}), frozenset({1}))
    )


def evaluate_conftest(code: int, output: str, ctx: GateContext) -> Verdict:
    parsed = parsers.parse_conftest(output)
    if parsed.problem is None and code == 0 and any(f.severity == "high" for f in parsed.findings):
        return Verdict("FAIL", "THRESHOLD", "conftest exit 0 but the report lists failures", findings=parsed.findings)
    if code == 1 and parsed.problem is None and not any(f.severity == "high" for f in parsed.findings):
        return Verdict("ERROR", "REPORT_UNPARSABLE", "conftest exit 1 without failures in the report")
    return _scanner_verdict("conftest-policy", "POLICY", code, parsed, ctx, ExitPolicy(frozenset({0}), frozenset({1})))


def evaluate_syft(code: int, output: str, _ctx: GateContext) -> Verdict:
    if code != 0:
        return Verdict("ERROR", "INFRA", f"syft-sbom: tool error (exit {code})")
    parsed = parsers.parse_cyclonedx(output)
    if parsed.problem:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"syft-sbom: {parsed.problem}")
    sha = hashlib.sha256(output.strip().encode("utf-8")).hexdigest()
    return Verdict(
        "PASS",
        message=f"CycloneDX {parsed.summary.get('spec_version')} SBOM: {parsed.summary.get('components')} components,"
        f" sha256 {sha}",
    )


def evaluate_cosign(code: int, output: str, _ctx: GateContext, stderr: str | None = None) -> Verdict:
    status, message = parsers.classify_cosign(code, stderr)
    if status == "PASS":
        return Verdict("PASS", message=message)
    if status == "FAIL":
        return Verdict("FAIL", "THRESHOLD", message)
    return Verdict("ERROR", "INFRA", message)


def _perf_inputs(ctx: GateContext) -> tuple[perf.BenchDefinition | None, list[perf.PerfClaim], str | None]:
    """Определение (с base commit) и заявления передаются движком в `ctx.config`:
    `aicd_perf_definition` (текст .aicd/perf.json на base) и `aicd_perf_claims` (claims AgentResult)."""
    cfg = ctx.config or {}
    text = cfg.get("aicd_perf_definition")
    claims = perf.speedup_claims(cfg.get("aicd_perf_claims") or (), cfg.get("aicd_perf_declared_outputs") or ())
    if not isinstance(text, str):
        return None, claims, f"no {perf.PERF_DEFINITION_PATH} at the base commit: the speed-up claim is unverifiable"
    try:
        return perf.parse_definition(text), claims, None
    except perf.PerfDefinitionError as exc:
        return None, claims, f"invalid {perf.PERF_DEFINITION_PATH} at the base commit: {exc}"


def evaluate_bench(code: int, output: str, ctx: GateContext) -> Verdict:
    definition, claims, problem = _perf_inputs(ctx)
    if definition is None:
        return Verdict("FAIL", "THRESHOLD", problem or "benchmark definition is missing")
    if code != 0:
        return Verdict("ERROR", "INFRA", f"bench-perf: harness error (exit {code})")
    verdict = perf.evaluate_bench_report(output, definition, claims)
    if verdict.status == "PASS":
        return Verdict("PASS", message=verdict.message)
    if verdict.status == "FAIL":
        return Verdict("FAIL", "THRESHOLD", verdict.message)
    return Verdict("ERROR", "INFRA" if verdict.error_code == "INFRA" else "REPORT_UNPARSABLE", verdict.message)


# --- Описания gates -------------------------------------------------------------------------------


InputFiles = Callable[[GateContext], Mapping[str, bytes]]


def _no_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {}


@dataclass(frozen=True)
class SecurityGateDef:
    """Gate + потребности исполнения сверх `GateSpec` v0 (подключает движок/реестр)."""

    spec: GateSpec
    image: images.PinnedImage
    report_name: str  # имя сырого отчёта в staging output dir
    media_type: str = "application/json"
    artifact_kind: str = "scan_report"
    input_files: InputFiles = _no_inputs  # файлы платформы для /aicd-input (ro)
    db: str | None = None  # "trivy" | "osv": нужен ro-mount офлайн-БД в /aicd-db
    needs_baseline: bool = False  # нужен ro-checkout base commit в /baseline
    env: Mapping[str, str] = field(default_factory=dict)
    stderr_aware: bool = False  # вердикту нужен stderr (cosign)


def _semgrep_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {SEMGREP_RULES_FILE: SEMGREP_RULES}


def _gitleaks_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {GITLEAKS_CONFIG_FILE: GITLEAKS_CONFIG}


def _trivy_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {TRIVY_CONFIG_FILE: _EMPTY_TRIVY_CONFIG, TRIVY_IGNORE_FILE: _EMPTY_IGNORE}


def _osv_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {OSV_CONFIG_FILE: _EMPTY_IGNORE}


def _checkov_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {CHECKOV_CONFIG_FILE: _CHECKOV_CONFIG}


def _syft_inputs(_ctx: GateContext) -> Mapping[str, bytes]:
    return {SYFT_CONFIG_FILE: _SYFT_CONFIG}


def _bench_inputs(ctx: GateContext) -> Mapping[str, bytes]:
    definition, _claims, _problem = _perf_inputs(ctx)
    if definition is None:
        return {}
    spec = definition.spec({"baseline": CONTAINER_BASELINE_DIR, "candidate": CONTAINER_WORKDIR})
    return {BENCH_SPEC_FILE: json.dumps(spec).encode()}


def _cosign_inputs(ctx: GateContext) -> Mapping[str, bytes]:
    key = (ctx.config or {}).get("public_key_pem")
    return {COSIGN_KEY_FILE: key.encode()} if isinstance(key, str) else {}


def _spec(
    name: str,
    gate_type: str,
    tool: str,
    probe: tuple[str, ...],
    build: Callable[[GateContext], tuple[str, ...]],
    evaluate: Callable[[int, str, GateContext], Verdict],
    *,
    evidence_kind: str = "scan_report",
    artifact_kind: str = "scan_report",
    display: Callable[[GateContext], tuple[str, ...]] | None = None,
    executes_repository: bool = False,
) -> GateSpec:
    def _interpret(code: int, output: str) -> Verdict:
        return evaluate(code, output, GateContext(CONTAINER_WORKDIR, CONTAINER_INPUT_DIR))

    return GateSpec(
        name,
        gate_type,
        tool,
        probe,
        evidence_kind,
        artifact_kind,
        build,
        display or build,
        _interpret,
        executes_repository=executes_repository,
        image_key=tool,
        evaluate=evaluate,
        needs_input_dir=True,
    )


SECURITY_GATES: Final[dict[str, SecurityGateDef]] = {
    d.spec.name: d
    for d in (
        SecurityGateDef(
            _spec("semgrep-sast", "SAST", "semgrep", ("semgrep", "--version"), semgrep_argv, evaluate_semgrep),
            images.SEMGREP,
            "semgrep.json",
            input_files=_semgrep_inputs,
            env={"SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0"},
        ),
        SecurityGateDef(
            _spec("gitleaks-secrets", "SECRETS", "gitleaks", ("gitleaks", "version"), gitleaks_argv, evaluate_gitleaks),
            images.GITLEAKS,
            "gitleaks.json",
            input_files=_gitleaks_inputs,
        ),
        SecurityGateDef(
            _spec("trivy-vuln", "SCA", "trivy", ("trivy", "--version"), trivy_vuln_argv, evaluate_trivy_vuln),
            images.TRIVY,
            "trivy-vuln.json",
            input_files=_trivy_inputs,
            db="trivy",
        ),
        SecurityGateDef(
            _spec("trivy-config", "IAC", "trivy", ("trivy", "--version"), trivy_config_argv, evaluate_trivy_config),
            images.TRIVY,
            "trivy-config.json",
            input_files=_trivy_inputs,
        ),
        SecurityGateDef(
            _spec("osv-scanner-sca", "SCA", "osv-scanner", (OSV_BINARY, "--version"), osv_argv, evaluate_osv),
            images.OSV_SCANNER,
            "osv-scanner.json",
            input_files=_osv_inputs,
            db="osv",
            env={"OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY": CONTAINER_DB_DIR},
        ),
        SecurityGateDef(
            _spec("checkov-iac", "IAC", "checkov", ("checkov", "--version"), checkov_argv, evaluate_checkov),
            images.CHECKOV,
            "checkov.json",
            input_files=_checkov_inputs,
            env={"BC_SKIP_MAPPING": "TRUE", "CHECKOV_RUN_SCA_PACKAGE_SCAN_V2": "false"},
        ),
        SecurityGateDef(
            _spec(
                "conftest-policy",
                "POLICY",
                "conftest",
                (CONFTEST_BINARY, "--version"),
                conftest_argv,
                evaluate_conftest,
            ),
            images.CONFTEST,
            "conftest.json",
        ),
        SecurityGateDef(
            _spec(
                "bench-perf",
                "PERF",
                "python",
                ("python3", "--version"),
                bench_argv,
                evaluate_bench,
                evidence_kind="benchmark",
                artifact_kind="report",
                display=bench_display,
                executes_repository=True,
            ),
            images.PYTHON_BENCH,
            "bench.json",
            artifact_kind="report",
            input_files=_bench_inputs,
            needs_baseline=True,
        ),
        SecurityGateDef(
            _spec(
                "syft-sbom",
                "SBOM",
                "syft",
                (SYFT_BINARY, "version"),
                syft_argv,
                evaluate_syft,
                evidence_kind="sbom",
                artifact_kind="sbom",
            ),
            images.SYFT,
            "sbom.cdx.json",
            media_type="application/vnd.cyclonedx+json",
            artifact_kind="sbom",
            input_files=_syft_inputs,
            env={"SYFT_CHECK_FOR_APP_UPDATE": "false"},
        ),
        SecurityGateDef(
            _spec(
                "cosign-verify",
                "SIGN",
                "cosign",
                (COSIGN_BINARY, "version"),
                cosign_argv,
                evaluate_cosign,
                evidence_kind="signature",
                artifact_kind="signature",
            ),
            images.COSIGN,
            "cosign.txt",
            media_type="text/plain",
            artifact_kind="signature",
            input_files=_cosign_inputs,
            stderr_aware=True,
        ),
    )
}

SECURITY_GATE_SPECS: Final[dict[str, GateSpec]] = {name: d.spec for name, d in SECURITY_GATES.items()}

# Ключ образа (`GateSpec.image_key` = инструмент) → ссылка с digest; сливается с images.DEFAULT_GATE_IMAGES.
SECURITY_GATE_IMAGES: Final[dict[str, str]] = {d.spec.tool: d.image.ref for d in SECURITY_GATES.values()}

# Seed `quality_gates` (name, gate_type, required, timeout_seconds); обязательность по профилю решает
# PROFILE_MATRIX (IAC в basic — O, POLICY в strict — O, SBOM в strict — O, SIGN — только critical R*).
SECURITY_GATE_DEFINITIONS: Final[tuple[tuple[str, str, bool, int], ...]] = (
    ("semgrep-sast", "SAST", True, DEFAULT_SECURITY_TIMEOUT_SECONDS),
    ("gitleaks-secrets", "SECRETS", True, 600),
    ("trivy-vuln", "SCA", True, 600),
    ("osv-scanner-sca", "SCA", True, 600),
    ("trivy-config", "IAC", True, 600),
    ("checkov-iac", "IAC", True, DEFAULT_SECURITY_TIMEOUT_SECONDS),
    ("conftest-policy", "POLICY", True, 300),
    ("bench-perf", "PERF", True, 1800),
    ("syft-sbom", "SBOM", True, 600),
    ("cosign-verify", "SIGN", True, 300),
)


# --- Эталонное исполнение -------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportFile:
    name: str
    path: Path
    sha256: str
    size: int
    media_type: str


@dataclass(frozen=True)
class SecurityGateResult:
    gate: str
    verdict: Verdict
    exit_code: int | None
    argv: tuple[str, ...]
    image: str
    report: ReportFile | None
    duration_ms: int
    stderr_tail: str = ""
    db: OfflineDbStatus | None = None
    timed_out: bool = False

    def evidence(self) -> dict[str, Any]:
        """Сводка для evidence/отчёта (без секретов)."""
        return {
            "gate": self.gate,
            "status": self.verdict.status,
            "error_code": self.verdict.error_code,
            "message": self.verdict.message,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "image": self.image,
            "command": [a if len(a) < 512 else f"<{len(a)} bytes>" for a in self.argv],
            "report": None
            if self.report is None
            else {"name": self.report.name, "sha256": self.report.sha256, "size": self.report.size},
            "findings": severity_counts(self.verdict.findings),
            "db": self.db.as_json() if self.db else None,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class SecurityRunRequest:
    worktree: Path
    input_dir: Path  # пустой каталог staging (ro в контейнере)
    output_dir: Path  # каталог staging для сырых отчётов (только хост)
    profile: str = "strict"
    facts: RepoFacts | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    db_dir: Path | None = None
    baseline_dir: Path | None = None
    execution_id: str = "security-gate"
    timeout_seconds: int = DEFAULT_SECURITY_TIMEOUT_SECONDS
    limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(cpus=2.0, memory_mb=2048, pids=512, max_output_bytes=32 * 1024 * 1024)
    )
    image_override: str | None = None
    now: datetime | None = None
    db_max_age: timedelta = DEFAULT_DB_MAX_AGE


def build_container_spec(
    definition: SecurityGateDef, request: SecurityRunRequest, argv: Sequence[str]
) -> ContainerSpec:
    mounts = [
        Mount(request.worktree, CONTAINER_WORKDIR, read_only=True),
        Mount(request.input_dir, CONTAINER_INPUT_DIR, read_only=True),
    ]
    if definition.db is not None and request.db_dir is not None:
        mounts.append(Mount(request.db_dir, CONTAINER_DB_DIR, read_only=True))
    if definition.needs_baseline and request.baseline_dir is not None:
        mounts.append(Mount(request.baseline_dir, CONTAINER_BASELINE_DIR, read_only=True))
    env = {
        "CI": "true",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "HOME": CONTAINER_HOME,
        "PYTHONDONTWRITEBYTECODE": "1",
        **definition.env,
    }
    return ContainerSpec(
        image=request.image_override or definition.image.ref,
        argv=tuple(argv),
        workdir=CONTAINER_WORKDIR,
        env=env,
        mounts=tuple(mounts),
        limits=replace(request.limits, timeout_seconds=request.timeout_seconds + 30),
        network="none",
        labels={
            "aicd.execution_id": request.execution_id,
            "aicd.kind": "verification",
            "aicd.gate": definition.spec.name,
        },
    )


def write_inputs(definition: SecurityGateDef, ctx: GateContext, input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    input_dir.chmod(0o755)
    for rel, data in definition.input_files(ctx).items():
        target = input_dir / _safe_rel(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)


async def _execute(
    runner: SandboxRunner, spec: ContainerSpec, limit_seconds: int
) -> tuple[int | None, bytes, bytes, bool]:
    proc = await runner.start(spec)
    out, err = bytearray(), bytearray()
    limit = spec.limits.max_output_bytes

    async def collect() -> int:
        async for chunk in proc.output():
            buf = out if chunk.stream == "stdout" else err
            if len(out) + len(err) < limit:
                buf.extend(chunk.data)
        return await proc.wait()

    try:
        return await asyncio.wait_for(collect(), timeout=limit_seconds), bytes(out), bytes(err), False
    except TimeoutError:
        with contextlib.suppress(Exception):
            await proc.terminate(grace_seconds=5.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=60)
        return None, bytes(out), bytes(err), True


def _store_report(definition: SecurityGateDef, output_dir: Path, text: str) -> ReportFile:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = parsers.redact_report(definition.spec.tool, text).encode("utf-8")
    path = output_dir / definition.report_name
    path.write_bytes(data)
    return ReportFile(definition.report_name, path, hashlib.sha256(data).hexdigest(), len(data), definition.media_type)


async def run_security_gate(
    runner: SandboxRunner,
    name: str,
    request: SecurityRunRequest,
    *,
    argv: Sequence[str] | None = None,
) -> SecurityGateResult:
    """Полный цикл одного gate: файлы входа → проверка офлайн-БД → контейнер (network none, repo ro) →
    сырой отчёт (redacted) в `output_dir` с sha256 → вердикт. Исключения runner'а → ERROR(INFRA)."""
    definition = SECURITY_GATES[name]
    ctx = GateContext(
        CONTAINER_WORKDIR,
        CONTAINER_INPUT_DIR,
        profile=request.profile,
        config=request.config,
        facts=request.facts,
        changed_files=request.facts.changed_paths if request.facts is not None else (),
    )
    command = tuple(argv) if argv is not None else definition.spec.build_argv(ctx)
    image = request.image_override or definition.image.ref
    started = time.monotonic()

    def done(verdict: Verdict, **kw: Any) -> SecurityGateResult:
        return SecurityGateResult(
            name, verdict, kw.pop("exit_code", None), command, image, kw.pop("report", None),
            int((time.monotonic() - started) * 1000), **kw,
        )  # fmt: skip

    db_status: OfflineDbStatus | None = None
    if definition.db is not None:
        now = request.now or datetime.now(UTC)
        check = trivy_db_status if definition.db == "trivy" else osv_db_status
        db_status = check(request.db_dir, now=now, max_age=request.db_max_age)
        if not db_status.ready:
            return done(Verdict("ERROR", "INFRA", f"DB_UNAVAILABLE: {db_status.reason}"), db=db_status)
    if definition.needs_baseline and request.baseline_dir is None:
        return done(Verdict("ERROR", "NO_BASE_COMMIT", "baseline checkout of the base commit is required"))
    if name == "bench-perf" and _perf_inputs(ctx)[0] is None:
        return done(evaluate_bench(0, "", ctx))
    write_inputs(definition, ctx, request.input_dir)
    spec = build_container_spec(definition, request, command)
    try:
        code, out, err, timed_out = await _execute(runner, spec, request.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — сбой запуска контейнера → ERROR, не PASS
        return done(Verdict("ERROR", "INFRA", f"gate process did not start ({type(exc).__name__})"), db=db_status)
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    stderr_tail = redact_text(stderr[-2000:])
    report = _store_report(definition, request.output_dir, stdout if not definition.stderr_aware else stdout + stderr)
    if timed_out or code is None:
        verdict = Verdict("ERROR", "TIMEOUT", f"gate exceeded {request.timeout_seconds}s")
    elif code < 0:
        verdict = Verdict("ERROR", "INFRA", f"gate process killed by signal {-code}")
    elif definition.stderr_aware:
        verdict = evaluate_cosign(code, stdout, ctx, stderr=stderr)
    else:
        verdict = definition.spec.run_evaluate(code, stdout, ctx)
    if verdict.status == "ERROR" and stderr_tail.strip():
        verdict = replace(verdict, message=f"{verdict.message} | stderr: {stderr_tail.strip()[-300:]}")
    return done(verdict, exit_code=code, report=report, stderr_tail=stderr_tail, db=db_status, timed_out=timed_out)


async def run_signature_gate(
    runner: SandboxRunner, policy: SigningPolicy, request: SecurityRunRequest
) -> tuple[Verdict, list[SecurityGateResult]]:
    """SIGN по всем объектам политики: любой FAIL → FAIL; иначе любой ERROR → ERROR; все PASS → PASS."""
    request.input_dir.mkdir(parents=True, exist_ok=True)
    results: list[SecurityGateResult] = []
    for i, subject in enumerate(policy.subjects):
        try:
            argv = cosign_subject_argv(subject)
        except ValueError as exc:
            results.append(
                SecurityGateResult(
                    "cosign-verify", Verdict("ERROR", "INFRA", str(exc)), None, (), images.COSIGN.ref, None, 0
                )
            )
            continue
        sub_request = replace(
            request,
            config={**request.config, "public_key_pem": policy.public_key_pem},
            output_dir=request.output_dir / f"subject-{i}",
            execution_id=f"{request.execution_id}-{i}",
        )
        results.append(await run_security_gate(runner, "cosign-verify", sub_request, argv=argv))
    statuses = [r.verdict.status for r in results]
    if not results:
        return Verdict("ERROR", "INFRA", "signing policy has no subjects"), results
    if "FAIL" in statuses:
        failed = [s.name for s, r in zip(policy.subjects, results, strict=True) if r.verdict.status == "FAIL"]
        return Verdict("FAIL", "THRESHOLD", f"signature not verified for: {', '.join(failed)}"), results
    if "ERROR" in statuses:
        return Verdict("ERROR", "INFRA", "signature verification could not be completed"), results
    return Verdict("PASS", message=f"{len(results)} subject(s) verified with the policy key"), results


def _provider_detect(facts: RepoFacts, _stack: StackInfo) -> Mapping[str, Applicability]:
    """Детектор для реестра. Заявления агента и политика подписей реестру недоступны: движок, у
    которого они есть, вызывает `detect_security_applicability(..., claims=…, signing_policy=…)`."""
    return detect_security_applicability(facts)


_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "semgrep-sast": "SAST: Semgrep, ruleset платформы",
    "gitleaks-secrets": "SECRETS: Gitleaks по файлам commit",
    "trivy-vuln": "SCA: Trivy fs (офлайн-БД уязвимостей)",
    "osv-scanner-sca": "SCA: OSV-Scanner по lock-файлам (офлайн-БД)",
    "trivy-config": "IAC: Trivy misconfig + secret",
    "checkov-iac": "IAC: Checkov",
    "conftest-policy": "POLICY: conftest/OPA по политикам *.rego",
    "bench-perf": "PERF: воспроизводимый бенчмарк baseline vs candidate при заявлении об ускорении",
    "syft-sbom": "SBOM: Syft → CycloneDX JSON",
    "cosign-verify": "SIGN: Cosign verify ключом политики (офлайн)",
}

# Провайдер для `aicd_verification.registry`. Намеренно не называется `PROVIDER` (реестр подхватывает
# его автоматически): включение — решение интеграции (файлы входа, монтирование БД/baseline в движке).
SECURITY_PROVIDER: Final = GateProvider(
    "security",
    tuple(SECURITY_GATE_SPECS.values()),
    _provider_detect,
    tuple((n, t, timeout, _DESCRIPTIONS[n]) for n, t, _req, timeout in SECURITY_GATE_DEFINITIONS),
)
