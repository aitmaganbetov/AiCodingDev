"""Применимость контролей к проекту (ТЗ §13, DATABASE_DESIGN §13.3, QUALITY_GATES §8.1).

Платформа только **предлагает** применимость по определённому стеку (`project_technologies`, P5-07):
черновик `project_controls` без `approved_by`. Утверждает (или меняет) применимость человек роли admin;
Compliance Engine учитывает лишь утверждённые строки. Релевантность контроля режиму run (`runs.mode`)
проверяется при оценке конкретного run (`relevant_for_mode`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from aicd_standards.catalog import rule_for
from aicd_standards.model import Applicability, Control

PACKAGE_ECOSYSTEMS = frozenset({"python", "npm", "composer"})
WEB_FRAMEWORKS = frozenset(
    {
        "fastapi",
        "django",
        "flask",
        "starlette",
        "aiohttp",
        "express",
        "koa",
        "fastify",
        "next",
        "nuxt",
        "react",
        "vue",
        "svelte",
        "@angular/core",
        "@nestjs/core",
        "laravel/framework",
        "symfony/framework-bundle",
        "symfony/http-kernel",
        "slim/slim",
    }
)
DATABASE_LIBRARIES = frozenset(
    {
        "sqlalchemy",
        "alembic",
        "psycopg",
        "psycopg2",
        "psycopg2-binary",
        "asyncpg",
        "pymysql",
        "mysqlclient",
        "django",
        "peewee",
        "tortoise-orm",
        "prisma",
        "@prisma/client",
        "pg",
        "mysql2",
        "typeorm",
        "sequelize",
        "knex",
        "drizzle-orm",
        "doctrine/orm",
        "doctrine/dbal",
        "laravel/framework",
    }
)
DATABASE_IMAGES = frozenset({"postgres", "postgresql", "mysql", "mariadb", "mongo"})
TYPE_CHECKERS = frozenset({"mypy", "pyright", "phpstan/phpstan", "vimeo/psalm"})
TAG_TITLES: dict[str, str] = {
    "code": "исходный код",
    "dependencies": "зависимости из менеджера пакетов",
    "web": "веб-фреймворк",
    "database": "база данных или ORM",
    "container": "контейнеры (Dockerfile/compose)",
    "typed": "статическая типизация",
}


@dataclass(frozen=True)
class StackFacts:
    """Стек проекта: пары (ecosystem, name) из `project_technologies` → `technologies`."""

    technologies: frozenset[tuple[str, str]]

    @classmethod
    def of(cls, pairs: Iterable[tuple[str, str]]) -> StackFacts:
        return cls(frozenset((e.lower(), n.lower()) for e, n in pairs))


@dataclass(frozen=True)
class ApplicabilityProposal:
    control_id: uuid.UUID
    control_code: str
    applicability: Applicability
    rationale: str
    agent_role: str | None


def _image_name(name: str) -> str:
    return name.rsplit("/", 1)[-1].split(":", 1)[0]


def stack_tags(facts: StackFacts) -> frozenset[str]:
    """Теги стека, по которым каталог решает применимость (см. `catalog`)."""
    tags: set[str] = set()
    for ecosystem, name in facts.technologies:
        if ecosystem == "lang" or ecosystem in PACKAGE_ECOSYSTEMS:
            tags.add("code")
        if ecosystem in PACKAGE_ECOSYSTEMS:
            tags.add("dependencies")
        if ecosystem in PACKAGE_ECOSYSTEMS and name in WEB_FRAMEWORKS:
            tags.add("web")
        if (ecosystem in PACKAGE_ECOSYSTEMS and name in DATABASE_LIBRARIES) or (
            ecosystem == "docker" and _image_name(name) in DATABASE_IMAGES
        ):
            tags.add("database")
        if ecosystem == "docker" or (ecosystem == "tool" and name in {"docker", "docker-compose"}):
            tags.add("container")
        if (ecosystem == "lang" and name == "typescript") or name in TYPE_CHECKERS:
            tags.add("typed")
    return frozenset(tags)


def _titles(tags: Iterable[str]) -> str:
    return ", ".join(TAG_TITLES.get(t, t) for t in sorted(tags))


def propose(controls: Sequence[Control], facts: StackFacts) -> list[ApplicabilityProposal]:
    """Черновики применимости для всех контролей реестра по стеку проекта (детерминированно)."""
    tags = stack_tags(facts)
    proposals: list[ApplicabilityProposal] = []
    for control in sorted(controls, key=lambda c: (c.standard_code, c.control_code)):
        rule = rule_for(control.control_code)
        if not rule.requires_any:
            applicability: Applicability = "applicable"
            rationale = "Применим к любому проекту (правило платформы)."
        elif matched := rule.requires_any & tags:
            applicability = "applicable"
            rationale = f"В стеке проекта обнаружено: {_titles(matched)}."
        else:
            applicability = "not_applicable"
            rationale = f"В стеке проекта не обнаружено: {_titles(rule.requires_any)}."
        proposals.append(
            ApplicabilityProposal(
                control.id, control.control_code, applicability, rationale, control.default_agent_role
            )
        )
    return proposals


def relevant_for_mode(control_code: str, mode: str) -> bool:
    modes = rule_for(control_code).modes
    return modes is None or mode in modes
