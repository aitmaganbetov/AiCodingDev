"""P2-03: профили команд, сигнатуры ошибок, compatibility matrix."""

import re
from pathlib import Path

import pytest

from aicd_cli_adapters import compat
from aicd_cli_adapters.errors import ErrorSignature, classify_output
from aicd_cli_adapters.profiles import CommandProfile, ProfileError, build_argv, build_env

PROFILE = CommandProfile(
    id="t",
    provider="codex",
    argv_template=("{binary}", "run", "{prompt_file}"),
    allowed_placeholders=frozenset({"binary", "prompt_file"}),
)


def test_argv_is_array_from_allowlisted_placeholders() -> None:
    argv = build_argv(PROFILE, {"binary": "/usr/bin/cli", "prompt_file": "/tmp/p; rm -rf /"})  # noqa: S108
    assert argv == ("/usr/bin/cli", "run", "/tmp/p; rm -rf /")  # значение — один аргумент, не shell  # noqa: S108


@pytest.mark.parametrize(
    ("template", "values"),
    [
        (("{binary}", "{evil}"), {"binary": "b", "evil": "x"}),
        (("{binary}", "--x={prompt_file}"), {"binary": "b", "prompt_file": "p"}),
        (("{binary}", "{prompt_file}"), {"binary": "b"}),
        (("{binary}", "{prompt_file}"), {"binary": "b", "prompt_file": "a\nb"}),
        (("{binary}", "<candidate-noninteractive-subcommand>"), {"binary": "b"}),
    ],
)
def test_argv_rejections(template: tuple[str, ...], values: dict[str, str]) -> None:
    profile = CommandProfile(
        id="t", provider="codex", argv_template=template, allowed_placeholders=frozenset({"binary", "prompt_file"})
    )
    with pytest.raises(ProfileError):
        build_argv(profile, values)


def test_env_built_from_allowlist_and_api_keys_refused() -> None:
    assert build_env(PROFILE, {"PATH": "/bin", "HOME": "/h", "EXTRA": "x"}) == {"PATH": "/bin", "HOME": "/h"}
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "MY_SECRET"):
        with pytest.raises(ProfileError):
            build_env(PROFILE, {"PATH": "/bin", key: "x"})


def test_signature_priority_and_retry_after() -> None:
    sigs = (
        ErrorSignature("RATE_LIMITED", re.compile(r"limit.*after (?P<ra>\d+)"), "ra"),
        ErrorSignature("AUTH_REQUIRED", re.compile(r"login required")),
    )
    c = classify_output("login required; limit, retry after 30", sigs)
    assert c is not None and c.code == "AUTH_REQUIRED"
    c = classify_output("usage limit, retry after 30", sigs)
    assert c is not None and c.code == "RATE_LIMITED" and c.retry_after_seconds == 30
    assert classify_output("all good", sigs) is None


def test_compat_matrix(tmp_path: Path) -> None:
    assert compat.load_matrix() == ()  # реальные версии не подтверждены до P2-01
    assert compat.evaluate("codex", "codex-cli 1.2.3") == ("UNTESTED", None)
    m = tmp_path / "m.yaml"
    m.write_text(
        "entries:\n  - {id: c1, provider: codex, version_range: '>=1.0.0,<2.0.0', status: SUPPORTED}\n"
        "  - {id: c0, provider: codex, version_range: '<1.0.0', status: UNSUPPORTED}\n"
    )
    entries = compat.load_matrix(m)
    assert compat.evaluate("codex", "1.5.0", entries)[0] == "SUPPORTED"
    assert compat.evaluate("codex", "0.9.1", entries)[0] == "UNSUPPORTED"
    assert compat.evaluate("claude", "1.5.0", entries)[0] == "UNTESTED"
    assert compat.evaluate("codex", "garbage", entries)[0] == "UNTESTED"
