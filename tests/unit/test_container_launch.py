"""Unit: одноразовый контейнер входа/статуса CLI над томом учётных данных (ADR-0010 п.3, cli-auth.md §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aicd_cli_adapters.container_launch import LauncherError, credential_launcher, credential_volume
from aicd_cli_adapters.real.adapter import cli_container_layout
from aicd_security.container_policy import build_host_config, lint_container_config
from aicd_security.sandbox import ContainerSpec, Mount

ARGS = {
    "docker": "/usr/bin/docker",
    "image": "aicd/cli-runner@sha256:" + "a" * 64,
    "volume": "aicd-cred-claude",
    "network": "aicd-cli-egress",
    "user": "10001:10001",
    "purpose": "login",
    "interactive": True,
}


def launch(**overrides: object) -> tuple[str, ...]:
    return credential_launcher(**{**ARGS, **overrides}).argv  # type: ignore[arg-type]


def test_launcher_is_hardened_and_mounts_only_the_credential_volume() -> None:
    argv = launch()
    joined = " ".join(argv)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges:true", "--read-only", "--pids-limit 256"):
        assert flag in joined
    assert "--user 10001:10001" in joined and "--network aicd-cli-egress" in joined
    # Единственное монтирование — том учётных данных в домашнем каталоге; worktree и сокет Docker не попадают.
    assert argv.count("--mount") == 1 and "-v" not in argv and "--volume" not in argv
    assert "type=volume,source=aicd-cred-claude,target=/home/agent,volume-nocopy" in argv
    assert "docker.sock" not in joined and "--privileged" not in argv
    assert argv[-1] == ARGS["image"]  # дальше идёт только путь CLI и его аргументы из allowlist профиля


def test_tty_only_for_interactive_login() -> None:
    assert "--tty" in launch(interactive=True) and "--interactive" in launch(interactive=True)
    status = launch(interactive=False, purpose="status")
    assert "--tty" not in status and "--interactive" not in status


@pytest.mark.parametrize(
    "bad",
    [
        {"volume": "my-volume"},  # не том учётных данных
        {"volume": "aicd-cred-claude,target=/etc"},  # инъекция в --mount
        {"network": "host"},
        {"network": "bridge"},
        {"user": "0:0"},  # root
        {"user": "root"},
        {"image": "--privileged"},
        {"purpose": "x y"},
    ],
)
def test_unsafe_parameters_are_rejected(bad: dict[str, str]) -> None:
    with pytest.raises(LauncherError):
        launch(**bad)


def test_container_names_are_unique_and_cleanup_targets_only_that_container() -> None:
    a, b = credential_launcher(**ARGS), credential_launcher(**ARGS)  # type: ignore[arg-type]
    assert a.container_name != b.container_name
    assert a.cleanup_argv() == ("/usr/bin/docker", "rm", "--force", a.container_name)


@pytest.mark.parametrize("worktree_ro", [False, True])
def test_runner_with_credential_volume_satisfies_container_policy(worktree_ro: bool) -> None:
    """Том — не rw-bind: единственным rw-bind остаётся worktree (или `.aicd` при ro-worktree)."""
    layout = cli_container_layout(
        "claude", "aicd/cli-runner:1", credential_volume=credential_volume("claude"), network="aicd-cli-egress"
    )
    assert layout.extra_mounts == (Mount(Path("aicd-cred-claude"), "/home/agent", read_only=False),)
    io = (Mount(Path("/var/lib/aicd/io/aicd-io-1/.aicd"), "/workspace/.aicd", read_only=False),) if worktree_ro else ()
    spec = ContainerSpec(
        image=layout.image,
        argv=(layout.binary, "-p"),
        workdir="/workspace",
        env={"HOME": "/home/agent"},
        mounts=(
            Mount(Path("/srv/aicd/worktrees/t1"), "/workspace", read_only=worktree_ro),
            *io,
            Mount(Path("/var/lib/aicd/io/aicd-prompt-1"), "/aicd/input", read_only=True),
            *layout.extra_mounts,
        ),
        network=layout.network,
        labels={"aicd.execution_id": "e1"},
    )
    assert (
        lint_container_config(build_host_config(spec), allowed_bind_roots=("/srv/aicd/worktrees", "/var/lib/aicd/io"))
        == []
    )


def test_host_directory_credentials_remain_rejected_by_policy() -> None:
    """Bind каталога хоста на запись рядом с rw-worktree политика отвергает — поэтому нужен том."""
    layout = cli_container_layout("claude", "aicd/cli-runner:1", credential_dir=Path("/home/u/.claude"))
    spec = ContainerSpec(
        image=layout.image,
        argv=(layout.binary,),
        workdir="/workspace",
        mounts=(Mount(Path("/srv/aicd/worktrees/t1"), "/workspace", read_only=False), *layout.extra_mounts),
        network="aicd-cli-egress",
        labels={"aicd.execution_id": "e1"},
    )
    errors = lint_container_config(
        build_host_config(spec), allowed_bind_roots=("/srv/aicd/worktrees", "/home/u/.claude")
    )
    assert any("single read-write bind" in e for e in errors)
