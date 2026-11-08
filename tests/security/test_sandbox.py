"""Security (P2-07/P2-08/P2-14): runner-контейнер против реального Docker daemon.

ST-SBX-01/02/03/05/06, ST-NET-01: non-root, read-only rootfs, запись только в worktree, отсутствие
сети и внешнего DNS, лимиты, тайм-аут с удалением контейнера, разделение stdout/stderr, хостовые
секреты не видны, egress-proxy пропускает только CONNECT :443 к allowlist, docker-socket-proxy
отклоняет запрещённые endpoint'ы. Тесты пропускаются, если Docker недоступен.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from aicd_security.container_policy import CONTAINER_TMP, ContainerPolicyError
from aicd_security.docker_runner import DockerSandboxProcess, DockerSandboxRunner
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits

DOCKER_SOCK = "/var/run/docker.sock"
PY_IMAGE = "python:3.12-slim-bookworm"
SQUID_IMAGE = "ubuntu/squid:6.10-24.10_beta"
SOCKET_PROXY_IMAGE = "tecnativa/docker-socket-proxy:v0.4.1"
REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "infrastructure" / "docker"


def _docker_available() -> bool:
    if not Path(DOCKER_SOCK).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            if c.get("/_ping").status_code != 200:
                return False
            return c.get(f"/images/{PY_IMAGE}/json").status_code == 200
    except httpx.HTTPError:
        return False


def _image_present(image: str) -> bool:
    with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=5) as c:
        return c.get(f"/images/{image}/json").status_code == 200


DOCKER_OK = _docker_available()
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not DOCKER_OK, reason=f"Docker daemon / image {PY_IMAGE} недоступны"),
]


def docker_cli(*args: str, check: bool = True) -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI не найден")
    res = subprocess.run([docker, *args], capture_output=True, text=True, timeout=120, check=False)
    if check and res.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {res.stderr}")
    return res.stdout.strip()


def docker_logs(name: str) -> str:
    res = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=30, check=False)
    return res.stdout + res.stderr


@pytest.fixture
def area(tmp_path: Path) -> Path:
    """Корень, разрешённый линтеру для bind mounts (аналог /srv/aicd/worktrees)."""
    root = tmp_path / "allowed"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def worktree(area: Path) -> Path:
    wt = area / "wt"
    wt.mkdir()
    if os.geteuid() == 0:
        os.chown(wt, 10001, 10001)
    else:
        wt.chmod(0o777)
    return wt


@pytest.fixture
async def runner(area: Path) -> AsyncIterator[DockerSandboxRunner]:
    async with DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(area)], timeout_grace_seconds=1
    ) as r:
        yield r


def make_spec(code: str, worktree: Path, **overrides: Any) -> ContainerSpec:
    base: dict[str, Any] = {
        "image": PY_IMAGE,
        "argv": ("python", "-c", code),
        "workdir": "/workspace",
        "env": {"HOME": "/home/runner", "PYTHONDONTWRITEBYTECODE": "1"},
        "mounts": (Mount(worktree, "/workspace", read_only=False),),
        "limits": ResourceLimits(cpus=1.0, memory_mb=256, pids=64, timeout_seconds=60, max_output_bytes=1 << 20),
        "network": "none",
        "labels": {"aicd.execution_id": f"test-{secrets.token_hex(4)}", "aicd.kind": "test"},
    }
    base.update(overrides)
    return ContainerSpec(**base)


class Result:
    def __init__(self, code: int, stdout: bytes, stderr: bytes, proc: DockerSandboxProcess) -> None:
        self.code, self.stdout, self.stderr, self.proc = code, stdout, stderr, proc

    def json(self) -> Any:
        return json.loads(self.stdout.decode())


async def run(runner: DockerSandboxRunner, spec: ContainerSpec) -> Result:
    proc = await runner.start(spec)
    out, err = bytearray(), bytearray()

    async def collect() -> None:
        async for chunk in proc.output():
            (out if chunk.stream == "stdout" else err).extend(chunk.data)

    task = asyncio.create_task(collect())
    code = await proc.wait()
    await task
    return Result(code, bytes(out), bytes(err), proc)


async def test_runs_as_non_root_without_capabilities(runner: DockerSandboxRunner, worktree: Path) -> None:
    code = """
