"""Hardening-спецификация runner/verification-контейнеров и линтер (ADR-0010, SC-SBX-01…05).

* `build_host_config(spec)` — тело запроса Docker Engine API `POST /containers/create` с
  обязательным hardening: non-root, `CapDrop=ALL`, `no-new-privileges`, read-only rootfs,
  tmpfs `/tmp` (`noexec,nosuid,nodev`, ограниченный размер), лимиты pids/CPU/памяти (без swap),
  `Init=true`, никаких хостовых namespaces/устройств/сокетов, метки `aicd.*`.
* `lint_container_config(payload)` — линтер в процессе (SECURITY_ARCHITECTURE §7.5, Q-5):
  docker-socket-proxy фильтрует только endpoint'ы, а не тело `POST /containers/create`, поэтому
  каждое тело проверяется перед отправкой. Пустой список — спецификация допустима.

Модуль чистый (stdlib), не обращается к Docker.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from aicd_security.sandbox import ContainerSpec, Mount

DEFAULT_ALLOWED_BIND_ROOTS: tuple[str, ...] = ("/srv/aicd/worktrees", "/srv/aicd/context-packs")
"""Корни, под которыми допустимы bind-mount'ы (worktree задач, context packs)."""

DEFAULT_ALLOWED_VOLUME_PREFIXES: tuple[str, ...] = ("aicd-cred-", "aicd-scratch-")
"""Именованные тома: учётные данные провайдера и scratch-тома verification."""

RW_VOLUME_PREFIXES: tuple[str, ...] = ("aicd-cred-",)
"""Единственные тома, которые могут монтироваться rw (CLI обновляет токены, ADR-0010 п.3)."""

FORBIDDEN_NETWORK_MODES = frozenset({"host", "bridge", "default"})
HOST_NAMESPACE_KEYS = ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode")
WEAKENING_SECURITY_OPTS = ("seccomp=unconfined", "seccomp:unconfined", "apparmor=unconfined", "apparmor:unconfined")
FORBIDDEN_HOSTCONFIG_KEYS = (
    "Binds",
    "VolumesFrom",
    "Devices",
    "DeviceRequests",
    "DeviceCgroupRules",
    "CapAdd",
    "Sysctls",
    "MaskedPaths",
    "ReadonlyPaths",
    "PortBindings",
    "Links",
    "VolumeDriver",
    "CgroupParent",
)
ALLOWED_RUNTIMES = frozenset({"runc", "runsc"})
EXECUTION_ID_LABEL = "aicd.execution_id"
NOFILE_LIMIT = 4096
TMPFS_TMP_MAX_MB = 1024
TMPFS_HOME_MB = 256
STOP_TIMEOUT_SECONDS = 10
CONTAINER_TMP = "/tmp"  # noqa: S108 — путь внутри контейнера (tmpfs), не хоста

_VOLUME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROOT_USERS = frozenset({"", "0", "root"})


