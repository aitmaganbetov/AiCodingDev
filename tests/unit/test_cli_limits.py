"""Лимиты подписки CLI: только то, что сообщил сам CLI, и только числа (`aicd_cli_adapters.limits`).

Событие в фикстуре снято с установленного Claude Code 2.1.319 (`--output-format stream-json --verbose`).
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aicd_cli_adapters import limits
from aicd_cli_adapters.profiles import ProfileError
from aicd_cli_adapters.real.parsers import ClaudeStreamParser
from aicd_platform.ids import uuid7

NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
EVENT: dict[str, Any] = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1794388800,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.24, "resetsAt": 1794388800, "status": "allowed"},
            "seven_day": {"utilization": 0.3, "resetsAt": 1794783600, "status": "allowed"},
        },
    },
}


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def set(self, name: str, value: str, *, ex: int | None = None) -> bool:
        self.data[name], self.ttl[name] = value, ex
        return True

    async def get(self, name: str) -> str | None:
        return self.data.get(name)


def test_windows_are_taken_as_the_cli_reports_them_and_nothing_else() -> None:
    observed = limits.from_claude_event(EVENT, now=NOW)
    assert observed == {
        "provider": "claude",
        "observed_at": NOW.isoformat(),
        "windows": [
            {"kind": "five_hour", "used": 0.24, "resets_at": "2026-11-11T09:20:00+00:00", "status": "allowed"},
            {"kind": "seven_day", "used": 0.3, "resets_at": "2026-11-15T23:00:00+00:00", "status": "allowed"},
        ],
        "credits": None,
    }
    # Настройки перерасхода организации и прочее в наблюдение не попадают.
    assert "org_level_disabled" not in json.dumps(observed) and "overage" not in json.dumps(observed)


@pytest.mark.parametrize(
    "event",
    [
        None,
        {"type": "system"},
        {"type": "rate_limit_event"},
        {"type": "rate_limit_event", "rate_limit": {"status": "allowed"}},  # без окон показывать нечего
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "24%"}}}},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"Bad Kind!": {"utilization": 0.1}}}},
    ],
)
def test_events_without_usable_windows_give_no_observation(event: object) -> None:
    assert limits.from_claude_event(event, now=NOW) is None


def test_out_of_range_values_are_clamped_and_bad_reset_time_is_dropped() -> None:
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": 1.7, "resetsAt": "soon", "status": "<script>"},
                "seven_day": {"utilization": -3, "resetsAt": 1794783600},
            }
        },
    }
    observed = limits.from_claude_event(event, now=NOW)
    assert observed is not None
    assert observed["windows"][0] == {"kind": "five_hour", "used": 1.0, "resets_at": None, "status": None}
    assert observed["windows"][1]["used"] == 0.0


def test_parser_remembers_the_limits_event_and_keeps_it_out_of_the_feed() -> None:
    parser = ClaudeStreamParser()
    assert parser.rate_limit_event is None
    parsed = parser.parse(json.dumps(EVENT))
    assert parsed.kind == "message" and not parsed.data.get("text"), "событие скрыто: это не вывод агента"
    assert limits.from_claude_event(parser.rate_limit_event, now=NOW) is not None


async def test_observation_round_trips_per_installation_and_environment() -> None:
    redis = FakeRedis()
    installation, other = uuid7(), uuid7()
    observed = limits.from_claude_event(EVENT, now=NOW)
    assert observed is not None
    await limits.save(redis, environment="test", installation_id=installation, limits=observed)
    assert await limits.load(redis, environment="test", installation_id=installation) == observed
    assert await limits.load(redis, environment="test", installation_id=other) is None
    assert await limits.load(redis, environment="production", installation_id=installation) is None
    assert redis.ttl[limits.key("test", installation)] == limits.TTL_SECONDS


@pytest.mark.parametrize("damaged", ["not json", "[]", '{"windows": "x", "observed_at": "t"}', '{"windows": []}'])
async def test_damaged_record_reads_as_absent(damaged: str) -> None:
    redis = FakeRedis()
    installation = uuid7()
    redis.data[limits.key("test", installation)] = damaged
    assert await limits.load(redis, environment="test", installation_id=installation) is None


def _stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_reading_asks_the_cli_once_and_stops_it_after_the_limits_event(tmp_path: Path) -> None:
    """Событие с лимитами печатается до ответа модели: дальше процесс не нужен и снимается."""
    marker = tmp_path / "finished"
    binary = _stub(
        tmp_path,
        f"""