import json, os
status = dict(l.split(':', 1) for l in open('/proc/self/status').read().splitlines() if ':' in l)
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'euid': os.geteuid(),
    'cap_eff': status['CapEff'].strip(), 'cap_bnd': status['CapBnd'].strip(),
    'nnp': status['NoNewPrivs'].strip(), 'seccomp': status['Seccomp'].strip(), 'pid1': open('/proc/1/comm').read().strip()}))
"""
    r = await run(runner, make_spec(code, worktree))
    assert r.code == 0, r.stderr
    info = r.json()
    assert info["uid"] == 10001 and info["euid"] == 10001 and info["gid"] == 10001
    assert int(info["cap_eff"], 16) == 0 and int(info["cap_bnd"], 16) == 0
    assert info["nnp"] == "1"
    assert info["seccomp"] == "2"  # seccomp filter (профиль Docker по умолчанию)
    assert info["pid1"] == "docker-init"  # --init: tini реапит зомби


async def test_inspect_confirms_hardening_and_limits(runner: DockerSandboxRunner, worktree: Path) -> None:
    spec = make_spec("import time; time.sleep(60)", worktree)
    proc = await runner.start(spec)
    try:
        assert await runner.is_alive(proc.ref)
        info = await runner.inspect(proc.ref)
        assert info is not None
        hc = info["HostConfig"]
        assert hc["Privileged"] is False
        assert hc["CapDrop"] == ["ALL"] and not hc.get("CapAdd")
        assert "no-new-privileges:true" in hc["SecurityOpt"]
        assert hc["ReadonlyRootfs"] is True and hc["Init"] is True
        assert hc["Memory"] == 256 * 1024 * 1024 and hc["MemorySwap"] == hc["Memory"]
        assert hc["PidsLimit"] == 64 and hc["NanoCpus"] == 1_000_000_000
        assert hc["NetworkMode"] == "none"
        assert hc["PidMode"] in ("", "private") and hc["IpcMode"] in ("private", "none", "")
        assert not hc.get("Binds") and not hc.get("Devices")
        assert info["Config"]["User"] == "10001:10001"
        assert info["Config"]["Labels"]["aicd.execution_id"] == spec.labels["aicd.execution_id"]
        assert [m["Destination"] for m in info["Mounts"]] == ["/workspace"]
    finally:
        await proc.terminate(grace_seconds=1)
        await proc.wait()
    assert not await runner.is_alive(proc.ref)
    assert await runner.inspect(proc.ref) is None  # контейнер удалён


async def test_writes_only_inside_worktree(runner: DockerSandboxRunner, worktree: Path, area: Path) -> None:
    ctx = area / "context"
    ctx.mkdir()
    (ctx / "pack.md").write_text("context")
    code = r"""
import errno, json, os, subprocess
res = {}
for path in ['/probe', '/etc/probe', '/usr/probe', '/bin/probe', '/context/probe', '/context/pack.md', '/var/probe', '/root/probe']:
    try:
        with open(path, 'w') as f:
            f.write('x')
        res[path] = 'WROTE'
    except OSError as e:
        res[path] = errno.errorcode.get(e.errno, str(e.errno))
with open('/workspace/out.txt', 'w') as f:
    f.write('ok from sandbox')
res['workspace'] = 'WROTE'
with open('/tmp/x.sh', 'w') as f:
    f.write('#!/bin/sh\necho executed\n')
os.chmod('/tmp/x.sh', 0o755)
try:
    subprocess.run(['/tmp/x.sh'], check=True)
    res['tmp_exec'] = 'EXECUTED'
except PermissionError:
    res['tmp_exec'] = 'EACCES'
for mount_attempt in (['mount', '-t', 'tmpfs', 'none', '/mnt'],):
    res['mount'] = subprocess.run(mount_attempt, capture_output=True).returncode
