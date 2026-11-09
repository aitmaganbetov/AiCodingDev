"""Самопроверка каталога security suite (P9-02): каталог совпадает с SECURITY_CONTROLS, ссылки на тесты существуют,
каждая категория ТЗ §20 покрыта автоматическими тестами."""

from __future__ import annotations

import re

import pytest

from tests.security import catalog
from tests.support.contract import ROOT

pytestmark = pytest.mark.security

CONTROLS_DOC = ROOT / "docs" / "security" / "SECURITY_CONTROLS.md"


def _doc_ids() -> set[str]:
    return set(re.findall(r"^\| (SC-[A-Z]+-\d+) \|", CONTROLS_DOC.read_text(encoding="utf-8"), flags=re.M))


def test_catalog_matches_security_controls_document() -> None:
    doc = _doc_ids()
    assert doc, "в SECURITY_CONTROLS нет контролей"
    assert set(catalog.CONTROLS) == doc, (
        f"нет в каталоге: {sorted(doc - set(catalog.CONTROLS))}; лишние: {sorted(set(catalog.CONTROLS) - doc)}"
    )


def test_every_referenced_test_exists() -> None:
    missing = []
    for cid, control in catalog.CONTROLS.items():
        for ref in control.tests:
            path, _, name = ref.partition("::")
            file = ROOT / path
            if not file.is_file():
                missing.append(f"{cid}: {path}")
                continue
            if name and not re.search(rf"^(async )?def {re.escape(name)}\(", file.read_text(encoding="utf-8"), re.M):
                missing.append(f"{cid}: {ref}")
    assert not missing, "\n".join(missing)


def test_kinds_are_consistent() -> None:
    for cid, control in catalog.CONTROLS.items():
        if control.kind == "manual":
            assert not control.tests and control.note, cid
        else:
            assert control.tests, f"{cid}: {control.kind} без тестов"
        if control.kind == "partial":
            assert control.note, f"{cid}: partial без описания остатка"


def test_every_tz20_category_has_automated_tests() -> None:
    for category in catalog.TZ20_CATEGORIES:
        covered = [cid for cid, c in catalog.CONTROLS.items() if category in c.tz20 and c.tests]
        assert covered, f"ТЗ §20 «{category}»: нет контролей с тестами"
    unknown = {t for c in catalog.CONTROLS.values() for t in c.tz20} - set(catalog.TZ20_CATEGORIES)
    assert not unknown, unknown


def test_nodeid_matching() -> None:
    assert catalog.controls_for_nodeid("tests/security/test_step_up.py::test_reauthenticate_requires_session") == [
        "SC-IAM-08"
    ]
    assert "SC-TEN-04" in catalog.controls_for_nodeid(
        "tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRun]"
    )
    assert catalog.is_security_test("tests/unit/test_passwords.py::test_argon2id_hash_and_verify")
    assert not catalog.is_security_test("tests/unit/test_ids.py::test_uuid7_version_variant_and_monotonic")
    assert not catalog.controls_for_nodeid("tests/unit/test_rbac.pyx::test_x")
