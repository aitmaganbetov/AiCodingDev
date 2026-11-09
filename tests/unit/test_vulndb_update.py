"""OQ-43 (f): одобрение обновления офлайн-БД уязвимостей, проверка архива OSV, атомарная замена и аудит."""

from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest
from scripts.vulndb import approval, update

from aicd_orchestration.approvals import subject_hash


def test_subject_is_canonical_and_bound_to_scope() -> None:
    """Предмет approval VULN_DB_UPDATE: порядок экосистем не влияет на хэш, состав и реестр — влияют."""
    a = approval.build_subject(["npm", "PyPI"], None)
    b = approval.build_subject(["PyPI", "npm"], None)
    assert a == b and a["kinds"] == ["osv"] and a["egress_hosts"] == [approval.OSV_HOST]
    assert subject_hash(a) == subject_hash(b)
    assert subject_hash(a) != subject_hash(approval.build_subject(["PyPI"], None))
    t = approval.build_subject([], "mirror.example/trivy-db:2")
    assert t["kinds"] == ["trivy"] and t["egress_hosts"] == ["mirror.example"]
    assert subject_hash(t) != subject_hash(approval.build_subject([], "other.example/trivy-db:2"))


def test_psql_placeholders_map_to_psycopg() -> None:
    assert approval._VAR.sub(r"%(\1)s", approval.AUTHORIZE_SQL).count("%(") == 5
    assert ":'" not in approval._VAR.sub(r"%(\1)s", approval.REQUEST_SQL)


def test_cli_requires_platform_approval_arguments() -> None:
    with pytest.raises(SystemExit):
        update.main(["apply", "--db-root", "/tmp/x", "--osv-ecosystems", "PyPI"])  # noqa: S108 — нет --approval-id
    with pytest.raises(SystemExit):
        update.main(["request", "--osv-ecosystems", "Bogus", "--workspace-id", str(uuid.uuid4()),
                     "--project-id", str(uuid.uuid4()), "--requested-by", "a@b.c", "--pg-container", "x"])  # fmt: skip


def test_zip_validation(tmp_path: Path) -> None:
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("GHSA-xxxx-yyyy-zzzz.json", json.dumps({"id": "GHSA-xxxx-yyyy-zzzz"}))
    assert update._validate_zip(good) == 1
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.json", "{}")
    with pytest.raises(update.UpdateError, match="недопустимое имя"):
        update._validate_zip(evil)
    wrong = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as zf:
        zf.writestr("a.json", json.dumps(["not", "osv"]))
    with pytest.raises(update.UpdateError, match="формате OSV"):
        update._validate_zip(wrong)


def test_swap_keeps_previous_and_audit(tmp_path: Path) -> None:
    root = tmp_path / "db"
    for version in ("v1", "v2"):
        staging = root / f".staging-{version}"
        staging.mkdir(parents=True)
        (staging / "marker").write_text(version)
        update.swap(root, "osv", staging)
    assert (root / "osv" / "marker").read_text() == "v2"
    assert (root / ".previous" / "osv" / "marker").read_text() == "v1"
    update.audit(root, {"result": "success"})
    assert json.loads((root / "audit.jsonl").read_text().splitlines()[0]) == {"result": "success"}