print(json.dumps(res))
"""
    spec = make_spec(
        code,
        worktree,
        mounts=(Mount(worktree, "/workspace", read_only=False), Mount(ctx, "/context", read_only=True)),
    )
    r = await run(runner, spec)
    assert r.code == 0, r.stderr
    res = r.json()
    for path in (
        "/probe",
        "/etc/probe",
        "/usr/probe",
        "/bin/probe",
        "/context/probe",
        "/context/pack.md",
        "/var/probe",
        "/root/probe",
    ):
        assert res[path] in ("EROFS", "EACCES", "EPERM"), (path, res[path])
    assert res["workspace"] == "WROTE"
    assert (worktree / "out.txt").read_text() == "ok from sandbox"
    assert res["tmp_exec"] == "EACCES"  # tmpfs /tmp noexec
    assert res["mount"] != 0
    assert (ctx / "pack.md").read_text() == "context"


async def test_host_secrets_not_visible(
    runner: DockerSandboxRunner, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "aicd-secret-" + secrets.token_hex(16)
    secret_dir = tmp_path / "host-secrets"
    secret_dir.mkdir()
    secret_file = secret_dir / "provider-token"
    secret_file.write_text(token)
    monkeypatch.setenv("AICD_TEST_HOST_SECRET", token)
    code = f"""
import json, os
found = []
for base in ('/workspace', '/tmp', '/home'):
    for dirpath, _, files in os.walk(base):
        for name in files:
            try:
                if {token!r} in open(os.path.join(dirpath, name), errors='ignore').read():
                    found.append(os.path.join(dirpath, name))
            except OSError:
                pass
print(json.dumps({{
    'secret_path_exists': os.path.exists({str(secret_file)!r}),
    'secret_dir_exists': os.path.exists({str(secret_dir)!r}),
    'in_env': any({token!r} in v for v in os.environ.values()),
    'env_keys': sorted(os.environ),
    'in_mountinfo': {str(secret_dir)!r} in open('/proc/self/mountinfo').read(),
    'docker_sock': os.path.exists('/var/run/docker.sock') or os.path.exists('/run/docker.sock'),
    'found': found,
}}))
"""
    r = await run(runner, make_spec(code, worktree))
    assert r.code == 0, r.stderr
    res = r.json()
    assert res["secret_path_exists"] is False and res["secret_dir_exists"] is False
    assert res["in_env"] is False and "AICD_TEST_HOST_SECRET" not in res["env_keys"]
    assert res["in_mountinfo"] is False
    assert res["docker_sock"] is False
    assert res["found"] == []


NET_PROBE = """
import json, socket
res = {}
try:
    socket.create_connection(('1.1.1.1', 443), timeout=5).close()
    res['direct'] = 'CONNECTED'
except OSError as e:
    res['direct'] = type(e).__name__ + ':' + str(e.errno)
try:
    socket.getaddrinfo('example.com', 443)
    res['dns'] = 'RESOLVED'
except OSError as e:
    res['dns'] = type(e).__name__
print(json.dumps(res))
"""


async def test_no_network_on_none(runner: DockerSandboxRunner, worktree: Path) -> None:
    r = await run(runner, make_spec(NET_PROBE, worktree, network="none"))
    assert r.code == 0, r.stderr
    res = r.json()
    assert res["direct"] != "CONNECTED" and res["dns"] != "RESOLVED"


@pytest.fixture
def internal_network() -> Iterator[str]:
    name = f"aicd-test-sbx-{secrets.token_hex(4)}"
    docker_cli("network", "create", "--internal", "--label", "aicd.test=1", name)
    try:
        yield name
    finally:
        docker_cli("network", "rm", name, check=False)


async def test_no_direct_egress_on_internal_network(
    runner: DockerSandboxRunner, worktree: Path, internal_network: str
) -> None:
    r = await run(runner, make_spec(NET_PROBE, worktree, network=internal_network))
    assert r.code == 0, r.stderr
    res = r.json()
    assert res["direct"] != "CONNECTED", res
    assert res["dns"] != "RESOLVED", res


async def test_pids_limit_enforced(runner: DockerSandboxRunner, worktree: Path) -> None:
    code = """
