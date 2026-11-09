"""Seed реестра стандартов из миграции 0008 как объекты `aicd_standards` (для unit-тестов без БД)."""

from __future__ import annotations

import importlib.util
import uuid
from functools import cache
from types import ModuleType
from typing import cast

from aicd_standards.model import Control, VerificationMethod
from tests.support.db import ROOT

MIGRATION = ROOT / "infrastructure" / "migrations" / "versions" / "0008_quality_standards.py"


@cache
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aicd_migration_0008", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def seed_controls() -> tuple[Control, ...]:
    m = migration()
    editions = {code: edition for _n, code, edition, *_ in m.STANDARDS}
    return tuple(
        Control(
            uuid.UUID(m.control_id(n)),
            std,
            editions[std],
            clause,
            code,
            desc,
            cast(VerificationMethod, method),
            role,
            gate,
        )
        for n, std, code, clause, desc, method, role, gate in m.CONTROLS
    )


def control(code: str) -> Control:
    return next(c for c in seed_controls() if c.control_code == code)