class ContainerPolicyError(ValueError):
    """Спецификация не может быть превращена в безопасную конфигурацию / отклонена линтером."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = list(violations)
        super().__init__("container spec rejected: " + "; ".join(self.violations))


def _mount_entry(mount: Mount) -> dict[str, Any]:
    source = mount.source
    if source.is_absolute():
        # realpath: symlink на worktree не должен уводить mount за пределы разрешённого корня.
        resolved = str(Path(source).resolve())
        return {
            "Type": "bind",
            "Source": resolved,
            "Target": mount.target,
            "ReadOnly": mount.read_only,
            "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
        }
    name = str(source)
    if not _VOLUME_NAME_RE.fullmatch(name):
        raise ContainerPolicyError([f"mount source {name!r} is neither an absolute path nor a valid volume name"])
    return {
        "Type": "volume",
        "Source": name,
        "Target": mount.target,
        "ReadOnly": mount.read_only,
        "VolumeOptions": {"NoCopy": True},
    }


def _log_max_size(max_output_bytes: int) -> str:
    # Логи json-file лежат на диске хоста: ограничиваем их с запасом на JSON-обёртку.
    mib = max(1, math.ceil(max_output_bytes * 2 / (1024 * 1024)))
    return f"{mib}m"


def build_host_config(spec: ContainerSpec) -> dict[str, Any]:
    """Тело `POST /containers/create` (Docker Engine API ≥ 1.44) для `spec`.

    argv исполняется напрямую (Entrypoint=argv[0], Cmd=argv[1:]) — entrypoint образа и shell не
    участвуют. Бросает `ContainerPolicyError`, если спецификацию нельзя представить безопасно.
    """
    errors: list[str] = []
    if not spec.argv or not spec.argv[0]:
        errors.append("argv must not be empty")
    if any("\x00" in a for a in spec.argv):
        errors.append("argv contains NUL")
    for key, value in spec.env.items():
        if not _ENV_KEY_RE.fullmatch(key) or "\x00" in value:
            errors.append(f"invalid env entry {key!r}")
    if not spec.workdir.startswith("/"):
        errors.append("workdir must be an absolute container path")
    for m in spec.mounts:
        if not m.target.startswith("/"):
            errors.append(f"mount target {m.target!r} must be absolute")
    if errors:
        raise ContainerPolicyError(errors)

    limits = spec.limits
    memory = int(limits.memory_mb) * 1024 * 1024
    tmp_mb = max(16, min(TMPFS_TMP_MAX_MB, limits.memory_mb // 4))
    tmpfs: dict[str, str] = {CONTAINER_TMP: f"rw,noexec,nosuid,nodev,size={tmp_mb}m"}
    home = spec.env.get("HOME")
    mount_targets = {m.target.rstrip("/") for m in spec.mounts}
    if home and home.startswith("/") and home.rstrip("/") not in mount_targets and home.rstrip("/") != CONTAINER_TMP:
        tmpfs[home] = f"rw,nosuid,nodev,size={TMPFS_HOME_MB}m"

    labels = {**spec.labels, "aicd.managed": "true"}
    labels.setdefault("aicd.kind", "runner")

    host_config: dict[str, Any] = {
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
        "Tmpfs": tmpfs,
        "Init": True,
        "NetworkMode": spec.network,
        "Mounts": [_mount_entry(m) for m in spec.mounts],
        "Memory": memory,
        "MemorySwap": memory,
        "NanoCpus": int(limits.cpus * 1_000_000_000),
        "PidsLimit": int(limits.pids),
        "Ulimits": [{"Name": "nofile", "Soft": NOFILE_LIMIT, "Hard": NOFILE_LIMIT}],
        "OomKillDisable": False,
        "PublishAllPorts": False,
        "AutoRemove": False,  # удаляет runner после сбора вывода и кода завершения
        "RestartPolicy": {"Name": "no"},
        "LogConfig": {
            "Type": "json-file",
            "Config": {"max-size": _log_max_size(limits.max_output_bytes), "max-file": "2"},
        },
    }
    return {
        "Image": spec.image,
        "Entrypoint": [spec.argv[0]],
        "Cmd": list(spec.argv[1:]),
        "Env": [f"{k}={v}" for k, v in spec.env.items()],
        "WorkingDir": spec.workdir,
        "User": spec.user,
        "Labels": labels,
        "Tty": False,
        "OpenStdin": False,
        "StdinOnce": False,
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
        "StopSignal": "SIGTERM",
        "StopTimeout": STOP_TIMEOUT_SECONDS,
        "NetworkDisabled": spec.network == "none",
        "HostConfig": host_config,
    }


def _under_root(path: str, roots: Iterable[str]) -> bool:
    p = PurePosixPath(path)
    if not p.is_absolute() or ".." in p.parts:
        return False
    for root in roots:
        r = PurePosixPath(root)
        if p == r or r in p.parents:
            return p != r  # сам корень (например, все worktree разом) монтировать нельзя
    return False


def _lint_mounts(
    mounts: Any,
    allowed_bind_roots: tuple[str, ...],
    allowed_volume_prefixes: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    if mounts is None:
        return errors
    if not isinstance(mounts, list):
        return ["HostConfig.Mounts must be a list"]
    rw_binds = 0
    targets: set[str] = set()
    for i, m in enumerate(mounts):
        if not isinstance(m, Mapping):
            errors.append(f"mount #{i} is not an object")
            continue
        mtype = m.get("Type")
        source = str(m.get("Source") or "")
        target = str(m.get("Target") or "")
        read_only = m.get("ReadOnly") is True
        where = f"mount #{i} ({source!r} -> {target!r})"
        if "docker.sock" in source or "docker.sock" in target or source.endswith(".sock") or "containerd" in source:
            errors.append(f"{where}: container runtime sockets must never be mounted")
        if not target.startswith("/") or ".." in PurePosixPath(target).parts:
            errors.append(f"{where}: target must be an absolute normalized path")
        if target.rstrip("/") in {"", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib"}:
            errors.append(f"{where}: system path target is forbidden")
        if target in targets:
            errors.append(f"{where}: duplicate target")
        targets.add(target)
        if mtype == "bind":
            if not _under_root(source, allowed_bind_roots):
                errors.append(f"{where}: bind source outside allowed roots {list(allowed_bind_roots)}")
            prop = (m.get("BindOptions") or {}).get("Propagation")
            if prop not in (None, "", "rprivate", "private"):
                errors.append(f"{where}: bind propagation {prop!r} is forbidden")
            if not read_only:
                rw_binds += 1
        elif mtype == "volume":
            if not source.startswith(allowed_volume_prefixes):
                errors.append(f"{where}: volume not in allowlist prefixes {list(allowed_volume_prefixes)}")
            if (m.get("VolumeOptions") or {}).get("DriverConfig"):
                errors.append(f"{where}: volume driver options are forbidden")
            if not read_only and not source.startswith(RW_VOLUME_PREFIXES):
                errors.append(f"{where}: only provider credential volumes may be read-write")
        else:
            errors.append(f"{where}: mount type {mtype!r} is forbidden")
    if rw_binds > 1:
        errors.append("only a single read-write bind mount (the task worktree) is allowed")
    return errors


def lint_container_config(
    payload: Mapping[str, Any],
    *,
    allowed_bind_roots: Iterable[str] = DEFAULT_ALLOWED_BIND_ROOTS,
    allowed_volume_prefixes: Iterable[str] = DEFAULT_ALLOWED_VOLUME_PREFIXES,
    allowed_images: Iterable[str] | None = None,
    require_image_digest: bool = False,
) -> list[str]:
    """Нарушения политики в теле `POST /containers/create`. Пустой список — можно отправлять."""
    roots = tuple(str(PurePosixPath(r)) for r in allowed_bind_roots)
    prefixes = tuple(allowed_volume_prefixes)
    errors: list[str] = []
    hc_raw = payload.get("HostConfig")
    if not isinstance(hc_raw, Mapping):
        return ["HostConfig is missing"]
    hc: Mapping[str, Any] = hc_raw

    image = payload.get("Image")
    if not isinstance(image, str) or not image:
        errors.append("Image is missing")
    else:
        if require_image_digest and "@sha256:" not in image:
            errors.append(f"image {image!r} is not pinned by digest")
        if allowed_images is not None and image not in set(allowed_images):
            errors.append(f"image {image!r} is not in the allowlist")

    user = str(payload.get("User") or "")
    if user.split(":", 1)[0] in _ROOT_USERS:
        errors.append("container must run as a non-root user")
    if payload.get("Tty") or payload.get("OpenStdin"):
        errors.append("Tty/OpenStdin are not allowed")
    labels = payload.get("Labels") or {}
    if not isinstance(labels, Mapping) or not labels.get(EXECUTION_ID_LABEL):
        errors.append(f"label {EXECUTION_ID_LABEL} is required")
    if payload.get("Volumes"):
        errors.append("anonymous Volumes are forbidden")
    if payload.get("ExposedPorts"):
        errors.append("ExposedPorts are forbidden")

    if hc.get("Privileged"):
        errors.append("Privileged is forbidden")
    for key in FORBIDDEN_HOSTCONFIG_KEYS:
        # MaskedPaths/ReadonlyPaths: даже пустой список снимает маскирование /proc (systempaths=unconfined).
        if hc.get(key) or (key in ("MaskedPaths", "ReadonlyPaths") and key in hc):
            errors.append(f"HostConfig.{key} is forbidden")
    cap_drop = [str(c).upper() for c in hc.get("CapDrop") or []]
    if "ALL" not in cap_drop:
        errors.append("CapDrop must contain ALL")
    sec_opts = [str(o).replace(" ", "") for o in hc.get("SecurityOpt") or []]
    if not any(o in ("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true") for o in sec_opts):
        errors.append("SecurityOpt must contain no-new-privileges:true")
    for opt in sec_opts:
        low = opt.lower()
        if (
            low in WEAKENING_SECURITY_OPTS
            or low.startswith("label=disable")
            or low.startswith("label:disable")
            or "systempaths=unconfined" in low
            or low.startswith(("seccomp=", "seccomp:"))  # только профиль по умолчанию
            or "no-new-privileges:false" in low
            or "no-new-privileges=false" in low
        ):
            errors.append(f"SecurityOpt {opt!r} weakens isolation")
    if hc.get("ReadonlyRootfs") is not True:
        errors.append("ReadonlyRootfs must be true")
    if hc.get("Init") is not True:
        errors.append("Init must be true")

    network = str(hc.get("NetworkMode") or "")
    if not network:
        errors.append("NetworkMode must be set explicitly")
    elif network in FORBIDDEN_NETWORK_MODES or network.startswith("container:"):
        errors.append(f"NetworkMode {network!r} is forbidden")
    for key in HOST_NAMESPACE_KEYS:
        value = str(hc.get(key) or "")
        if value == "host" or value.startswith("container:"):
            errors.append(f"{key} {value!r} is forbidden")
    runtime = hc.get("Runtime")
    if runtime and runtime not in ALLOWED_RUNTIMES:
        errors.append(f"Runtime {runtime!r} is not allowed")
    if hc.get("PublishAllPorts"):
        errors.append("PublishAllPorts is forbidden")
    if hc.get("OomKillDisable"):
        errors.append("OomKillDisable is forbidden")

    memory = hc.get("Memory")
    if not isinstance(memory, int) or memory <= 0:
        errors.append("Memory limit is required")
    elif hc.get("MemorySwap") != memory:
        errors.append("MemorySwap must equal Memory (no swap)")
    pids = hc.get("PidsLimit")
    if not isinstance(pids, int) or pids <= 0:
        errors.append("PidsLimit is required")
    nano = hc.get("NanoCpus")
    if not isinstance(nano, int) or nano <= 0:
        errors.append("NanoCpus limit is required")

    tmpfs = hc.get("Tmpfs") or {}
    if not isinstance(tmpfs, Mapping):
        errors.append("Tmpfs must be an object")
    else:
        for target, opts in tmpfs.items():
            parts = str(opts).split(",")
            if "nosuid" not in parts or "nodev" not in parts or not any(p.startswith("size=") for p in parts):
                errors.append(f"tmpfs {target!r} must be nosuid,nodev and size-limited")
        tmp_opts = str(tmpfs.get(CONTAINER_TMP, "")).split(",")
        if "noexec" not in tmp_opts:
            errors.append("tmpfs /tmp must be noexec")

    errors.extend(_lint_mounts(hc.get("Mounts"), roots, prefixes))
    return errors


def check_container_config(payload: Mapping[str, Any], **lint_options: Any) -> None:
    """`lint_container_config` + исключение — вызывается runner'ом перед каждым create."""
    violations = lint_container_config(payload, **lint_options)
    if violations:
        raise ContainerPolicyError(violations)