import threading, time
ev = threading.Event()
n = 0
try:
    for _ in range(500):
        threading.Thread(target=ev.wait, daemon=True).start()
        n += 1
except RuntimeError:
    pass
print(n)
ev.set()
"""
    r = await run(
        runner, make_spec(code, worktree, limits=ResourceLimits(cpus=1, memory_mb=256, pids=32, timeout_seconds=60))
    )
    assert r.code == 0, r.stderr
    assert int(r.stdout) < 32


async def test_memory_limit_kills_allocation(runner: DockerSandboxRunner, worktree: Path) -> None:
    code = "b = bytearray(512 * 1024 * 1024); print('allocated', len(b))"
    r = await run(
        runner, make_spec(code, worktree, limits=ResourceLimits(cpus=1, memory_mb=128, pids=32, timeout_seconds=60))
    )
    assert r.code != 0
    assert b"allocated" not in r.stdout


async def test_timeout_terminates_and_removes(runner: DockerSandboxRunner, worktree: Path) -> None:
    spec = make_spec(
        "import time\nprint('started', flush=True)\ntime.sleep(300)",
        worktree,
        limits=ResourceLimits(cpus=1, memory_mb=128, pids=32, timeout_seconds=2),
    )
    t0 = time.monotonic()
    r = await run(runner, spec)
    elapsed = time.monotonic() - t0
    assert r.proc.timed_out is True
    assert r.code < 0, r.code  # убит сигналом (-15/-9)
    assert elapsed < 30
    assert b"started" in r.stdout
    assert not await runner.is_alive(r.proc.ref)
    assert await runner.inspect(r.proc.ref) is None


async def test_output_streams_separated(runner: DockerSandboxRunner, worktree: Path) -> None:
    code = """
import sys
for i in range(50):
    print(f'out-{i}', flush=True)
    print(f'err-{i}', file=sys.stderr, flush=True)
sys.exit(3)
"""
    r = await run(runner, make_spec(code, worktree))
    assert r.code == 3
    assert r.stdout.decode().split() == [f"out-{i}" for i in range(50)]
    assert r.stderr.decode().split() == [f"err-{i}" for i in range(50)]
    assert r.proc.truncated is False


async def test_output_truncated_but_process_accounted(runner: DockerSandboxRunner, worktree: Path) -> None:
    code = "import sys\nfor _ in range(200): sys.stdout.write('x' * 1000 + '\\n')\nsys.stdout.flush()\nsys.exit(0)"
    limits = ResourceLimits(cpus=1, memory_mb=128, pids=32, timeout_seconds=60, max_output_bytes=4096)
    r = await run(runner, make_spec(code, worktree, limits=limits))
    assert r.code == 0
    assert len(r.stdout) + len(r.stderr) == 4096
    assert r.proc.truncated is True
    assert r.proc.output_bytes_total == 200 * 1001


async def test_policy_violation_rejected_before_create(
    runner: DockerSandboxRunner, worktree: Path, tmp_path: Path
) -> None:
    label = f"test-deny-{secrets.token_hex(4)}"
    outside = tmp_path / "outside"
    outside.mkdir()
    bad_specs = [
        make_spec("print(1)", worktree, mounts=(Mount(Path(DOCKER_SOCK), "/var/run/docker.sock"),)),
        make_spec("print(1)", worktree, mounts=(Mount(outside, "/workspace", read_only=False),)),
        make_spec("print(1)", worktree, network="host"),
        make_spec("print(1)", worktree, network="bridge"),
        make_spec("print(1)", worktree, user="0:0"),
    ]
    for spec in bad_specs:
        spec.labels["aicd.execution_id"] = label
        with pytest.raises(ContainerPolicyError):
            await runner.start(spec)
    assert docker_cli("ps", "-a", "-q", "--filter", f"label=aicd.execution_id={label}") == ""


# --- P2-08: egress-proxy (Squid, конфигурация из infrastructure/docker/egress-proxy) -------------

EGRESS_PROBE = r"""
import json, os, socket
proxy = os.environ['PROXY_HOST']
def req(raw):
    try:
        s = socket.create_connection((proxy, 3128), timeout=20)
        s.sendall(raw.encode())
        line = s.recv(256).split(b'\r\n')[0].decode()
        s.close()
        return int(line.split()[1])
    except OSError as e:
        return 'ERR ' + type(e).__name__
