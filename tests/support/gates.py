"""Фикстурные репозитории gates (tests/fixtures/gates/<tool>/<case>) и прогон GateSpec без БД.

Python-файлы фикстур хранятся как `*.py.tmpl` (иначе их подхватили бы pytest/ruff/mypy самого проекта);
при материализации суффикс `.tmpl` снимается. Репозиторий получает base-commit (README) и commit с
содержимым фикстуры, поэтому все файлы фикстуры — «изменённые» относительно base.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits, SandboxRunner
from aicd_verification.gates import CONTAINER_WORKDIR, GateContext, GateSpec, RepoFacts, Verdict, collect_repo_facts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gates"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=GIT_ENV
    ).stdout.strip()


@dataclass(frozen=True)
class FixtureRepo:
    path: Path
    base: str
    head: str

    def facts(self) -> RepoFacts:
        return collect_repo_facts(self.path, self.head, self.base)


def materialize(tool: str, case: str, dest: Path) -> FixtureRepo:
    src = FIXTURES / tool / case
    assert src.is_dir(), src  # noqa: S101 — тестовый помощник
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", "main")
    (dest / "README.md").write_text("fixture\n")
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
    base = git(dest, "rev-parse", "HEAD")
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dest / (str(rel)[: -len(".tmpl")] if rel.name.endswith(".tmpl") else rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", f"{tool}/{case}")
    return FixtureRepo(dest, base, git(dest, "rev-parse", "HEAD"))


@dataclass(frozen=True)
class SpecRun:
    probe_code: int | None
    code: int | None
    stdout: str
    stderr: str
    verdict: Verdict | None


async def _collect(runner: SandboxRunner, spec: ContainerSpec, timeout: float) -> tuple[int | None, str, str]:
    try:
        proc = await runner.start(spec)
    except (OSError, ValueError):
        return None, "", ""
    out, err = bytearray(), bytearray()

    async def read() -> int:
        async for chunk in proc.output():
            (out if chunk.stream == "stdout" else err).extend(chunk.data)
        return await proc.wait()

    try:
        code = await asyncio.wait_for(read(), timeout=timeout)
    except TimeoutError:
        await proc.terminate(grace_seconds=1.0)
        code = -9
    return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def run_spec(
    spec: GateSpec,
    repo: FixtureRepo,
    *,
    profile: str = "strict",
    path: str | None = None,
    runner: SandboxRunner | None = None,
    image: str | None = None,
    config: dict[str, object] | None = None,
    timeout: float = 300.0,
) -> SpecRun:
    """Probe + команда gate тем же argv, что у движка; локально (по умолчанию) или в контейнере."""
    facts = repo.facts()
    docker = image is not None
    workdir = CONTAINER_WORKDIR if docker else str(repo.path)
    ctx = GateContext(
        workdir,
        workdir,
        profile=profile,
        config=config or {},
        facts=facts,
        changed_files=facts.changed_paths,
        nonce=uuid.uuid4().hex,
    )
    run = runner or LocalProcessRunner()
    home = repo.path.parent / f"home-{uuid.uuid4().hex[:8]}"
    home.mkdir()

    def container(argv: tuple[str, ...]) -> ContainerSpec:
        limits = ResourceLimits(cpus=2.0, memory_mb=2048, pids=256, timeout_seconds=int(timeout) + 30)
        labels = {"aicd.execution_id": f"test-{uuid.uuid4()}", "aicd.kind": "verification"}
        if docker:
            assert image is not None  # noqa: S101
            extra = {"user": spec.container_user} if spec.container_user else {}
            return ContainerSpec(
                image=image,
                argv=argv,
                workdir=CONTAINER_WORKDIR,
                env={"CI": "true", "HOME": "/home/verify", "LANG": "C.UTF-8"},
                mounts=(Mount(repo.path, CONTAINER_WORKDIR, read_only=True),),
                limits=limits,
                network="none",
                labels=labels,
                **extra,  # type: ignore[arg-type]
            )
        env = {"CI": "true", "HOME": str(home), "LANG": "C.UTF-8", "PATH": path or os.environ.get("PATH", "")}
        return ContainerSpec(image="local", argv=argv, workdir=str(repo.path), env=env, limits=limits, cwd=repo.path)

    probe_code, _o, _e = await _collect(run, container(spec.probe(ctx)), 60)
    if probe_code != 0:
        return SpecRun(probe_code, None, "", "", None)
    code, out, err = await _collect(run, container(spec.build_argv(ctx)), timeout)
    verdict = spec.run_evaluate(code, out, ctx) if code is not None and code >= 0 else None
    return SpecRun(probe_code, code, out, err, verdict)
