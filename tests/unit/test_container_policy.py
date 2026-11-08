"""Unit: hardening-спецификация контейнера и линтер (SC-SBX-01…05, ST-SBX-05)."""

from __future__ import annotations

import copy
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aicd_security.container_policy import (
    CONTAINER_TMP,
    ContainerPolicyError,
    build_host_config,
    check_container_config,
    lint_container_config,
)
from aicd_security.docker_runner import DockerFrameParser, DockerSandboxRunner
from aicd_security.sandbox import ContainerSpec, Mount, OutputChunk, ResourceLimits

ROOT = "/srv/aicd/worktrees"


def make_spec(**overrides: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "image": "aicd-runner-test@sha256:" + "a" * 64,
        "argv": ("codex", "exec", "--json"),
        "workdir": "/workspace",
        "env": {"HOME": "/home/runner", "LANG": "C.UTF-8"},
        "mounts": (
            Mount(Path(f"{ROOT}/ws-1"), "/workspace", read_only=False),
            Mount(Path("/srv/aicd/context-packs/cp-1"), "/context", read_only=True),
            Mount(Path("aicd-cred-codex"), "/home/runner/.codex", read_only=False),
        ),
        "limits": ResourceLimits(cpus=2.0, memory_mb=4096, pids=512, timeout_seconds=1800),
        "network": "aicd-sandbox",
        "labels": {"aicd.execution_id": "exe_1", "aicd.workspace_id": "ws_1"},
    }
    base.update(overrides)
    return ContainerSpec(**base)


@pytest.fixture
def payload() -> dict[str, Any]:
    return build_host_config(make_spec())


def test_build_host_config_hardening(payload: dict[str, Any]) -> None:
    hc = payload["HostConfig"]
    assert payload["User"] == "10001:10001"
    assert payload["Entrypoint"] == ["codex"] and payload["Cmd"] == ["exec", "--json"]
    assert payload["Tty"] is False and payload["OpenStdin"] is False
    assert payload["Labels"]["aicd.execution_id"] == "exe_1"
    assert payload["Labels"]["aicd.kind"] == "runner"
    assert hc["Privileged"] is False
    assert hc["CapDrop"] == ["ALL"] and "CapAdd" not in hc
    assert hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["ReadonlyRootfs"] is True and hc["Init"] is True
    assert hc["Tmpfs"][CONTAINER_TMP].startswith("rw,noexec,nosuid,nodev,size=")
    assert hc["Tmpfs"]["/home/runner"] == "rw,nosuid,nodev,size=256m"
    assert hc["Memory"] == hc["MemorySwap"] == 4096 * 1024 * 1024
    assert hc["NanoCpus"] == 2_000_000_000 and hc["PidsLimit"] == 512
    assert hc["NetworkMode"] == "aicd-sandbox"
    assert {"Name": "nofile", "Soft": 4096, "Hard": 4096} in hc["Ulimits"]
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "Binds", "Devices"):
        assert key not in hc
    kinds = [(m["Type"], m["Target"], m["ReadOnly"]) for m in hc["Mounts"]]
    assert kinds == [
        ("bind", "/workspace", False),
        ("bind", "/context", True),
        ("volume", "/home/runner/.codex", False),
    ]
    assert lint_container_config(payload) == []
    assert lint_container_config(payload, require_image_digest=True) == []


def test_build_rejects_invalid_specs() -> None:
    with pytest.raises(ContainerPolicyError):
        build_host_config(make_spec(argv=()))
    with pytest.raises(ContainerPolicyError):
        build_host_config(make_spec(env={"BAD KEY": "x"}))
    with pytest.raises(ContainerPolicyError):
        build_host_config(make_spec(mounts=(Mount(Path("../etc"), "/x"),)))
    with pytest.raises(ContainerPolicyError):
        build_host_config(make_spec(workdir="relative"))


def test_network_none_disables_networking() -> None:
    p = build_host_config(make_spec(network="none"))
    assert p["NetworkDisabled"] is True and p["HostConfig"]["NetworkMode"] == "none"
    assert lint_container_config(p) == []


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(p: dict[str, Any]) -> None:
        target = p
        *parents, leaf = path.split(".")
        for key in parents:
            target = target[key]
        target[leaf] = value

    return apply


def _del(path: str) -> Callable[[dict[str, Any]], None]:
    def apply(p: dict[str, Any]) -> None:
        target = p
        *parents, leaf = path.split(".")
        for key in parents:
            target = target[key]
        target.pop(leaf, None)

    return apply