res = {
  'denied_host': req('CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n'),
  'allowed_host_port_80': req('CONNECT allowed.test:80 HTTP/1.1\r\n\r\n'),
  'subdomain_of_allowed': req('CONNECT sub.allowed.test:443 HTTP/1.1\r\n\r\n'),
  'ip_literal': req('CONNECT 1.1.1.1:443 HTTP/1.1\r\n\r\n'),
  'plain_http': req('GET http://allowed.test/ HTTP/1.1\r\nHost: allowed.test\r\n\r\n'),
  'allowlisted_but_private': req('CONNECT localhost:443 HTTP/1.1\r\n\r\n'),
  'allowed_host': req('CONNECT allowed.test:443 HTTP/1.1\r\n\r\n'),
}
try:
    socket.create_connection(('1.1.1.1', 443), timeout=5).close()
    res['direct'] = 'CONNECTED'
except OSError as e:
    res['direct'] = type(e).__name__
print(json.dumps(res))
"""


@pytest.fixture
def egress_proxy(tmp_path: Path, internal_network: str) -> Iterator[str]:
    if not _image_present(SQUID_IMAGE):
        pytest.skip(f"образ {SQUID_IMAGE} отсутствует локально")
    subnet = docker_cli("network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", internal_network)
    confd = tmp_path / "squid-conf.d"
    confd.mkdir()
    (confd / "test.conf").write_text(f"acl sandbox_src src {subnet}\n")
    allow = tmp_path / "squid-allowlist"
    allow.mkdir()
    (allow / "allowed-hosts.txt").write_text("# test allowlist\nallowed.test\nlocalhost\n")
    for p in (confd, allow):
        p.chmod(0o755)
        for f in p.iterdir():
            f.chmod(0o644)
    outbound = f"aicd-test-egress-{secrets.token_hex(4)}"
    docker_cli("network", "create", "--label", "aicd.test=1", outbound)
    name = f"aicd-test-squid-{secrets.token_hex(4)}"
    try:
        docker_cli(
            "run", "-d", "--name", name, "--user", "proxy", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", CONTAINER_TMP, "--tmpfs", "/var/run/squid",
            "--tmpfs", "/var/spool/squid", "--tmpfs", "/var/log/squid",
            "-v", f"{DOCKER_DIR / 'egress-proxy'}:/etc/squid/aicd:ro",
            "-v", f"{confd}:/etc/squid/aicd/conf.d:ro",
            "-v", f"{allow}:/etc/squid/aicd/allowlist.d:ro",
            "--network", internal_network, "--entrypoint", "/usr/sbin/squid",
            SQUID_IMAGE, "-N", "-f", "/etc/squid/aicd/squid.conf",
        )  # fmt: skip
        docker_cli("network", "connect", outbound, name)
        deadline = time.monotonic() + 30
        while "Accepting HTTP Socket connections" not in docker_logs(name):
            if time.monotonic() > deadline:
                raise AssertionError("squid did not start: " + docker_logs(name))
            time.sleep(0.5)
        yield name
    finally:
        docker_cli("rm", "-f", name, check=False)
        docker_cli("network", "rm", outbound, check=False)


async def test_egress_proxy_enforces_allowlist(
    runner: DockerSandboxRunner, worktree: Path, internal_network: str, egress_proxy: str
) -> None:
    spec = make_spec(EGRESS_PROBE, worktree, network=internal_network, env={"PROXY_HOST": egress_proxy})
    r = await run(runner, spec)
    assert r.code == 0, r.stderr
    res = r.json()
    for key in (
        "denied_host",
        "allowed_host_port_80",
        "subdomain_of_allowed",
        "ip_literal",
        "plain_http",
        "allowlisted_but_private",
    ):
        assert res[key] == 403, (key, res)
    # Разрешённый хост проходит ACL (не 403); имя .test не резолвится → 503 от squid.
    assert res["allowed_host"] != 403 and isinstance(res["allowed_host"], int), res
    assert res["direct"] != "CONNECTED", res
    logs = docker_logs(egress_proxy)
    assert "dst=example.com:443 status=403 action=TCP_DENIED" in logs


# --- P2-07: docker-socket-proxy (конфигурация из compose.sandbox.yaml) ---------------------------


@pytest.fixture
def socket_proxy_url() -> Iterator[str]:
    if not _image_present(SOCKET_PROXY_IMAGE):
        pytest.skip(f"образ {SOCKET_PROXY_IMAGE} отсутствует локально")
    compose = yaml.safe_load((DOCKER_DIR / "compose.sandbox.yaml").read_text())
    svc = compose["services"]["docker-socket-proxy"]
    env_args = [a for k, v in svc["environment"].items() for a in ("-e", f"{k}={v}")]
    tmpfs_args = [a for t in svc["tmpfs"] for a in ("--tmpfs", t)]
    name = f"aicd-test-dsp-{secrets.token_hex(4)}"
    try:
        docker_cli(
            "run", "-d", "--name", name, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            *tmpfs_args, *env_args, "-v", f"{DOCKER_SOCK}:{DOCKER_SOCK}:ro", "-p", "127.0.0.1::2375",
            svc["image"].split("@")[0],
        )  # fmt: skip
        port = docker_cli("port", name, "2375").splitlines()[0].rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(f"{url}/_ping", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("docker-socket-proxy did not start: " + docker_cli("logs", name, check=False))
            time.sleep(0.3)
        yield url
    finally:
        docker_cli("rm", "-f", name, check=False)


async def test_socket_proxy_denies_forbidden_endpoints(socket_proxy_url: str) -> None:
    forbidden = [
        ("POST", "/v1.44/images/create?fromImage=alpine&tag=latest"),
        ("GET", "/v1.44/images/json"),
        ("POST", "/v1.44/build"),
        ("POST", "/v1.44/networks/create"),
        ("POST", "/v1.44/volumes/create"),
        ("GET", "/v1.44/info"),
        ("GET", "/v1.44/events"),
        ("POST", "/v1.44/exec/abc/start"),
        ("GET", "/v1.44/secrets"),
        ("GET", "/v1.44/plugins"),
        ("POST", "/v1.44/swarm/init"),
        ("GET", "/v1.44/system/df"),
    ]
    async with httpx.AsyncClient(base_url=socket_proxy_url, trust_env=False, timeout=10) as c:
        for method, path in forbidden:
            resp = await c.request(method, path, json={} if method == "POST" else None)
            assert resp.status_code == 403, (method, path, resp.status_code)
        assert (await c.get("/v1.44/containers/json")).status_code == 200


async def test_runner_through_socket_proxy(socket_proxy_url: str, worktree: Path, area: Path) -> None:
    async with DockerSandboxRunner(socket_proxy_url, allowed_bind_roots=[str(area)], timeout_grace_seconds=1) as runner:
        assert await runner.ping()
        r = await run(runner, make_spec("import sys; print('via proxy'); print('e', file=sys.stderr)", worktree))
        assert r.code == 0 and r.stdout == b"via proxy\n" and r.stderr == b"e\n"
        assert await runner.inspect(r.proc.ref) is None
        # тайм-аут (stop/kill/delete) тоже проходит через прокси
        spec = make_spec(
            "import time; time.sleep(120)",
            worktree,
            limits=ResourceLimits(cpus=1, memory_mb=128, pids=32, timeout_seconds=1),
        )
        r2 = await run(runner, spec)
        assert r2.proc.timed_out and r2.code < 0
        assert not await runner.is_alive(r2.proc.ref)
