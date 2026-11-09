"""Модели запросов (зеркало `packages/contracts/openapi.yaml`, `additionalProperties: false`).

Соответствие контракту проверяется контрактными тестами (tests/contract).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from aicd_identity.rbac import Role

DocumentationMode = Literal["OFF", "SMART", "STRICT"]
QualityProfile = Literal["basic", "strict", "critical"]
ProjectStatus = Literal["ACTIVE", "ARCHIVED"]

# Синтаксическая проверка email без DNS/«special-use» ограничений email-validator:
# self-hosted установки используют внутренние домены (.local, .corp, .internal).
Email = Annotated[str, StringConstraints(pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$", max_length=320)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginRequest(_Strict):
    email: Email
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class ReauthenticateRequest(_Strict):
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class WorkspaceCreateRequest(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    # Контракт: ^[a-z0-9][a-z0-9-]{1,62}$; дополнительно правило CHECK БД — без дефиса в конце (OQ-31).
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")]


AgentRole = Literal[
    "requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance",
    "devops", "maintenance", "documentation",
]  # fmt: skip


class ProviderSettingsUpdateRequest(_Strict):
    """Безопасные настройки провайдера в workspace (API_DESIGN §15); иные поля (binary_path, env…) ⇒ 422."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    enabled: bool | None = None
    max_parallel_sessions: Annotated[int, Field(ge=0, le=64, strict=True)] | None = None
    default_timeout_seconds: Annotated[int, Field(ge=60, le=14400, strict=True)] | None = None
    allowed_roles: list[AgentRole] | None = None


class WorkspaceSettingsUpdate(_Strict):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    allow_single_member_self_approval: bool | None = None


class WorkspaceUpdateRequest(_Strict):
    """Merge-patch workspace (OQ-45 (f)); пустое тело и `null` для необнуляемых полей ⇒ 422 в обработчике."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    settings: WorkspaceSettingsUpdate | None = None


class MemberCreateRequest(_Strict):
    email: Email
    role: Role


class MemberUpdateRequest(_Strict):
    role: Role


class ProjectCreateRequest(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(max_length=4000)] | None = None
    documentation_mode: DocumentationMode = "SMART"
    quality_profile: QualityProfile = "strict"


class ProjectUpdateRequest(_Strict):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: Annotated[str, Field(max_length=4000)] | None = None
    status: ProjectStatus | None = None
    quality_profile: QualityProfile | None = None
    routing_policy_id: str | None = None


class RepositoryAttachRequest(_Strict):
    provider: Literal["local", "github"]
    remote_url: Annotated[str, Field(max_length=2048)] | None = None
    local_path: Annotated[str, Field(max_length=4096)] | None = None
    default_branch: Annotated[str, Field(max_length=255)] | None = None