echo "$@" > {tmp_path}/argv
echo '{{"type":"system","subtype":"init"}}'
echo 'not json at all'
echo '{json.dumps(EVENT)}'
sleep 30
touch {marker}
""",
    )
    observed = await limits.read_limits(
        provider="claude", binary_path=binary, env_source={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, now=NOW
    )
    assert observed is not None and [w["kind"] for w in observed["windows"]] == ["five_hour", "seven_day"]
    assert not marker.exists(), "CLI снят сразу после события с лимитами"
    assert (tmp_path / "argv").read_text().split()[:4] == ["--print", "--output-format", "stream-json", "--verbose"]


async def test_reading_gives_nothing_for_a_cli_that_does_not_report_limits(tmp_path: Path) -> None:
    silent = _stub(tmp_path, 'echo \'{"type":"result"}\'\n')
    env = {"PATH": "/usr/bin:/bin"}
    assert await limits.read_limits(provider="claude", binary_path=silent, env_source=env, now=NOW) is None
    assert await limits.read_limits(provider="other", binary_path="/nonexistent", env_source=env, now=NOW) is None


async def test_provider_api_keys_in_environment_block_the_read(tmp_path: Path) -> None:
    """Вход по API-ключу платформой запрещён (ТЗ §1): с ключом в окружении CLI не запускается."""
    binary = _stub(tmp_path, "echo '{}'\n")
    with pytest.raises(ProfileError):
        await limits.read_limits(
            provider="claude", binary_path=binary, env_source={"PATH": "/bin", "ANTHROPIC_API_KEY": "x"}, now=NOW
        )


# --- Codex: лимиты сообщает `codex app-server` (ответ снят с установленного codex-cli 0.167.0) -------------

CODEX_RESULT: dict[str, Any] = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1794976051},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "plus",
    },
    "rateLimitResetCredits": {"availableCount": 2, "credits": [{"id": "RateLimitResetCredit_secret"}]},
    "accountId": "8a756412-0000-0000-0000-000000000000",
}


def test_codex_windows_and_credits_without_account_data() -> None:
    observed = limits.from_codex_rate_limits(CODEX_RESULT, now=NOW)
    assert observed == {
        "provider": "codex",
        "observed_at": NOW.isoformat(),
        "windows": [{"kind": "seven_day", "used": 0.04, "resets_at": "2026-11-18T04:27:31+00:00", "status": None}],
        "credits": {"balance": "0", "unlimited": False},
    }
    dumped = json.dumps(observed)
    for private in ("8a756412", "RateLimitResetCredit", "plus"):
        assert private not in dumped, "идентификатор аккаунта, купоны и план в наблюдение не попадают"


def test_codex_window_kinds_follow_the_duration_and_bad_values_are_dropped() -> None:
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 150, "windowDurationMins": 300, "resetsAt": None},
            "secondary": {"usedPercent": 10, "windowDurationMins": 43200},
            "credits": {"balance": "12; DROP", "unlimited": True},
        }
    }
    observed = limits.from_codex_rate_limits(result, now=NOW)
    assert observed is not None
    assert [(w["kind"], w["used"]) for w in observed["windows"]] == [("five_hour", 1.0), ("window_43200m", 0.1)]
    assert observed["credits"] == {"balance": None, "unlimited": True}
    for empty in (None, {}, {"rateLimits": None}, {"rateLimits": {"primary": {"usedPercent": "x"}}}):
        assert limits.from_codex_rate_limits(empty, now=NOW) is None


async def test_codex_is_asked_over_app_server_without_any_model_request(tmp_path: Path) -> None:
    """Протокол: initialize → initialized → account/rateLimits/read; ответ с id запроса — наблюдение."""
    received = tmp_path / "received"
    reply = json.dumps({"id": 2, "result": CODEX_RESULT})
    binary = _stub(
        tmp_path,
        f"""
echo "$@" > {tmp_path}/argv
echo '{{"id":1,"result":{{"userAgent":"x"}}}}'
echo '{{"method":"account/updated","params":{{}}}}'
head -n 3 > {received}
echo '{reply}'
sleep 30
""",
    )
    observed = await limits.read_limits(
        provider="codex", binary_path=binary, env_source={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, now=NOW
    )
    assert observed is not None and observed["windows"][0]["used"] == 0.04
    assert (tmp_path / "argv").read_text().strip() == "app-server"
    methods = [json.loads(line).get("method") for line in received.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]


async def test_credits_survive_the_round_trip() -> None:
    redis = FakeRedis()
    installation = uuid7()
    observed = limits.from_codex_rate_limits(CODEX_RESULT, now=NOW)
    assert observed is not None
    await limits.save(redis, environment="test", installation_id=installation, limits=observed)
    assert await limits.load(redis, environment="test", installation_id=installation) == observed
