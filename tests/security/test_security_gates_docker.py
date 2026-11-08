"""Security/perf/supply-chain gates против реальных закреплённых образов (P6-04, P6-06, P6-07).

Каждый gate исполняется `run_security_gate` через `DockerSandboxRunner` (network none, non-root,
read-only rootfs, репозиторий read-only) по фикстурам `tests/fixtures/security/<tool>` с заведомыми
находками; проверяются вердикт, нормализованные находки (rule id, severity, CWE, path:line) и
сохранённый отчёт (sha256, без секретов). Тесты пропускаются только при недоступном Docker daemon;
отсутствующий образ скачивается (`docker pull`) или собирается (OSV-Scanner).

`trivy-vuln` с настоящей БД выполняется, если задан `AICD_TRIVY_DB_DIR` (каталог `--download-db-only`);
без БД проверяется fail-closed (ERROR).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import string
import subprocess
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from aicd_security.docker_runner import DockerSandboxRunner
from aicd_verification import security_gates as sg
from aicd_verification.gates import RepoFacts
from aicd_verification.tools import images

DOCKER_SOCK = "/var/run/docker.sock"
REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "security"
OSV_DOCKERFILE = REPO / "infrastructure" / "docker" / "verification" / "osv-scanner.Dockerfile"


def _docker_available() -> bool:
    if not Path(DOCKER_SOCK).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            return c.get("/_ping").status_code == 200
    except httpx.HTTPError:
        return False


DOCKER_OK = _docker_available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not DOCKER_OK, reason="Docker daemon недоступен")]


def docker_cli(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None, "docker CLI не найден"
    return subprocess.run([docker, *args], capture_output=True, text=True, timeout=timeout, check=False)


def ensure_image(image: images.PinnedImage) -> None:
    if docker_cli("image", "inspect", image.ref).returncode == 0:
        return
    if image is images.OSV_SCANNER:
        res = docker_cli(
            "build", "-q", "-f", str(OSV_DOCKERFILE), "-t", f"{image.repository}:{image.tag}", str(OSV_DOCKERFILE.parent)
        )
        assert res.returncode == 0, res.stderr
        inspect = docker_cli("image", "inspect", image.ref)
        assert inspect.returncode == 0, (
            f"собранный образ OSV-Scanner не совпадает с закреплённым digest {image.digest}: обновите tools/images.py"
        )
        return
    res = docker_cli("pull", "-q", image.ref)
    assert res.returncode == 0, res.stderr


@pytest.fixture
def area(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    return root.resolve()


@pytest.fixture
async def runner(area: Path) -> AsyncIterator[DockerSandboxRunner]:
    async with DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(area)], timeout_grace_seconds=1, require_image_digest=True
    ) as r:
        yield r


def _readable(path: Path) -> None:
    for p in (path, *path.rglob("*")):
        p.chmod(0o755 if p.is_dir() else 0o644)


def make_repo(area: Path, fixture: str, name: str | None = None) -> Path:
    target = area / (name or fixture.replace("/", "-"))
    shutil.copytree(FIXTURES / fixture, target)
    _readable(target)
    return target


def facts_for(repo: Path, changed: tuple[str, ...] = ()) -> RepoFacts:
    from aicd_verification.gates import DiffEntry

    files = frozenset(str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file())
    changes = tuple(DiffEntry(c, "M", "100644", "100644", "0" * 40) for c in changed)
    return RepoFacts(files=files, changes=changes)


def request(area: Path, repo: Path, **kw: object) -> sg.SecurityRunRequest:
    token = secrets.token_hex(3)
    params: dict[str, object] = {
        "worktree": repo,
        "input_dir": area / f"input-{token}",
        "output_dir": area / f"output-{token}",
        "facts": facts_for(repo),
        "execution_id": f"test-sec-{token}",
        "timeout_seconds": 600,
    }
    params.update(kw)
    return sg.SecurityRunRequest(**params)  # type: ignore[arg-type]


def rules(result: sg.SecurityGateResult) -> set[str]:
    return {f.rule_id for f in result.verdict.findings}


def assert_report(result: sg.SecurityGateResult) -> str:
    assert result.report is not None
    data = result.report.path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == result.report.sha256
    assert result.report.size == len(data)
    return data.decode()


# --- SAST: Semgrep -----------------------------------------------------------------------------------


async def test_semgrep_finds_known_issues(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.SEMGREP)
    repo = make_repo(area, "semgrep/vulnerable")
    result = await sg.run_security_gate(runner, "semgrep-sast", request(area, repo))
    assert result.verdict.status == "FAIL", result.verdict
    assert result.exit_code == 0
    assert {
        "aicd.python.subprocess-shell-true",
        "aicd.python.os-system",  # `# nosemgrep` не действует (--disable-nosem)
        "aicd.python.yaml-unsafe-load",
        "aicd.python.pickle-load",
        "aicd.python.eval-exec",
        "aicd.js.inner-html",
        "aicd.js.child-process-exec",
    } <= rules(result)
    shell = next(f for f in result.verdict.findings if f.rule_id == "aicd.python.subprocess-shell-true")
    assert (shell.file_path, shell.line, shell.severity, shell.cwe_id) == ("vuln_handlers.py", 11, "high", "CWE-78")
    assert len({f.fingerprint for f in result.verdict.findings}) == len(result.verdict.findings)
    report = json.loads(assert_report(result))
    assert report["version"] == images.SEMGREP.tag


async def test_semgrep_clean_passes_and_basic_profile_threshold(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.SEMGREP)
    clean = await sg.run_security_gate(runner, "semgrep-sast", request(area, make_repo(area, "semgrep/clean")))
    assert clean.verdict.status == "PASS", clean.verdict
    assert clean.verdict.findings == ()
    # basic блокирует только critical: high-находки остаются в evidence, но gate PASS.
    vuln = await sg.run_security_gate(
        runner, "semgrep-sast", request(area, make_repo(area, "semgrep/vulnerable", "v2"), profile="basic")
    )
    assert vuln.verdict.status == "PASS", vuln.verdict
    assert vuln.verdict.findings


async def test_semgrep_broken_config_is_error(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.SEMGREP)
    repo = make_repo(area, "semgrep/clean")
    argv = ("semgrep", "scan", "--config", "/aicd-input/missing.yml", "--metrics=off", "--json", "--quiet", "/workspace")
    result = await sg.run_security_gate(runner, "semgrep-sast", request(area, repo), argv=argv)
    assert result.verdict.status == "ERROR"


# --- SECRETS: Gitleaks -------------------------------------------------------------------------------


def _random(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def materialize_secrets(repo: Path) -> dict[str, str]:
    values = {
        "SLACK_BOT_TOKEN": f"xoxb-{_random(string.digits, 12)}-{_random(string.digits, 12)}-"
        f"{_random(string.ascii_letters + string.digits, 24)}",
        "GITHUB_PAT": "ghp_" + _random(string.ascii_letters + string.digits, 36),
    }
    for tmpl in repo.rglob("*.tmpl"):
        text = tmpl.read_text()
        for key, value in values.items():
            text = text.replace(f"@@{key}@@", value)
        target = tmpl.with_suffix("")
        target.write_text(text)
        target.chmod(0o644)
        tmpl.unlink()
    return values


async def test_gitleaks_detects_secrets_ignoring_repo_suppressions(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.GITLEAKS)
    repo = make_repo(area, "gitleaks/leaky")
    values = materialize_secrets(repo)
    result = await sg.run_security_gate(runner, "gitleaks-secrets", request(area, repo, profile="basic"))
    assert result.verdict.status == "FAIL", result.verdict
    assert result.exit_code == 1
    assert {"slack-bot-token", "github-pat"} <= rules(result)
    assert all(f.cwe_id == "CWE-798" and f.file_path == "settings.env" for f in result.verdict.findings)
    report = assert_report(result)
    for value in values.values():
        assert value not in report
        assert all(value not in f.title for f in result.verdict.findings)


async def test_gitleaks_clean_passes(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.GITLEAKS)
    result = await sg.run_security_gate(runner, "gitleaks-secrets", request(area, make_repo(area, "gitleaks/clean")))
    assert result.verdict.status == "PASS", result.verdict
    assert result.exit_code == 0


# --- IAC: Trivy config + secrets, Checkov ------------------------------------------------------------


async def test_trivy_config_finds_misconfigurations(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.TRIVY)
    repo = make_repo(area, "trivy/iac")
    result = await sg.run_security_gate(runner, "trivy-config", request(area, repo))
    assert result.verdict.status == "FAIL", result.verdict
    found = rules(result)
    assert "DS-0002" in found  # .trivyignore репозитория не действует
    assert any(r.startswith("KSV") for r in found)
    root_user = next(f for f in result.verdict.findings if f.rule_id == "DS-0002")
    assert (root_user.file_path, root_user.severity) == ("Dockerfile", "high")


async def test_checkov_finds_iac_issues(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.CHECKOV)
    repo = make_repo(area, "checkov/iac")
    result = await sg.run_security_gate(runner, "checkov-iac", request(area, repo))
    assert result.verdict.status == "FAIL", result.verdict
    assert result.exit_code == 1
    found = {f.rule_id: f for f in result.verdict.findings}
    assert found["CKV_AWS_57"].severity == "critical"
    assert found["CKV_DOCKER_8"].file_path == "Dockerfile"
    assert found["CKV_AWS_24"].file_path == "main.tf" and found["CKV_AWS_24"].line == 10


async def test_checkov_timeout_is_error(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.CHECKOV)
    result = await sg.run_security_gate(
        runner, "checkov-iac", request(area, make_repo(area, "checkov/iac"), timeout_seconds=1)
    )
    assert result.verdict.status == "ERROR"
    assert result.verdict.error_code == "TIMEOUT"


# --- SCA: Trivy vuln, OSV-Scanner --------------------------------------------------------------------


async def test_trivy_vuln_without_db_is_error(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.TRIVY)
    repo = make_repo(area, "trivy/vuln")
    no_db = await sg.run_security_gate(runner, "trivy-vuln", request(area, repo))
    assert no_db.verdict.status == "ERROR" and "DB_UNAVAILABLE" in no_db.verdict.message
    # Метаданные «свежие», но файла БД нет содержимого: fail-closed обеспечивает сам trivy.
    fake = area / "fake-trivy-db"
    (fake / "db").mkdir(parents=True)
    (fake / "db" / "trivy.db").write_bytes(b"")
    now = datetime.now(UTC)
    (fake / "db" / "metadata.json").write_text(json.dumps({"Version": 2, "UpdatedAt": now.isoformat()}))
    _readable(fake)
    broken = await sg.run_security_gate(runner, "trivy-vuln", request(area, repo, db_dir=fake))
    assert broken.verdict.status == "ERROR", broken.verdict
    assert broken.exit_code not in (None, 0)


@pytest.mark.skipif(not os.environ.get("AICD_TRIVY_DB_DIR"), reason="AICD_TRIVY_DB_DIR (офлайн-БД trivy) не задан")
async def test_trivy_vuln_with_offline_db(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.TRIVY)
    db = area / "trivy-db"
    shutil.copytree(Path(os.environ["AICD_TRIVY_DB_DIR"]), db)
    _readable(db)
    repo = make_repo(area, "trivy/vuln")
    result = await sg.run_security_gate(
        runner, "trivy-vuln", request(area, repo, db_dir=db, db_max_age=timedelta(days=30))
    )
    assert result.verdict.status == "FAIL", result.verdict
    cve = next(f for f in result.verdict.findings if f.rule_id == "CVE-2020-14343")  # .trivyignore не действует
    assert (cve.severity, cve.file_path, cve.cwe_id) == ("critical", "requirements.txt", "CWE-20")
    assert result.db is not None and result.db.ready


def make_osv_db(area: Path, *, ecosystem: str = "PyPI", records: bool = True) -> Path:
    db = area / f"osv-db-{secrets.token_hex(3)}"
    target = db / "osv-scalibr" / ecosystem
    target.mkdir(parents=True)
    with zipfile.ZipFile(target / "all.zip", "w") as zf:
        if records:
            for rec in (FIXTURES / "osv" / "db").glob("*.json"):
                zf.write(rec, rec.name)
    (db / sg.OSV_MANIFEST_FILE).write_text(
        json.dumps({"updated_at": datetime.now(UTC).isoformat(), "ecosystems": [ecosystem]})
    )
    _readable(db)
    return db


async def test_osv_scanner_offline_db_finds_vulnerability(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.OSV_SCANNER)
    repo = make_repo(area, "osv/project")
    result = await sg.run_security_gate(runner, "osv-scanner-sca", request(area, repo, db_dir=make_osv_db(area)))
    assert result.verdict.status == "FAIL", result.verdict
    assert result.exit_code == 1
    (finding,) = result.verdict.findings  # osv-scanner.toml репозитория (IgnoredVulns) не действует
    assert (finding.rule_id, finding.severity, finding.cwe_id, finding.file_path) == (
        "AICD-TEST-2026-0001",
        "critical",
        "CWE-20",
        "requirements.txt",
    )
    assert "PyPI:pyyaml@5.3.1" in finding.title


async def test_osv_scanner_without_db_is_error(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.OSV_SCANNER)
    repo = make_repo(area, "osv/project")
    missing = await sg.run_security_gate(runner, "osv-scanner-sca", request(area, repo))
    assert missing.verdict.status == "ERROR" and "DB_UNAVAILABLE" in missing.verdict.message
    # Манифест есть, но БД экосистемы PyPI нет — osv-scanner сам завершается ошибкой.
    wrong = await sg.run_security_gate(
        runner, "osv-scanner-sca", request(area, repo, db_dir=make_osv_db(area, ecosystem="npm"))
    )
    assert wrong.verdict.status == "ERROR", wrong.verdict
    stale = await sg.run_security_gate(
        runner,
        "osv-scanner-sca",
        request(area, repo, db_dir=make_osv_db(area), now=datetime.now(UTC) + timedelta(days=30)),
    )
    assert stale.verdict.status == "ERROR" and "stale" in stale.verdict.message


# --- POLICY: conftest --------------------------------------------------------------------------------


async def test_conftest_policy_violation_and_compliant(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.CONFTEST)
    bad = await sg.run_security_gate(runner, "conftest-policy", request(area, make_repo(area, "conftest/project")))
    assert bad.verdict.status == "FAIL", bad.verdict
    deny = next(f for f in bad.verdict.findings if f.severity == "high")
    assert (deny.rule_id, deny.file_path) == ("data.main.deny", "deploy/app.yaml")
    assert "privileged" in deny.title
    good = await sg.run_security_gate(runner, "conftest-policy", request(area, make_repo(area, "conftest/compliant")))
    assert good.verdict.status == "PASS", good.verdict


async def test_conftest_policy_change_in_diff_is_flagged(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.CONFTEST)
    repo = make_repo(area, "conftest/compliant")
    result = await sg.run_security_gate(
        runner, "conftest-policy", request(area, repo, facts=facts_for(repo, ("policy/k8s.rego",)))
    )
    assert result.verdict.status == "FAIL"
    assert sg.GATE_CONFIG_CHANGED_RULE in rules(result)


# --- SBOM: Syft --------------------------------------------------------------------------------------


async def test_syft_sbom_is_cyclonedx_artifact(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.SYFT)
    result = await sg.run_security_gate(runner, "syft-sbom", request(area, make_repo(area, "syft/project")))
    assert result.verdict.status == "PASS", result.verdict
    sbom = json.loads(assert_report(result))
    assert sbom["bomFormat"] == "CycloneDX"
    names = {c["name"] for c in sbom["components"]}
    assert {"pyyaml", "left-pad"} <= names
    assert result.report is not None and result.report.name == "sbom.cdx.json"
    assert result.report.media_type == "application/vnd.cyclonedx+json"


# --- SIGN: Cosign ------------------------------------------------------------------------------------


def cosign_setup(area: Path, name: str = "cosign-keys") -> tuple[Path, str]:
    """Ключи и подпись релизного файла (тестовая подготовка вне gate, без сети и Rekor)."""
    keys = area / name
    keys.mkdir()
    keys.chmod(0o777)
    base = ["run", "--rm", "--network", "none", "--read-only", "--user", "10001:10001", "--tmpfs", "/tmp"]
    base += ["-e", "HOME=/tmp", "-e", "COSIGN_PASSWORD=", "-w", "/keys", "-v", f"{keys}:/keys"]
    res = docker_cli(*base, images.COSIGN.ref, "generate-key-pair")
    assert res.returncode == 0, res.stderr
    shutil.copy(FIXTURES / "cosign" / "release-notes.txt", keys / "release-notes.txt")
    (keys / "release-notes.txt").chmod(0o644)
    res = docker_cli(
        *base,
        images.COSIGN.ref,
        "sign-blob",
        "--key",
        "cosign.key",
        "--use-signing-config=false",
        "--tlog-upload=false",
        "--yes",
        "--bundle",
        "release-notes.txt.sigstore.json",
        "release-notes.txt",
    )
    assert res.returncode == 0, res.stderr
    return keys, (keys / "cosign.pub").read_text()


async def test_cosign_verifies_blob_offline_and_rejects_tampering(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.COSIGN)
    keys, pub = cosign_setup(area)
    repo = area / "release"
    (repo / "dist").mkdir(parents=True)
    shutil.copy(keys / "release-notes.txt", repo / "dist" / "release-notes.txt")
    shutil.copy(keys / "release-notes.txt.sigstore.json", repo / "dist" / "release-notes.txt.sigstore.json")
    _readable(repo)
    subject = sg.SignatureSubject("blob", "release-notes", "dist/release-notes.txt", "dist/release-notes.txt.sigstore.json")
    policy = sg.SigningPolicy(pub, (subject,))
    verdict, results = await sg.run_signature_gate(runner, policy, request(area, repo))
    assert verdict.status == "PASS", [r.verdict for r in results]

    (repo / "dist" / "release-notes.txt").write_text("tampered\n")
    verdict, results = await sg.run_signature_gate(runner, policy, request(area, repo))
    assert verdict.status == "FAIL", [r.verdict for r in results]
    assert "invalid signature" in results[0].verdict.message

    # Подпись верна, но ключ политики другой → FAIL.
    shutil.copy(keys / "release-notes.txt", repo / "dist" / "release-notes.txt")
    _readable(repo)
    other_key = sg.SigningPolicy(cosign_setup(area, "other-keys")[1], (subject,))
    verdict, results = await sg.run_signature_gate(runner, other_key, request(area, repo))
    assert verdict.status == "FAIL", [r.verdict for r in results]


async def test_cosign_unsigned_image_layout_is_not_pass(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.COSIGN)
    _keys, pub = cosign_setup(area)
    req = request(area, make_repo(area, "cosign"))
    layout = req.input_dir / "images" / "alpine"
    layout.mkdir(parents=True)
    tarball = area / "image.tar"
    assert docker_cli("save", "-o", str(tarball), images.PYTHON_BENCH.ref).returncode == 0
    subprocess.run(["tar", "-xf", str(tarball), "-C", str(layout)], check=True)
    _readable(req.input_dir)
    policy = sg.SigningPolicy(pub, (sg.SignatureSubject("image", "python", "alpine"),))
    verdict, results = await sg.run_signature_gate(runner, policy, req)
    assert verdict.status in ("FAIL", "ERROR"), [r.verdict for r in results]


# --- PERF: бенчмарк baseline vs candidate ------------------------------------------------------------


def perf_repos(area: Path, base_iterations: int, head_iterations: int) -> tuple[Path, Path]:
    base = make_repo(area, "perf/project", "perf-base")
    head = make_repo(area, "perf/project", "perf-head")
    (base / "bench" / "params.json").write_text(json.dumps({"iterations": base_iterations}))
    (head / "bench" / "params.json").write_text(json.dumps({"iterations": head_iterations}))
    return base, head


def perf_config(base: Path, claim: str) -> dict[str, object]:
    return {
        "aicd_perf_definition": (base / ".aicd" / "perf.json").read_text(),
        "aicd_perf_claims": [{"type": "implementation_completed", "summary": claim}],
    }


async def test_perf_confirms_real_speedup(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.PYTHON_BENCH)
    base, head = perf_repos(area, 2_000_000, 200_000)
    req = request(area, head, baseline_dir=base, config=perf_config(base, "Ускорил подсчёт на 30%"))
    result = await sg.run_security_gate(runner, "bench-perf", req)
    assert result.verdict.status == "PASS", result.verdict
    report = json.loads(assert_report(result))
    assert len(report["variants"]["baseline"]["samples_ns"]) == 7
    assert report["environment"]["cpu_count"] >= 1


async def test_perf_rejects_unconfirmed_claim(runner: DockerSandboxRunner, area: Path) -> None:
    ensure_image(images.PYTHON_BENCH)
    base, head = perf_repos(area, 500_000, 500_000)
    req = request(area, head, baseline_dir=base, config=perf_config(base, "2x faster parsing"))
    result = await sg.run_security_gate(runner, "bench-perf", req)
    assert result.verdict.status == "FAIL", result.verdict
    assert "not confirmed" in result.verdict.message
    no_definition = await sg.run_security_gate(
        runner,
        "bench-perf",
        request(area, head, baseline_dir=base, config={"aicd_perf_claims": [{"type": "other", "summary": "faster"}]}),
    )
    assert no_definition.verdict.status == "FAIL"
    assert "unverifiable" in no_definition.verdict.message
