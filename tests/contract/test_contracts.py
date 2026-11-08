"""P1-03 / AC-15: контракты валидны, реализация соответствует контракту, нет дублей схем."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from aicd_api.main import API_PREFIX, create_app
from aicd_identity.rbac import AUTHENTICATED, PERMISSIONS, PUBLIC
from aicd_platform.config import Settings
from tests.support.contract import OPENAPI_PATH, openapi, operations

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "packages" / "contracts" / "schemas"


def test_openapi_is_valid_3_1() -> None:
    assert openapi()["openapi"].startswith("3.1")
    validate(openapi())


def test_docs_symlink_points_to_single_source() -> None:
    link = ROOT / "docs" / "api" / "openapi.yaml"
    assert link.is_symlink() and link.resolve() == OPENAPI_PATH.resolve()


@pytest.mark.parametrize("path", sorted(SCHEMAS.rglob("*.schema.json")), ids=lambda p: str(p.relative_to(SCHEMAS)))
def test_json_schemas_are_valid(path: Path) -> None:
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/" + str(path.relative_to(SCHEMAS)))


def test_embedded_doc_schemas_equal_contract_files() -> None:
    """Документы protocols/agents не должны расходиться с packages/contracts (ADR-0013)."""
    found = 0
    for doc in [*(ROOT / "docs" / "protocols").glob("*.md"), *(ROOT / "docs" / "agents").glob("*.md")]:
        for block in re.findall(r"```json\n(.*?)```", doc.read_text(), re.S):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            if isinstance(data, dict) and "$schema" in data:
                rel = data["$id"].split("/contracts/", 1)[1]
                assert json.loads((SCHEMAS / rel).read_text()) == data, f"{doc.name}: {rel} расходится"
                found += 1
    assert found == len(list(SCHEMAS.rglob("*.schema.json")))


def _app_routes() -> dict[str, tuple[str, str, dict[str, Any]]]:
    """operationId → (METHOD, path, operation) из OpenAPI, которую генерирует само приложение."""
    spec = create_app(Settings(environment="test", log_level="WARNING")).openapi()
    result = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            result[op["operationId"]] = (method.upper(), path, op)
    return result


def test_implemented_routes_match_contract() -> None:
    contract = operations()
    implemented = _app_routes()
    assert len(implemented) >= 18
    for op_id, (method, path, op) in implemented.items():
        assert op_id in contract, f"{op_id} отсутствует в контракте"
        c_method, c_path, c_op = contract[op_id]
        assert path == API_PREFIX + c_path, f"{op_id}: путь {path} ≠ {c_path}"
        assert method == c_method, f"{op_id}: метод"
        assert op.get("x-required-permission") == c_op["x-required-permission"], op_id


def test_every_contract_permission_is_known_to_rbac() -> None:
    for op_id, (_, _, op) in operations().items():
        perm = op["x-required-permission"]
        assert perm in PERMISSIONS or perm in (PUBLIC, AUTHENTICATED), f"{op_id}: {perm}"


def test_phase1_operations_are_implemented() -> None:
    expected = {
        "login",
        "logout",
        "getSession",
        "healthLive",
        "healthReady",
        "listWorkspaces",
        "createWorkspace",
        "listMembers",
        "addMember",
        "updateMember",
        "removeMember",
        "listProjects",
        "createProject",
        "getProject",
        "updateProject",
        "listRepositories",
        "attachRepository",
        "listAuditEvents",
    }
    assert expected <= set(_app_routes())


def test_canonical_enums_match_readme() -> None:
    readme = (ROOT / "docs" / "README.md").read_text()
    schemas = openapi()["components"]["schemas"]
    checks = {
        "WorkspaceRole": "workspace_role",
        "DocumentationMode": "documentation_mode",
        "QualityProfile": "quality_profile",
    }
    for schema_name, enum_name in checks.items():
        row = re.search(rf"\| `{enum_name}`[^|]*\| (.+?) \|", readme)
        assert row, enum_name
        values = re.findall(r"`([^`]+)`", row.group(1))
        values = [v.replace(" (default)", "") for v in values]
        assert set(schemas[schema_name]["enum"]) == set(values), schema_name
