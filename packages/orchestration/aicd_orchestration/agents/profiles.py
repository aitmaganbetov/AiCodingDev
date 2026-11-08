"""Профили 12 ролей и версионированные шаблоны инструкций (P4-02; AGENT_ROLES §1.2, §2, §2.13, §3.1).

Профиль — данные платформы, а не текст задачи: права (`write_scope`, `path_policy`, `network`), классы
инструментов, допустимые для отправки типы ACP (ACP §4.2) и уровни, на которых роль включается по
умолчанию. Содержимое задачи, сообщений и репозитория права не расширяет (AGENT_ROLES §1 п.3).

Шаблоны лежат в пакете: `templates/<role>/<MAJOR.MINOR.PATCH>/role.md` + `manifest.json` (`role`,
`version`, `acp_protocol`, `cli_contract`, sha256 файлов, `changelog`). Выпущенная версия неизменяема:
загрузчик сверяет sha256 с манифестом, а unit-тест — с golden-значениями. Новая формулировка — новая
версия (SemVer: PATCH — текст, MINOR — новые необязательные разделы, MAJOR — контракт или права).

`sync_profiles` синхронизирует справочник `agent_profiles` (DATABASE_DESIGN §7.5, глобальная таблица,
`role UNIQUE`): в строке хранится активная версия шаблона роли. Колонки из «Предлагаемых дополнений»
AGENT_ROLES §5 (`write_scope`, `path_policy`, `network_default`, `template_sha256`) заполняются, если
миграция их создала.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.ids import uuid7

WriteScope = Literal["none", "worktree"]
Network = Literal["none", "restricted", "unrestricted"]

TEMPLATES_ROOT: Final = Path(__file__).parent / "templates"
ACP_PROTOCOL: Final = "acp/1.0"
CLI_CONTRACT: Final = "1.0"
REQUIRED_SECTIONS: Final = ("MISSION", "INPUTS", "PROCEDURE", "CONSTRAINTS", "OUTPUT", "ACP")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

ROLES: Final = (
    "requirements",
    "architect",
    "planner",
    "developer",
    "reviewer",
    "security",
    "qa",
    "database",
    "performance",
    "devops",
    "maintenance",
    "documentation",
)
ACP_TYPES: Final = (
    "TASK_ASSIGN",
    "HANDOFF",
    "QUESTION",
    "ANSWER",
    "REVIEW_REQUEST",
    "FINDING",
    "FIX_REQUEST",
    "EVIDENCE_SUBMITTED",
    "DECISION_PROPOSAL",
    "DECISION_RECORDED",
    "BLOCKED",
    "TASK_COMPLETED",
)
# Типы, которые агент не может отправить никогда: их публикует только orchestrator (ACP §4.1).
ORCHESTRATOR_ONLY: Final = frozenset({"TASK_ASSIGN", "DECISION_RECORDED"})

# Классы инструментов (`agent_profiles.allowed_tools`, AGENT_ROLES §1.2). Конкретный режим CLI выбирает
# `command_profile` адаптера; при расхождении действует более строгое ограничение (SC-CLI-07).
TOOLS_READ: Final = ("file_read", "search")
TOOLS_WRITE: Final = (*TOOLS_READ, "file_write", "sandbox_command")


@dataclass(frozen=True)
class Permissions:
    """Права исполнения роли (AGENT_ROLES §1.2). `path_policy` — allowlist glob внутри worktree."""

    write_scope: WriteScope
    network: Network = "restricted"
    path_policy: tuple[str, ...] = ()
    path_deny: tuple[str, ...] = ()
    task_paths_allowed: bool = False  # дополнительно разрешены файлы, явно перечисленные в задаче
    comments_only_paths: tuple[str, ...] = ()  # в этих путях допустимы только изменения комментариев

    def as_json(self) -> dict[str, Any]:
        return {
            "write_scope": self.write_scope,
            "network": self.network,
            "path_policy": list(self.path_policy),
            "path_deny": list(self.path_deny),
            "task_paths_allowed": self.task_paths_allowed,
            "comments_only_paths": list(self.comments_only_paths),
        }


@dataclass(frozen=True)
class RoleProfile:
    role: str
    template_version: str
    permissions: Permissions
    allowed_tools: tuple[str, ...]
    acp_send: frozenset[str]
    # Уровни L1–L5, на которых роль включается в конвейер по умолчанию вне режимов, где она основная.
    complexity_band: tuple[int, int]
    # Минимальный уровень, с которого результат роли требует независимой проверки другим провайдером
    # (AGENT_ROLES §2.13, колонка «Независимость обязательна»); None — не требуется.
    independent_check_from: int | None
    # reviewer/security: провайдер обязан отличаться от провайдера автора (ROUTING_POLICY §5).
    must_differ_from_author: bool = False
    can_send_fix_request: bool = False
    prototype_permissions: Permissions | None = None  # architect: прототип L5 (AGENT_ROLES §2.2)
    required_capabilities: tuple[str, ...] = ()
    acp_notes: tuple[str, ...] = field(default=())

    @property
    def template_ref(self) -> str:
        """Значение `ExecutionRequest.input.instruction_template_version` (`developer@1.0.0`)."""
        return f"{self.role}@{self.template_version}"


_ACP_ALL_AGENT = frozenset(
    {"QUESTION", "ANSWER", "EVIDENCE_SUBMITTED", "DECISION_PROPOSAL", "BLOCKED", "TASK_COMPLETED"}
)


def _acp(*extra: str) -> frozenset[str]:
    return _ACP_ALL_AGENT | frozenset(extra)


_READ_ONLY = Permissions(write_scope="none")
_DEVELOPER_DENY = (".github/workflows/**", "infrastructure/**", "**/migrations/**")
_TEST_PATHS = (
    "tests/**",
    "**/*test*",
    "**/__tests__/**",
    "e2e/**",
    "**/conftest.py",
    "**/pytest.ini",
    "**/jest.config.*",
    "**/vitest.config.*",
    "**/playwright.config.*",
)
_MANIFESTS = (
    "**/pyproject.toml",
    "**/requirements*.txt",
    "**/uv.lock",
    "**/poetry.lock",
    "**/Pipfile",
    "**/Pipfile.lock",
    "**/package.json",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/go.mod",
    "**/go.sum",
    "**/Cargo.toml",
    "**/Cargo.lock",
    "**/Gemfile",
    "**/Gemfile.lock",
    "**/pom.xml",
    "**/build.gradle*",
)

PROFILES: Final[Mapping[str, RoleProfile]] = {
    p.role: p
    for p in (
        RoleProfile(
            role="requirements",
            template_version="1.0.0",
            permissions=_READ_ONLY,
            allowed_tools=TOOLS_READ,
            acp_send=_acp("HANDOFF"),
            complexity_band=(3, 5),
            independent_check_from=4,
        ),
        RoleProfile(
            role="architect",
            template_version="1.0.0",
            permissions=_READ_ONLY,
            allowed_tools=TOOLS_READ,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST", "FINDING", "FIX_REQUEST"),
            complexity_band=(5, 5),
            independent_check_from=4,
            can_send_fix_request=True,
            prototype_permissions=Permissions(write_scope="worktree", path_policy=("docs/adr/**", "prototype/**")),
        ),
        RoleProfile(
            role="planner",
            template_version="1.0.0",
            permissions=_READ_ONLY,
            allowed_tools=TOOLS_READ,
            acp_send=_acp("HANDOFF"),
            complexity_band=(1, 5),
            independent_check_from=None,  # план проверяется детерминированно (dag.validate_plan)
            acp_notes=("HANDOFF plan_ready — только к orchestrator",),
        ),
        RoleProfile(
            role="developer",
            template_version="1.0.0",
            permissions=Permissions(write_scope="worktree", path_policy=("**",), path_deny=_DEVELOPER_DENY),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST"),
            complexity_band=(1, 5),
            independent_check_from=3,
            required_capabilities=("workspace_write",),
        ),
        RoleProfile(
            role="reviewer",
            template_version="1.0.0",
            permissions=_READ_ONLY,
            allowed_tools=TOOLS_READ,
            acp_send=_acp("FINDING", "FIX_REQUEST"),
            complexity_band=(3, 5),
            independent_check_from=1,
            must_differ_from_author=True,
            can_send_fix_request=True,
        ),
        RoleProfile(
            role="security",
            template_version="1.0.0",
            permissions=_READ_ONLY,
            allowed_tools=TOOLS_READ,
            acp_send=_acp("FINDING", "FIX_REQUEST"),
            complexity_band=(4, 5),
            independent_check_from=1,
            must_differ_from_author=True,
            can_send_fix_request=True,
        ),
        RoleProfile(
            role="qa",
            template_version="1.0.0",
            permissions=Permissions(write_scope="worktree", path_policy=_TEST_PATHS),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "FINDING", "FIX_REQUEST"),
            complexity_band=(2, 5),
            independent_check_from=4,
            can_send_fix_request=True,
            required_capabilities=("workspace_write",),
            acp_notes=("HANDOFF только handoff_kind=test_plan_ready → developer или orchestrator",),
        ),
        RoleProfile(
            role="database",
            template_version="1.0.0",
            permissions=Permissions(
                write_scope="worktree",
                path_policy=("**/migrations/**", "**/models/**", "**/models.py", "**/*.sql", "**/schema.prisma"),
            ),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST", "FINDING", "FIX_REQUEST"),
            complexity_band=(1, 5),
            independent_check_from=3,
            can_send_fix_request=True,
            required_capabilities=("workspace_write",),
        ),
        RoleProfile(
            role="performance",
            template_version="1.0.0",
            permissions=Permissions(
                write_scope="worktree",
                path_policy=("bench/**", "perf/**", "tests/performance/**"),
                task_paths_allowed=True,
            ),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST", "FINDING"),
            complexity_band=(1, 5),
            independent_check_from=3,
            required_capabilities=("workspace_write",),
        ),
        RoleProfile(
            role="devops",
            template_version="1.0.0",
            permissions=Permissions(
                write_scope="worktree",
                path_policy=(
                    ".github/**",
                    ".gitlab-ci*",
                    "infrastructure/**",
                    "Dockerfile*",
                    "**/Dockerfile*",
                    "docker-compose*",
                    "**/*.tf",
                    "helm/**",
                    "k8s/**",
                ),
            ),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST"),
            complexity_band=(1, 5),
            independent_check_from=1,  # изменения workflow-файлов — всегда независимое ревью
            required_capabilities=("workspace_write",),
        ),
        RoleProfile(
            role="maintenance",
            template_version="1.0.0",
            permissions=Permissions(write_scope="worktree", path_policy=_MANIFESTS, task_paths_allowed=True),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST"),
            complexity_band=(1, 5),
            independent_check_from=3,
            required_capabilities=("workspace_write",),
        ),
        RoleProfile(
            role="documentation",
            template_version="1.0.0",
            permissions=Permissions(
                write_scope="worktree",
                path_policy=("docs/**", "**/*.md", "**/openapi*"),
                comments_only_paths=("**",),
            ),
            allowed_tools=TOOLS_WRITE,
            acp_send=_acp("HANDOFF", "REVIEW_REQUEST"),
            complexity_band=(1, 5),
            independent_check_from=3,
            required_capabilities=("workspace_write",),
        ),
    )
}


def profile(role: str) -> RoleProfile:
    try:
        return PROFILES[role]
    except KeyError:
        raise UnknownRoleError(role) from None


class UnknownRoleError(LookupError):
    pass


class TemplateIntegrityError(RuntimeError):
    """Файл шаблона не совпадает с манифестом/контрактом — исполнение с таким шаблоном запрещено."""


def can_send(role: str, message_type: str, to_role: str, *, handoff_kind: str | None = None) -> bool:
    """Предварительная проверка ACL отправителя (ACP §4.2 + уточнение ¹ для qa). Окончательная — брокер."""
    p = PROFILES.get(role)
    if p is None or message_type in ORCHESTRATOR_ONLY or message_type not in p.acp_send:
        return False
    if message_type == "HANDOFF":
        if role == "qa":
            return handoff_kind == "test_plan_ready" and to_role in ("developer", "orchestrator")
        if handoff_kind == "plan_ready":
            return role == "planner" and to_role == "orchestrator"
        if role == "planner":
            return to_role == "orchestrator"
    return True


# --- Шаблоны ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstructionTemplate:
    role: str
    version: str
    text: str
    sha256: str
    manifest: Mapping[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.role}@{self.version}"


def semver_key(version: str) -> tuple[int, int, int]:
    m = _SEMVER.match(version)
    if m is None:
        raise ValueError(f"версия шаблона не SemVer: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def available_versions(role: str) -> list[str]:
    base = TEMPLATES_ROOT / role
    if role not in PROFILES or not base.is_dir():
        return []
    return sorted((d.name for d in base.iterdir() if d.is_dir() and _SEMVER.match(d.name)), key=semver_key)


def sha256_text(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@cache
def load_template(role: str, version: str | None = None) -> InstructionTemplate:
    """Загрузка шаблона роли с проверкой целостности по манифесту."""
    p = profile(role)
    version = version or p.template_version
    semver_key(version)
    base = TEMPLATES_ROOT / role / version
    try:
        raw = (base / "role.md").read_bytes()
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TemplateIntegrityError(f"шаблон {role}@{version} не найден") from exc
    body = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    expected = {"role": role, "version": version, "acp_protocol": ACP_PROTOCOL, "cli_contract": CLI_CONTRACT}
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise TemplateIntegrityError(
                f"{role}@{version}: manifest.{key} = {manifest.get(key)!r}, ожидается {value!r}"
            )
    if manifest.get("files", {}).get("role.md") != digest:
        raise TemplateIntegrityError(f"{role}@{version}: sha256 role.md не совпадает с манифестом")
    missing = [s for s in REQUIRED_SECTIONS if f"\n## {s}\n" not in f"\n{body}"]
    if missing:
        raise TemplateIntegrityError(f"{role}@{version}: нет разделов {missing}")
    return InstructionTemplate(role=role, version=version, text=body, sha256=digest, manifest=manifest)


def template_sections(body: str) -> dict[str, str]:
    """Разделы `## NAME` шаблона (для проверок согласованности с профилем)."""
    sections: dict[str, str] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


# --- Синхронизация справочника agent_profiles -----------------------------------------------------


@dataclass(frozen=True)
class ProfileSyncResult:
    role: str
    version: str
    action: Literal["inserted", "updated", "unchanged", "kept_newer"]


_OPTIONAL_COLUMNS: Final = ("write_scope", "path_policy", "network_default", "template_sha256")


def profile_row(p: RoleProfile) -> dict[str, Any]:
    """Значения строки `agent_profiles` (без id/служебных колонок). Чистая функция для unit-тестов."""
    tpl = load_template(p.role, p.template_version)
    return {
        "role": p.role,
        "instruction_template_version": p.template_version,
        "allowed_tools": json.dumps(list(p.allowed_tools)),
        "required_capabilities": json.dumps(list(p.required_capabilities)),
        "write_scope": p.permissions.write_scope,
        "path_policy": json.dumps(p.permissions.as_json(), ensure_ascii=False, sort_keys=True),
        "network_default": p.permissions.network,
        "template_sha256": tpl.sha256,
    }


async def sync_profiles(conn: AsyncConnection) -> list[ProfileSyncResult]:
    """Идемпотентный upsert активных версий профилей в `agent_profiles` (seed, DATABASE_DESIGN §22).

    Версия в БД не понижается (`kept_newer`): откат шаблона — только новой версией. Если в БД уже записана
    та же версия с другим sha256 (колонка `template_sha256`), выпущенная версия была изменена —
    `TemplateIntegrityError`. Вызывать в транзакции под ролью с правом записи справочника.
    """
    cols = set(
        (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = current_schema() AND table_name = 'agent_profiles'"
                )
            )
        )
        .scalars()
        .all()
    )
    if not cols:
        raise LookupError("таблица agent_profiles отсутствует (миграция Phase 4 не применена)")
    optional = [c for c in _OPTIONAL_COLUMNS if c in cols]
    select_cols = "role, instruction_template_version" + (", template_sha256" if "template_sha256" in cols else "")
    existing = {
        r["role"]: r
        for r in (await conn.execute(text(f"SELECT {select_cols} FROM agent_profiles FOR UPDATE"))).mappings()  # noqa: S608 — имена колонок из константы
    }
    results: list[ProfileSyncResult] = []
    for role in ROLES:
        p = PROFILES[role]
        row = profile_row(p)
        current = existing.get(role)
        if current is not None:
            cur_version = str(current["instruction_template_version"])
            if semver_key(cur_version) > semver_key(p.template_version):
                results.append(ProfileSyncResult(role, cur_version, "kept_newer"))
                continue
            if (
                cur_version == p.template_version
                and "template_sha256" in cols
                and current["template_sha256"] not in (None, row["template_sha256"])
            ):
                raise TemplateIntegrityError(f"{role}@{cur_version}: sha256 в БД отличается — версия неизменяема")
        assignments = ["instruction_template_version", "allowed_tools", "required_capabilities", *optional]
        casts = {"allowed_tools": "jsonb", "required_capabilities": "jsonb", "path_policy": "jsonb"}

        def val(c: str, casts: dict[str, str] = casts) -> str:
            return f"CAST(:{c} AS {casts[c]})" if c in casts else f":{c}"

        insert_cols = ["id", "role", *assignments]
        changed = " OR ".join(f"agent_profiles.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in assignments)
        sql = (
            f"INSERT INTO agent_profiles ({', '.join(insert_cols)})"  # noqa: S608 — имена колонок из константы
            f" VALUES (:id, :role, {', '.join(val(c) for c in assignments)})"
            f" ON CONFLICT (role) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in assignments)}"
            + (", updated_at = now()" if "updated_at" in cols else "")
            + f" WHERE {changed} RETURNING (xmax = 0) AS inserted"
        )
        params = {"id": uuid7(), **{k: row[k] for k in ("role", *assignments)}}
        res = (await conn.execute(text(sql), params)).first()
        if res is None:
            results.append(ProfileSyncResult(role, p.template_version, "unchanged"))
        else:
            results.append(ProfileSyncResult(role, p.template_version, "inserted" if res[0] else "updated"))
    return results
