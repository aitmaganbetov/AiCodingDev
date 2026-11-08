"""AC-15 (P9-01): контракты — schema validation, backward compatibility, negative tests.

* CLI JSON Schema (execution-request/-result, normalized-event, probe-result, auth-check-result) и SSE-конверт:
  позитивные фикстуры получены от настоящих производителей (Fake CLI через процесс, `aicd_platform.sse`), каждая
  валидна; негативные — удаление каждого обязательного поля, неизвестное поле, нарушение ограничений
  (тайм-аут < 30 с, неизвестный провайдер, production_access, seq < 1, неизвестный тип SSE) — невалидны.
  ACP (12 типов) и AgentResult — tests/unit/test_acp_schema.py, tests/unit/test_agent_result.py.
* `oasdiff breaking` (образ и правила severity — как в CI, `.github/workflows/ci.yml`) против базовой версии
  контракта: git-ревизия `AICD_CONTRACT_BASELINE_REF` (по умолчанию `HEAD` — последний закоммиченный контракт;
  релизных тегов ещё нет, отчёт P9-01 указывает использованную базу). Без Docker/образа тест SKIPPED (≠ PASS).
* OpenAPI 3.1 валидна — tests/contract/test_contracts.py; Schemathesis (нет 5xx) — fuzz-тесты API.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_cli_adapters.models import result_json
from aicd_platform import sse
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from tests.support.cli import MemorySink, make_request
from tests.support.contract import ROOT

SCHEMAS = ROOT / "packages" / "contracts" / "schemas"
SPEC = "packages/contracts/openapi.yaml"
OASDIFF_IMAGE = "tufin/oasdiff:v1.35.0"  # = OASDIFF_IMAGE в .github/workflows/ci.yml
SEVERITY = "response-property-enum-value-added INFO\nresponse-property-became-not-enum INFO\n"


def _validator(rel: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / rel).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _errors(validator: Draft202012Validator, doc: Any) -> list[str]:
    return [e.message for e in validator.iter_errors(doc)]


@pytest.fixture(scope="module")
async def produced(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Документы контрактов от настоящих производителей (Fake CLI — отдельный процесс)."""
    root = tmp_path_factory.mktemp("ac15")
    wid = uuid7()
    (root / str(wid)).mkdir()
    adapter = FakeCLIExecutor(
        scenario="success_diff",
        runner=LocalProcessRunner(),
        artifact_sink=MemorySink(),
        worktree_path=worktree_resolver(root),
        env_source={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
    )
    request = make_request(worktree_id=wid)
    handle = await adapter.execute(request)
    events = [e async for e in adapter.stream_events(handle.execution_id)]
    result = await adapter.collect_result(handle.execution_id)
    probe = await adapter.probe(installation_id=uuid7())
    auth = await adapter.check_auth(installation_id=uuid7())
    now = datetime.now(UTC)
    run_id = uuid7()
    state = sse.envelope(
        {
            "event_id": uuid7(),
            "run_id": run_id,
            "seq": 1,
            "event_type": "run.state_changed",
            "occurred_at": now,
            "payload": {"from_status": "CREATED", "to_status": "DISCOVERY", "run_version": 1, "blocked_reason": None},
        }
    )
    heartbeat = json.loads(sse.heartbeat_frame(run_id, 1, now).split("data: ", 1)[1])
    return {
        "cli/1.0/execution-request.schema.json": json.loads(request.model_dump_json(exclude_none=True)),
        "cli/1.0/execution-result.schema.json": json.loads(result_json(result)),
        "cli/1.0/normalized-event.schema.json": [json.loads(e.model_dump_json(exclude_none=True)) for e in events],
        "cli/1.0/probe-result.schema.json": json.loads(probe.model_dump_json(exclude_none=True)),
        "cli/1.0/auth-check-result.schema.json": json.loads(auth.model_dump_json(exclude_none=True)),
        "events/1.0/sse-event.schema.json": [state, heartbeat],
    }


def _docs(produced: dict[str, Any], rel: str) -> list[dict[str, Any]]:
    value = produced[rel]
    return value if isinstance(value, list) else [value]


SCHEMA_FILES = [
    "cli/1.0/execution-request.schema.json",
    "cli/1.0/execution-result.schema.json",
    "cli/1.0/normalized-event.schema.json",
    "cli/1.0/probe-result.schema.json",
    "cli/1.0/auth-check-result.schema.json",
    "events/1.0/sse-event.schema.json",
]


@pytest.mark.parametrize("rel", SCHEMA_FILES)
def test_ac15_positive_fixtures_from_real_producers_are_valid(produced: dict[str, Any], rel: str) -> None:
    validator = _validator(rel)
    docs = _docs(produced, rel)
    assert docs
    for doc in docs:
        assert _errors(validator, doc) == [], (rel, doc)


@pytest.mark.parametrize("rel", SCHEMA_FILES)
def test_ac15_negative_fixtures_missing_required_and_unknown_fields_are_rejected(
    produced: dict[str, Any], rel: str
) -> None:
    validator = _validator(rel)
    schema: dict[str, Any] = dict(validator.schema)  # type: ignore[arg-type]
    required = schema["required"]
    assert required, "схема объявляет обязательные поля"
    for doc in _docs(produced, rel):
        for name in required:
            broken = {k: v for k, v in doc.items() if k != name}
            assert _errors(validator, broken), f"{rel}: без обязательного {name} документ принят"
        extra = {**doc, "unexpected_field": "x"}
        assert _errors(validator, extra), f"{rel}: неизвестное поле принято (additionalProperties)"
        assert _errors(validator, [doc]), f"{rel}: не-объект принят"


def _set(doc: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    cur = out
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value
    return out


NEGATIVE_CASES: list[tuple[str, tuple[str, ...], Any, str]] = [
    ("cli/1.0/execution-request.schema.json", ("limits", "timeout_seconds"), 10, "тайм-аут < 30 с"),
    ("cli/1.0/execution-request.schema.json", ("provider",), "openai", "провайдер вне {codex, claude}"),
    ("cli/1.0/execution-request.schema.json", ("permissions", "production_access"), True, "доступ к production"),
    ("cli/1.0/execution-request.schema.json", ("contract_version",), "2.0", "чужая версия контракта"),
    ("cli/1.0/execution-request.schema.json", ("execution_id",), "not-a-uuid", "id не UUID"),
    ("cli/1.0/execution-result.schema.json", ("exit_code",), "0", "exit_code строкой"),
    ("cli/1.0/execution-result.schema.json", ("execution_status",), "VERIFIED", "статус вне перечисления"),
    ("cli/1.0/normalized-event.schema.json", ("seq",), 0, "seq < 1"),
    ("cli/1.0/normalized-event.schema.json", ("type",), "thinking", "тип события вне перечисления (нет CoT)"),
    ("cli/1.0/probe-result.schema.json", ("installed",), "yes", "installed не boolean"),
    ("cli/1.0/auth-check-result.schema.json", ("auth_status",), "LOGGED_IN", "статус вне перечисления"),
    ("events/1.0/sse-event.schema.json", ("type",), "debug.dump", "неизвестный тип SSE"),
    ("events/1.0/sse-event.schema.json", ("seq",), 0, "seq < 1"),
    ("events/1.0/sse-event.schema.json", ("schema_version",), "2.0", "чужая версия схемы"),
    ("events/1.0/sse-event.schema.json", ("payload", "to_status"), "DONE", "статус run вне перечисления"),
]


@pytest.mark.parametrize(("rel", "path", "value", "why"), NEGATIVE_CASES, ids=[c[3] for c in NEGATIVE_CASES])
def test_ac15_negative_fixtures_constraint_violations_are_rejected(
    produced: dict[str, Any], rel: str, path: tuple[str, ...], value: Any, why: str
) -> None:
    validator = _validator(rel)
    doc = _docs(produced, rel)[0]
    assert _errors(validator, doc) == []
    assert _errors(validator, _set(doc, path, value)), f"{rel}: принято ({why})"


def _docker_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", image], check=False, capture_output=True)
    return probe.returncode == 0


@pytest.mark.docker
def test_ac15_oasdiff_no_breaking_changes_against_baseline(tmp_path: Path) -> None:
    if not _docker_image(OASDIFF_IMAGE):
        pytest.skip(f"Docker или образ {OASDIFF_IMAGE} недоступен — oasdiff не выполнен (≠ PASS)")
    ref = os.environ.get("AICD_CONTRACT_BASELINE_REF", "HEAD")
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{SPEC}"], check=False, capture_output=True)
    if shown.returncode != 0:
        pytest.skip(f"в ревизии {ref} нет {SPEC} — сравнивать не с чем")
    specs = tmp_path / f"oasdiff-{uuid.uuid4().hex[:8]}"
    specs.mkdir()
    (specs / "base.yaml").write_bytes(shown.stdout)
    shutil.copyfile(ROOT / SPEC, specs / "revision.yaml")
    (specs / "severity.txt").write_text(SEVERITY)
    for p in (specs, *specs.iterdir()):
        p.chmod(0o755 if p.is_dir() else 0o644)
    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{specs}:/specs:ro",
            OASDIFF_IMAGE,
            "breaking",
            "/specs/base.yaml",
            "/specs/revision.yaml",
            "--fail-on",
            "ERR",
            "--severity-levels",
            "/specs/severity.txt",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(f"oasdiff baseline={ref}\n{run.stdout}\n{run.stderr}")
    assert run.returncode == 0, f"oasdiff breaking (ERR) против {ref}:\n{run.stdout}\n{run.stderr}"