def _add_mount(mount: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    return lambda p: p["HostConfig"]["Mounts"].append(mount)


FORBIDDEN: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("privileged", _set("HostConfig.Privileged", True), "Privileged"),
    ("cap_add", _set("HostConfig.CapAdd", ["SYS_ADMIN"]), "CapAdd"),
    ("cap_drop_missing", _set("HostConfig.CapDrop", []), "CapDrop"),
    ("no_new_privs_missing", _set("HostConfig.SecurityOpt", []), "no-new-privileges"),
    ("seccomp_unconfined", _set("HostConfig.SecurityOpt", ["no-new-privileges:true", "seccomp=unconfined"]), "weakens"),
    (
        "apparmor_unconfined",
        _set("HostConfig.SecurityOpt", ["no-new-privileges:true", "apparmor=unconfined"]),
        "weakens",
    ),
    ("label_disable", _set("HostConfig.SecurityOpt", ["no-new-privileges:true", "label=disable"]), "weakens"),
    ("rootfs_rw", _set("HostConfig.ReadonlyRootfs", False), "ReadonlyRootfs"),
    ("no_init", _set("HostConfig.Init", False), "Init"),
    ("host_network", _set("HostConfig.NetworkMode", "host"), "NetworkMode"),
    ("default_bridge", _set("HostConfig.NetworkMode", "bridge"), "NetworkMode"),
    ("container_network", _set("HostConfig.NetworkMode", "container:abc"), "NetworkMode"),
    ("host_pid", _set("HostConfig.PidMode", "host"), "PidMode"),
    ("host_ipc", _set("HostConfig.IpcMode", "host"), "IpcMode"),
    ("host_uts", _set("HostConfig.UTSMode", "host"), "UTSMode"),
    ("host_userns", _set("HostConfig.UsernsMode", "host"), "UsernsMode"),
    ("host_cgroupns", _set("HostConfig.CgroupnsMode", "host"), "CgroupnsMode"),
    ("devices", _set("HostConfig.Devices", [{"PathOnHost": "/dev/kvm"}]), "Devices"),
    ("gpus", _set("HostConfig.DeviceRequests", [{"Count": -1}]), "DeviceRequests"),
    ("binds", _set("HostConfig.Binds", ["/:/host"]), "Binds"),
    ("volumes_from", _set("HostConfig.VolumesFrom", ["other"]), "VolumesFrom"),
    ("sysctls", _set("HostConfig.Sysctls", {"net.ipv4.ip_forward": "1"}), "Sysctls"),
    ("unmask_proc", _set("HostConfig.MaskedPaths", []), "MaskedPaths"),
    ("ports", _set("HostConfig.PortBindings", {"22/tcp": [{"HostPort": "22"}]}), "PortBindings"),
    ("publish_all", _set("HostConfig.PublishAllPorts", True), "PublishAllPorts"),
    ("runtime", _set("HostConfig.Runtime", "nvidia"), "Runtime"),
    ("root_user", _set("User", "0:0"), "non-root"),
    ("root_user_name", _set("User", "root"), "non-root"),
    ("empty_user", _set("User", ""), "non-root"),
    ("tty", _set("Tty", True), "Tty"),
    ("no_execution_label", _set("Labels", {}), "aicd.execution_id"),
    ("no_memory", _del("HostConfig.Memory"), "Memory"),
    ("swap", _set("HostConfig.MemorySwap", -1), "MemorySwap"),
    ("no_pids", _set("HostConfig.PidsLimit", 0), "PidsLimit"),
    ("no_cpus", _del("HostConfig.NanoCpus"), "NanoCpus"),
    ("oom_kill_disable", _set("HostConfig.OomKillDisable", True), "OomKillDisable"),
    ("tmp_exec", _set("HostConfig.Tmpfs", {CONTAINER_TMP: "rw,nosuid,nodev,size=64m"}), "noexec"),
    ("tmpfs_unbounded", _set("HostConfig.Tmpfs", {CONTAINER_TMP: "rw,noexec,nosuid,nodev"}), "size-limited"),
    (
        "docker_sock",
        _add_mount(
            {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/var/run/docker.sock", "ReadOnly": True}
        ),
        "sockets",
    ),
    (
        "docker_sock_in_root",
        _add_mount({"Type": "bind", "Source": f"{ROOT}/ws-1/docker.sock", "Target": "/x.sock", "ReadOnly": True}),
        "sockets",
    ),
    (
        "bind_etc",
        _add_mount({"Type": "bind", "Source": "/etc", "Target": "/hostetc", "ReadOnly": True}),
        "outside allowed roots",
    ),
    (
        "bind_traversal",
        _add_mount({"Type": "bind", "Source": f"{ROOT}/../../etc", "Target": "/e", "ReadOnly": True}),
        "outside allowed roots",
    ),
    (
        "bind_root_itself",
        _add_mount({"Type": "bind", "Source": ROOT, "Target": "/all", "ReadOnly": True}),
        "outside allowed roots",
    ),
    (
        "second_rw_bind",
        _add_mount({"Type": "bind", "Source": f"{ROOT}/ws-2", "Target": "/other", "ReadOnly": False}),
        "single read-write",
    ),
    (
        "rw_scratch_volume",
        _add_mount({"Type": "volume", "Source": "aicd-scratch-1", "Target": "/scratch", "ReadOnly": False}),
        "credential volumes",
    ),
    (
        "foreign_volume",
        _add_mount({"Type": "volume", "Source": "pgdata", "Target": "/pg", "ReadOnly": True}),
        "allowlist prefixes",
    ),
    ("tmpfs_mount_type", _add_mount({"Type": "tmpfs", "Target": "/x"}), "forbidden"),
    ("npipe", _add_mount({"Type": "npipe", "Source": "x", "Target": "/x"}), "forbidden"),
    (
        "shared_propagation",
        _add_mount(
            {
                "Type": "bind",
                "Source": f"{ROOT}/ws-3",
                "Target": "/p",
                "ReadOnly": True,
                "BindOptions": {"Propagation": "rshared"},
            }
        ),
        "propagation",
    ),
    (
        "proc_target",
        _add_mount({"Type": "bind", "Source": f"{ROOT}/ws-4", "Target": "/proc", "ReadOnly": True}),
        "system path",
    ),
    ("anon_volumes", _set("Volumes", {"/data": {}}), "Volumes"),
    ("no_hostconfig", _del("HostConfig"), "HostConfig"),
]


@pytest.mark.parametrize(("mutate", "expected"), [pytest.param(m, e, id=i) for i, m, e in FORBIDDEN])
def test_linter_rejects_forbidden_config(
    payload: dict[str, Any], mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    bad = copy.deepcopy(payload)
    mutate(bad)
    violations = lint_container_config(bad)
    assert violations, "linter accepted a forbidden configuration"
    assert any(expected in v for v in violations), violations
    with pytest.raises(ContainerPolicyError):
        check_container_config(bad)


def test_linter_image_allowlist_and_digest(payload: dict[str, Any]) -> None:
    assert lint_container_config(payload, allowed_images={payload["Image"]}) == []
    assert lint_container_config(payload, allowed_images={"other@sha256:" + "b" * 64})
    unpinned = copy.deepcopy(payload)
    unpinned["Image"] = "python:3.12-slim-bookworm"
    assert any("digest" in v for v in lint_container_config(unpinned, require_image_digest=True))


def test_symlinked_worktree_is_resolved_before_lint(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "evil").symlink_to(tmp_path / "outside")
    spec = make_spec(mounts=(Mount(root / "evil", "/workspace", read_only=False),))
    p = build_host_config(spec)
    assert p["HostConfig"]["Mounts"][0]["Source"] == str((tmp_path / "outside").resolve())
    assert any("outside allowed roots" in v for v in lint_container_config(p, allowed_bind_roots=[str(root)]))


def test_runner_lints_before_create() -> None:
    runner = DockerSandboxRunner("http://127.0.0.1:9")  # не используется: отказ до сетевого вызова
    with pytest.raises(ContainerPolicyError):
        runner.create_payload(make_spec(mounts=(Mount(Path("/var/run/docker.sock"), "/var/run/docker.sock"),)))
    with pytest.raises(ContainerPolicyError):
        runner.create_payload(make_spec(network="host"))
    with pytest.raises(ContainerPolicyError):
        runner.create_payload(make_spec(user="0"))
    assert runner.create_payload(make_spec())["HostConfig"]["CapDrop"] == ["ALL"]


def _frame(stream: int, data: bytes) -> bytes:
    return struct.pack(">BxxxL", stream, len(data)) + data


def test_frame_parser_splits_streams_across_chunks() -> None:
    raw = _frame(1, b"hello ") + _frame(2, b"oops") + _frame(1, b"world") + _frame(3, b"ignored")
    parser = DockerFrameParser()
    out: list[OutputChunk] = []
    for i in range(0, len(raw), 3):  # режем поток на произвольные куски
        out.extend(parser.feed(raw[i : i + 3]))
    assert out == [
        OutputChunk("stdout", b"hello "),
        OutputChunk("stderr", b"oops"),
        OutputChunk("stdout", b"world"),
    ]
    assert parser.pending == 0


def test_runner_rejects_unknown_scheme_and_bad_ref() -> None:
    with pytest.raises(ValueError, match="scheme"):
        DockerSandboxRunner("ftp://x")
    with pytest.raises(ValueError, match="ref"):
        DockerSandboxRunner._container_id("pid:123")
    with pytest.raises(ValueError, match="ref"):
        DockerSandboxRunner._container_id("docker:../../x")
