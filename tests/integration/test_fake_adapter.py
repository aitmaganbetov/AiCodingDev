"""P2-04/P2-10: Fake-адаптер через настоящий процесс — все исходы контракта, без подписок CLI."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor, ScenarioSpec
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_cli_adapters.models import ExecutionResult
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from tests.support.cli import MemorySink, make_request
from tests.support.contract import ROOT

SCHEMAS = ROOT / "packages" / "contracts" / "schemas" / "cli" / "1.0"


def _setup(tmp_path: Path, scenario: ScenarioSpec, **kw: object) -> tuple[FakeCLIExecutor, MemorySink, uuid.UUID]:
    wid = uuid7()
    (tmp_path / str(wid)).mkdir()
    sink = MemorySink()
    adapter = FakeCLIExecutor(
        scenario=scenario,
        runner=LocalProcessRunner(),
        artifact_sink=sink,
        worktree_path=worktree_resolver(tmp_path),
        env_source={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
        **kw,  # type: ignore[arg-type]
    )
    return adapter, sink, wid


async def _run(adapter: FakeCLIExecutor, request: object) -> tuple[ExecutionResult, list[Any]]:
    handle = await adapter.execute(request)  # type: ignore[arg-type]
    events = [e async for e in adapter.stream_events(handle.execution_id)]
    return await adapter.collect_result(handle.execution_id), events


def _validate_against_schema(result: ExecutionResult) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    from aicd_cli_adapters.models import result_json

    schema = json.loads((SCHEMAS / "execution-result.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(json.loads(result_json(result)))


async def test_success_claims_are_unverified(tmp_path: Path) -> None:
    adapter, sink, wid = _setup(tmp_path, "success_diff")
    result, events = await _run(adapter, make_request(worktree_id=wid))
    _validate_against_schema(result)
    assert result.status == "completed" and result.execution_status == "COLLECTED" and result.exit_code == 0
    assert result.verification.status == "pending", "exit 0 ≠ verified (ТЗ §9.3, AC-11)"
    assert [c.type for c in result.claims] == ["implementation_completed"]
    assert all(not c.evidence_refs for c in result.claims), "claims агента без доказательств платформы"
    assert result.usage.tokens is None and result.usage.source == "not_exposed_by_cli"
    assert (tmp_path / str(wid) / "FAKE_CHANGE.md").exists()
    kinds = [e.type for e in events]
    assert kinds[0] == "started" and kinds[-1] == "finished" and "tool_event" in kinds
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert sink.by_kind("cli_raw_log") and next(a for a in result.artifacts if a.type == "raw_log").protected


async def test_usage_only_when_reported(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "usage_reported")
    result, _ = await _run(adapter, make_request(worktree_id=wid))
    assert result.usage.source == "cli_reported" and result.usage.tokens == 150


@pytest.mark.parametrize(
    ("scenario", "exec_status", "code", "retryable"),
    [
        ("nonzero_exit", "COLLECTED", "PROCESS_FAILED", True),
        ("auth_required", "AUTH_REQUIRED", "AUTH_REQUIRED", False),
        ("rate_limited", "RATE_LIMITED", "RATE_LIMITED", True),
        ("garbage_output", "COLLECTED", "OUTPUT_PARSE_ERROR", True),
    ],
)
async def test_errors_are_normalized(
    tmp_path: Path, scenario: str, exec_status: str, code: str, retryable: bool
) -> None:
    adapter, _, wid = _setup(tmp_path, scenario)
    result, _ = await _run(adapter, make_request(worktree_id=wid))
    _validate_against_schema(result)
    assert result.status == "failed" and result.execution_status == exec_status
    assert result.error is not None and result.error.code == code and result.error.retryable is retryable
    assert not result.claims
    if code == "RATE_LIMITED":
        assert result.error.retry_after_seconds == 120


async def test_wall_clock_timeout(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "timeout")
    adapter._grace = 0.5
    request = make_request(worktree_id=wid, small_limits={"timeout_seconds": 1})
    result, _ = await _run(adapter, request)
    assert result.execution_status == "TIMED_OUT" and result.error and result.error.code == "TIMEOUT"


async def test_idle_timeout_while_wall_clock_remains(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "timeout")
    adapter._grace = 0.5
    request = make_request(worktree_id=wid, small_limits={"timeout_seconds": 60, "idle_timeout_seconds": 1})
    result, _ = await _run(adapter, request)
    assert result.execution_status == "TIMED_OUT"


async def test_chatty_process_not_idle_but_wall_clock_enforced(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "chatty_hang")
    adapter._grace = 0.5
    request = make_request(worktree_id=wid, small_limits={"timeout_seconds": 2, "idle_timeout_seconds": 1})
    result, events = await _run(adapter, request)
    assert result.execution_status == "TIMED_OUT" and len(events) > 5


async def test_cancel_is_idempotent(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "ignore_sigterm")
    adapter._grace = 0.5
    request = make_request(worktree_id=wid)
    handle = await adapter.execute(request)
    async for event in adapter.stream_events(handle.execution_id):
        if event.type == "output":  # процесс уже установил обработчик SIGTERM
            break
    await adapter.cancel(handle.execution_id, reason="user")
    await adapter.cancel(handle.execution_id, reason="user")
    result = await adapter.collect_result(handle.execution_id)
    assert result.status == "cancelled" and result.execution_status == "CANCELLED" and result.signal == "SIGKILL"


async def test_execute_is_idempotent(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "success_diff")
    request = make_request(worktree_id=wid)
    h1 = await adapter.execute(request)
    h2 = await adapter.execute(request)
    assert h1.execution_id == h2.execution_id
    await adapter.collect_result(h1.execution_id)
    assert len(adapter._executions) == 1


async def test_secrets_redacted_in_events_and_raw_log(tmp_path: Path) -> None:
    adapter, sink, wid = _setup(tmp_path, "secret_leak")
    result, events = await _run(adapter, make_request(worktree_id=wid))
    text = " ".join(str(e.payload) for e in events)
    raw = b"".join(sink.by_kind("cli_raw_log"))
    for secret in ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter2hunter2", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in text and secret.encode() not in raw
    assert any(e.payload.get("redactions") for e in events if e.type == "output")
    assert result.status == "completed"


async def test_output_limit_truncates_without_killing(tmp_path: Path) -> None:
    adapter, sink, wid = _setup(tmp_path, "big_output")
    request = make_request(worktree_id=wid, small_limits={"max_output_bytes": 100_000})
    result, events = await _run(adapter, request)
    assert result.truncated is True and result.status == "completed"
    assert len(sink.by_kind("cli_raw_log")[0]) <= 100_000
    assert any(e.type == "warning" and e.payload["code"] == "OUTPUT_TRUNCATED" for e in events)


async def test_process_environment_is_allowlisted(tmp_path: Path) -> None:
    adapter, _, wid = _setup(tmp_path, "env_dump")
    _, events = await _run(adapter, make_request(worktree_id=wid))
    names = next(e.payload["text"] for e in events if e.type == "output" and "PATH" in e.payload["text"])
    assert "PATH" in names and "AICD" not in names and "HOME" not in names


async def test_missing_worktree_is_crash_not_success(tmp_path: Path) -> None:
    adapter, _, _ = _setup(tmp_path, "success_diff")
    result, _ = await _run(adapter, make_request(worktree_id=uuid7()))
    assert result.execution_status == "CRASHED" and result.status == "failed"


async def test_probe_and_auth_match_contract(tmp_path: Path) -> None:
    adapter, _, _ = _setup(tmp_path, "success_diff", auth_status="AUTH_REQUIRED")
    probe = await adapter.probe(installation_id=uuid7())
    assert probe.compatibility == "UNTESTED" and probe.capabilities.noninteractive_mode
    auth = await adapter.check_auth(installation_id=uuid7())
    assert auth.auth_status == "AUTH_REQUIRED"


async def test_agent_io_dir_outside_worktree_and_prompt_text(tmp_path: Path) -> None:
    """Phase 4: `.aicd/`, `output/` ведут в каталог вывода исполнения; промпт роли — файл `{prompt_text_file}`."""
    io_root = tmp_path / "io"
    adapter, _, wid = _setup(tmp_path, {"reviewer": "agent_review_approve"}, agent_io=True, agent_io_root=io_root)
    base = make_request(worktree_id=wid)
    data = base.model_dump(mode="json", exclude_unset=True)
    data["agent_role"] = "reviewer"
    data["permissions"]["write_scope"] = "none"
    data["input"]["instruction_template_version"] = "reviewer@1.0.0"
    request = type(base).model_validate(data)
    adapter.stage_prompt(request.execution_id, "base_commit: " + "c" * 40 + "\n")
    result, _ = await _run(adapter, request)
    assert result.status == "completed", result.error
    worktree = tmp_path / str(wid)
    assert not (worktree / ".aicd").exists() and not (worktree / "output").exists(), "worktree не тронут"
    io_dir = adapter.agent_io_dir(request.execution_id)
    assert io_dir is not None and io_dir.parent == io_root
    doc = json.loads((io_dir / ".aicd" / "result.json").read_text())
    assert doc["acp_messages"][-1]["payload"]["review_verdict"] == "approved"
    assert (io_dir / ".aicd" / "output" / "review.md").is_file()
    adapter.release_agent_io(request.execution_id)
    assert not io_dir.exists() and adapter.agent_io_dir(request.execution_id) is None
