"""Unit (P6-04/P6-06/P6-07): argv builders, применимость, пороги профилей, правила вердикта,
офлайн-БД и эталонный цикл `run_security_gate` на поддельном runner'е (без Docker)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aicd_security.container_policy import build_host_config, lint_container_config
from aicd_security.sandbox import ContainerSpec, OutputChunk
from aicd_verification import security_gates as sg
from aicd_verification.engine import GateEnvError, declared_gate_env
from aicd_verification.gates import DiffEntry, GateContext, GateSpec, RepoFacts
from aicd_verification.images import validate_image_map
from aicd_verification.registry import GATE_SPECS
from aicd_verification.tools import images

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "security" / "samples"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def facts(*files: str, changed: tuple[str, ...] = ()) -> RepoFacts:
    return RepoFacts(
        files=frozenset(files), changes=tuple(DiffEntry(c, "M", "100644", "100644", "0" * 40) for c in changed)
    )


def ctx(profile: str = "strict", f: RepoFacts | None = None, **config: object) -> GateContext:
    return GateContext("/workspace", "/aicd-input", profile=profile, config=config, facts=f)


# --- Образы и спецификации --------------------------------------------------------------------------


def test_images_are_pinned_by_digest_without_latest() -> None:
    assert validate_image_map(sg.SECURITY_GATE_IMAGES, require_digest=True) == []
    for image in images.PINNED_IMAGES.values():
        assert image.tag != "latest"
        assert image.ref.endswith(image.digest) and image.digest.startswith("sha256:")


def test_specs_are_gate_specs_with_unique_names_and_definitions() -> None:
    assert set(sg.SECURITY_GATE_SPECS) == {name for name, *_ in sg.SECURITY_GATE_DEFINITIONS}
    for name, spec in sg.SECURITY_GATE_SPECS.items():
        assert isinstance(spec, GateSpec) and spec.name == name
        assert spec.evaluate is not None and spec.needs_input_dir
        assert sg.SECURITY_GATE_IMAGES[spec.image_key or spec.tool]
    types = {n: t for n, t, *_ in sg.SECURITY_GATE_DEFINITIONS}
    assert types == {
        "semgrep-sast": "SAST",
        "gitleaks-secrets": "SECRETS",
        "trivy-vuln": "SCA",
        "osv-scanner-sca": "SCA",
        "trivy-config": "IAC",
        "checkov-iac": "IAC",
        "conftest-policy": "POLICY",
        "bench-perf": "PERF",
        "syft-sbom": "SBOM",
        "cosign-verify": "SIGN",
    }
    assert {s.name for s in sg.SECURITY_PROVIDER.specs} == set(sg.SECURITY_GATE_SPECS)


# --- argv --------------------------------------------------------------------------------------------


def test_argv_are_fixed_and_offline() -> None:
    c = ctx()
    semgrep = sg.semgrep_argv(c)
    assert semgrep[0] == "semgrep" and "--metrics=off" in semgrep and "--disable-nosem" in semgrep
    assert semgrep[semgrep.index("--config") + 1] == "/aicd-input/semgrep.yml"
    gitleaks = sg.gitleaks_argv(c)
    assert gitleaks[gitleaks.index("--config") + 1] == "/aicd-input/gitleaks.toml"
    assert gitleaks[gitleaks.index("--gitleaks-ignore-path") + 1] == "/aicd-input"
    assert "--redact" in gitleaks and gitleaks[gitleaks.index("--report-path") + 1] == "-"
    vuln = sg.trivy_vuln_argv(c)
    for flag in ("--skip-db-update", "--offline-scan", "--skip-java-db-update"):
        assert flag in vuln
    assert vuln[vuln.index("--cache-dir") + 1] == "/aicd-db"
    assert vuln[vuln.index("--ignorefile") + 1] == "/aicd-input/trivyignore"
    assert sg.trivy_config_argv(c)[sg.trivy_config_argv(c).index("--scanners") + 1] == "misconfig,secret"
    osv = sg.osv_argv(c)
    assert "--offline" in osv and osv[osv.index("--config") + 1] == "/aicd-input/osv-scanner.toml"
    assert "--skip-download" in sg.checkov_argv(c)
    assert sg.syft_argv(c)[-3:] == ("-o", "cyclonedx-json", "-q")
    for builder in (sg.semgrep_argv, sg.gitleaks_argv, sg.trivy_vuln_argv, sg.osv_argv, sg.checkov_argv, sg.syft_argv):
        argv = builder(c)
        assert all(isinstance(a, str) and a for a in argv)
        assert not {"sh", "bash", "-c"} & set(argv[:2])


def test_conftest_argv_lists_policies_and_config_inputs() -> None:
    f = facts(
        "policy/k8s.rego",
        "policy/k8s_test.rego",
        "deploy/app.yaml",
        "charts/x/templates/d.yaml",
        "node_modules/a/package.json",
        "Dockerfile",
        "main.py",
    )
    argv = sg.conftest_argv(ctx(f=f))
    assert argv[:2] == ("/conftest", "test") and "--all-namespaces" in argv
    assert argv[argv.index("--policy") + 1] == "/workspace/policy"
    assert argv[-2:] == ("/workspace/Dockerfile", "/workspace/deploy/app.yaml")


def test_cosign_argv() -> None:
    blob = sg.SignatureSubject("blob", "notes", "dist/n.txt", "dist/n.txt.sigstore.json")
    assert sg.cosign_subject_argv(blob) == (
        "/ko-app/cosign",
        "verify-blob",
        "--key",
        "/aicd-input/cosign.pub",
        "--bundle",
        "/workspace/dist/n.txt.sigstore.json",
        "--insecure-ignore-tlog=true",
        "/workspace/dist/n.txt",
    )
    image = sg.cosign_subject_argv(sg.SignatureSubject("image", "app", "app"))
    assert image[1] == "verify" and image[-2:] == ("--local-image", "/aicd-input/images/app")
    for bad in (sg.SignatureSubject("blob", "x", "../etc/passwd", "b"), sg.SignatureSubject("blob", "x", "a", None)):
        with pytest.raises(ValueError, match=r"unsafe relative path|has no bundle"):
            sg.cosign_subject_argv(bad)
    single = sg.cosign_argv(ctx(subject={"kind": "blob", "path": "a.bin", "bundle": "a.bin.sigstore.json"}))
    assert single[-1] == "/workspace/a.bin"


def test_bench_argv_runs_stdlib_harness_isolated() -> None:
    argv = sg.bench_argv(ctx())
    assert argv[:4] == ("python3", "-I", "-S", "-c") and argv[-1] == "/aicd-input/bench.json"
    assert sg.BENCH_SHA256 in sg.bench_display(ctx())[4]


def test_container_spec_is_read_only_offline_and_passes_policy_lint(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    (root / "wt").mkdir(parents=True)
    req = sg.SecurityRunRequest(
        worktree=root / "wt",
        input_dir=root / "in",
        output_dir=root / "out",
        db_dir=root / "db",
        execution_id="verify-x",
    )
    spec = sg.build_container_spec(sg.SECURITY_GATES["trivy-vuln"], req, ("trivy", "--version"))
    assert spec.network == "none" and all(m.read_only for m in spec.mounts)
    assert {m.target for m in spec.mounts} == {"/workspace", "/aicd-input", "/aicd-db"}
    assert spec.image == images.TRIVY.ref
    payload = build_host_config(spec)
    assert lint_container_config(payload, allowed_bind_roots=[str(root)], require_image_digest=True) == []


# --- Применимость ------------------------------------------------------------------------------------


def test_applicability_markers() -> None:
    app = sg.detect_security_applicability(
        facts("src/app.py", "requirements.txt", "Dockerfile", "policy/a.rego", "k8s/deploy.yaml")
    )
    for name in ("semgrep-sast", "gitleaks-secrets", "trivy-vuln", "osv-scanner-sca", "trivy-config", "checkov-iac"):
        assert app[name].applicable, name
    assert app["conftest-policy"].applicable and app["syft-sbom"].applicable
    assert not app["bench-perf"].applicable and "no speed-up claim" in app["bench-perf"].reason
    assert not app["cosign-verify"].applicable and app["cosign-verify"].reason


def test_not_applicable_always_has_reason_and_markers() -> None:
    app = sg.detect_security_applicability(facts("README.md", "node_modules/x/index.js"))
    assert app["gitleaks-secrets"].applicable
    for name in ("semgrep-sast", "trivy-vuln", "osv-scanner-sca", "trivy-config", "checkov-iac", "conftest-policy"):
        assert not app[name].applicable and app[name].reason and app[name].markers, name
    assert not sg.detect_security_applicability(facts())["gitleaks-secrets"].applicable


def test_perf_and_sign_applicability_from_claims_and_policy() -> None:
    f = facts("bench/b.py")
    app = sg.detect_security_applicability(
        f,
        claims=[{"type": "implementation_completed", "summary": "Ускорил парсер в 2 раза"}],
        perf_definition_text="{}",
        signing_policy=sg.SigningPolicy("KEY", (sg.SignatureSubject("image", "app", "app"),)),
    )
    assert app["bench-perf"].applicable and app["cosign-verify"].applicable
    declared = sg.detect_security_applicability(f, declared_outputs=[{"kind": "benchmark", "path": "/output/b.json"}])
    assert declared["bench-perf"].applicable and "no .aicd/perf.json" in declared["bench-perf"].reason


# --- Пороги и вердикты -------------------------------------------------------------------------------


def test_security_thresholds() -> None:
    assert sg.security_threshold("SAST", "basic") == "critical"
    assert sg.security_threshold("SAST", "strict") == "high"
    assert sg.security_threshold("SCA", "critical") == "medium"
    assert sg.security_threshold("SAST", "unknown-profile") == "medium"  # fail-closed: как critical
    assert sg.security_threshold("SECRETS", "basic") == "info"
    assert sg.security_threshold("POLICY", "basic") == "medium"


def test_scanner_verdict_rules() -> None:
    semgrep = sample("semgrep.json")
    assert sg.evaluate_semgrep(0, semgrep, ctx("strict")).status == "FAIL"
    assert sg.evaluate_semgrep(0, semgrep, ctx("basic")).status == "PASS"  # high < critical
    crash = sg.evaluate_semgrep(2, semgrep, ctx())
    assert (crash.status, crash.error_code) == ("ERROR", "INFRA")
    garbage = sg.evaluate_semgrep(0, "Traceback ...", ctx())
    assert (garbage.status, garbage.error_code) == ("ERROR", "REPORT_UNPARSABLE")
    # Код «найдены секреты» без находок в отчёте — противоречие → ERROR.
    contradiction = sg.evaluate_gitleaks(1, "[]", ctx())
    assert contradiction.status == "ERROR"
    assert sg.evaluate_gitleaks(0, "[]", ctx("critical")).status == "PASS"
    assert sg.evaluate_gitleaks(1, sample("gitleaks.json"), ctx("basic")).status == "FAIL"
    # exit 0, но в отчёте секреты — всё равно FAIL (отчёт важнее кода выхода).
    assert sg.evaluate_gitleaks(0, sample("gitleaks.json"), ctx("basic")).status == "FAIL"


def test_sca_and_iac_verdicts() -> None:
    assert sg.evaluate_osv(1, sample("osv-scanner.json"), ctx("basic")).status == "FAIL"  # critical
    no_sources = sg.evaluate_osv(128, "", ctx())
    assert no_sources.status == "ERROR"
    assert sg.evaluate_osv(127, "", ctx()).status == "ERROR"
    assert sg.evaluate_trivy_vuln(1, "", ctx()).status == "ERROR"  # нет БД → ошибка trivy → ERROR
    assert sg.evaluate_trivy_vuln(0, sample("trivy-vuln.json"), ctx("basic")).status == "FAIL"
    checkov = sample("checkov.json")
    assert sg.evaluate_checkov(1, checkov, ctx("basic")).status == "FAIL"  # CKV_AWS_57 critical
    assert sg.evaluate_trivy_config(0, sample("trivy-config.json"), ctx("strict")).status == "FAIL"
    empty_trivy = json.dumps({"SchemaVersion": 2, "Results": []})
    passed = sg.evaluate_trivy_config(0, empty_trivy, ctx("critical"))
    assert passed.status == "PASS" and "none" in passed.message


def test_conftest_verdicts() -> None:
    report = sample("conftest.json")
    assert sg.evaluate_conftest(1, report, ctx()).status == "FAIL"
    assert sg.evaluate_conftest(0, report, ctx()).status == "FAIL"  # exit 0, но есть deny
    warn_only = json.dumps([{"filename": "/workspace/a.yaml", "warnings": [{"msg": "w"}], "failures": []}])
    assert sg.evaluate_conftest(0, warn_only, ctx()).status == "PASS"
    assert sg.evaluate_conftest(1, warn_only, ctx()).status == "ERROR"
    assert sg.evaluate_conftest(1, "", ctx()).status == "ERROR"  # ошибка загрузки политик/парсинга


def test_gate_config_change_in_diff_is_high_finding() -> None:
    clean = json.dumps({"results": [], "errors": [], "paths": {"scanned": ["/workspace/a.py"]}})
    f = facts("a.py", ".semgrepignore", changed=(".semgrepignore",))
    verdict = sg.evaluate_semgrep(0, clean, ctx("strict", f))
    assert verdict.status == "FAIL"
    assert [x.rule_id for x in verdict.findings] == [sg.GATE_CONFIG_CHANGED_RULE]
    assert sg.evaluate_semgrep(0, clean, ctx("strict", facts("a.py"))).status == "PASS"


def test_sbom_and_cosign_verdicts() -> None:
    sbom = sg.evaluate_syft(0, sample("sbom.cdx.json"), ctx())
    assert sbom.status == "PASS" and "CycloneDX" in sbom.message
    assert sg.evaluate_syft(0, "{}", ctx()).status == "ERROR"
    assert sg.evaluate_syft(1, sample("sbom.cdx.json"), ctx()).status == "ERROR"
    assert sg.evaluate_cosign(0, "", ctx(), stderr="Verified OK").status == "PASS"
    assert sg.evaluate_cosign(1, "", ctx(), stderr="Error: no signatures found").status == "FAIL"
    assert sg.evaluate_cosign(1, "", ctx(), stderr="Error: unexpected").status == "ERROR"
    assert sg.SECURITY_GATE_SPECS["cosign-verify"].run_evaluate(1, "", ctx()).status == "FAIL"


def test_bench_verdict_needs_definition_from_base() -> None:
    claims = [{"type": "other", "summary": "faster by 20%"}]
    missing = sg.evaluate_bench(0, sample("bench.json"), ctx(aicd_perf_claims=claims))
    assert missing.status == "FAIL" and "unverifiable" in missing.message
    definition = json.dumps({"command": ["python3", "bench/workload.py"], "repeat": 7})
    ok = sg.evaluate_bench(0, sample("bench.json"), ctx(aicd_perf_definition=definition, aicd_perf_claims=claims))
    assert ok.status == "PASS", ok
    too_much = [{"type": "other", "summary": "speed-up 90%"}]
    rejected = sg.evaluate_bench(
        0, sample("bench.json"), ctx(aicd_perf_definition=definition, aicd_perf_claims=too_much)
    )
    assert rejected.status == "FAIL"
    assert sg.evaluate_bench(1, "", ctx(aicd_perf_definition=definition)).status == "ERROR"


# --- Офлайн-БД ---------------------------------------------------------------------------------------


def test_trivy_db_status(tmp_path: Path) -> None:
    assert not sg.trivy_db_status(None, now=NOW).ready
    assert not sg.trivy_db_status(tmp_path, now=NOW).ready
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "trivy.db").write_bytes(b"x")
    (tmp_path / "db" / "metadata.json").write_text(
        json.dumps({"Version": 2, "UpdatedAt": "2026-11-08T06:39:21.614725497Z"})
    )
    fresh = sg.trivy_db_status(tmp_path, now=NOW)
    assert fresh.ready and fresh.updated_at is not None and fresh.as_json()["schema_version"] == 2
    stale = sg.trivy_db_status(tmp_path, now=NOW + timedelta(days=8))
    assert not stale.ready and "stale" in stale.reason


def test_osv_db_status(tmp_path: Path) -> None:
    assert not sg.osv_db_status(tmp_path, now=NOW).ready
    (tmp_path / "osv-scalibr" / "PyPI").mkdir(parents=True)
    (tmp_path / "osv-scalibr" / "PyPI" / "all.zip").write_bytes(b"PK")
    assert "manifest" in sg.osv_db_status(tmp_path, now=NOW).reason
    (tmp_path / sg.OSV_MANIFEST_FILE).write_text(json.dumps({"updated_at": "2026-11-07T00:00:00Z"}))
    status = sg.osv_db_status(tmp_path, now=NOW)
    assert status.ready and status.details["ecosystems"] == ["PyPI"]
    assert not sg.osv_db_status(tmp_path, now=NOW, max_age=timedelta(hours=1)).ready


# --- run_security_gate на поддельном runner'е --------------------------------------------------------


@dataclass
class FakeProcess:
    stdout: bytes
    stderr: bytes
    code: int
    ref: str = "fake:1"

    async def output(self) -> AsyncIterator[OutputChunk]:
        yield OutputChunk("stdout", self.stdout)
        yield OutputChunk("stderr", self.stderr)

    async def wait(self) -> int:
        return self.code

    async def terminate(self, grace_seconds: float = 15.0) -> None:
        return None


@dataclass
class FakeRunner:
    stdout: bytes = b""
    stderr: bytes = b""
    code: int = 0
    fail_start: bool = False
    specs: list[ContainerSpec] = field(default_factory=list)

    async def start(self, spec: ContainerSpec) -> FakeProcess:
        if self.fail_start:
            raise OSError("docker is down")
        self.specs.append(spec)
        return FakeProcess(self.stdout, self.stderr, self.code)

    async def is_alive(self, ref: str) -> bool:
        return False

    def owns(self, ref: str) -> bool:
        return True

    async def kill(self, ref: str) -> bool:
        return True


def _request(tmp_path: Path, **kw: object) -> sg.SecurityRunRequest:
    (tmp_path / "wt").mkdir(exist_ok=True)
    params: dict[str, object] = {
        "worktree": tmp_path / "wt",
        "input_dir": tmp_path / "in",
        "output_dir": tmp_path / "out",
        "facts": facts("a.py"),
    }
    params.update(kw)
    return sg.SecurityRunRequest(**params)  # type: ignore[arg-type]


async def test_run_security_gate_writes_inputs_and_report(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=sample("semgrep.json").encode())
    result = await sg.run_security_gate(runner, "semgrep-sast", _request(tmp_path))
    assert result.verdict.status == "FAIL" and result.exit_code == 0
    assert (tmp_path / "in" / "semgrep.yml").read_bytes() == sg.SEMGREP_RULES
    assert result.report is not None and result.report.path == tmp_path / "out" / "semgrep.json"
    assert json.loads(result.report.path.read_text())["results"]
    (spec,) = runner.specs
    assert spec.argv == sg.semgrep_argv(ctx()) and spec.network == "none"
    evidence = result.evidence()
    assert evidence["report"]["sha256"] == result.report.sha256 and evidence["findings"]["high"] >= 1


async def test_run_security_gate_errors_never_pass(tmp_path: Path) -> None:
    down = await sg.run_security_gate(FakeRunner(fail_start=True), "gitleaks-secrets", _request(tmp_path))
    assert (down.verdict.status, down.verdict.error_code) == ("ERROR", "INFRA")
    killed = await sg.run_security_gate(FakeRunner(code=-9), "gitleaks-secrets", _request(tmp_path))
    assert killed.verdict.status == "ERROR"
    no_db = await sg.run_security_gate(
        FakeRunner(stdout=sample("trivy-vuln.json").encode()), "trivy-vuln", _request(tmp_path)
    )
    assert no_db.verdict.status == "ERROR" and "DB_UNAVAILABLE" in no_db.verdict.message
    no_baseline = await sg.run_security_gate(FakeRunner(), "bench-perf", _request(tmp_path))
    assert (no_baseline.verdict.status, no_baseline.verdict.error_code) == ("ERROR", "NO_BASE_COMMIT")
    crash = await sg.run_security_gate(FakeRunner(code=2, stderr=b"panic: boom"), "checkov-iac", _request(tmp_path))
    assert crash.verdict.status == "ERROR" and "panic: boom" in crash.verdict.message


async def test_run_signature_gate_aggregates(tmp_path: Path) -> None:
    policy = sg.SigningPolicy(
        "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
        (sg.SignatureSubject("blob", "a", "a.bin", "a.bin.sigstore.json"), sg.SignatureSubject("image", "b", "b")),
    )
    ok, results = await sg.run_signature_gate(FakeRunner(stderr=b"Verified OK"), policy, _request(tmp_path))
    assert ok.status == "PASS" and len(results) == 2
    assert (tmp_path / "in" / "cosign.pub").read_text().startswith("-----BEGIN PUBLIC KEY")
    bad, _ = await sg.run_signature_gate(
        FakeRunner(code=1, stderr=b"Error: no signatures found"), policy, _request(tmp_path)
    )
    assert bad.status == "FAIL"
    empty, _ = await sg.run_signature_gate(FakeRunner(), sg.SigningPolicy("k", ()), _request(tmp_path))
    assert empty.status == "ERROR"


# --- Окружение gate в контейнере (VerificationEngine._spec_for) ------------------------------------------------


def test_declared_gate_env_is_static_offline_flags_only() -> None:
    assert declared_gate_env(GATE_SPECS["semgrep-sast"]) == {
        "SEMGREP_SEND_METRICS": "off",
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
    }
    assert declared_gate_env(GATE_SPECS["scope"]) == {}  # без декларации — ничего (и ничего с хоста)
    for name in sg.SECURITY_GATES:  # все декларации платформы проходят проверку
        declared_gate_env(GATE_SPECS[name])


@pytest.mark.parametrize(
    "bad",
    [
        {"HOME": "/root"},
        {"PATH": "/opt/evil"},
        {"LD_PRELOAD": "/x.so"},
        {"GITHUB_TOKEN": "x"},
        {"AWS_SECRET_ACCESS_KEY": "x"},
        {"lower_case": "1"},
        {"OK_NAME": "a\nb"},
        {"OK_NAME": "x" * 300},
    ],
)
def test_declared_gate_env_rejects_reserved_secret_like_or_malformed(
    bad: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sg.SECURITY_GATES["semgrep-sast"]
    monkeypatch.setitem(sg.SECURITY_GATES, "semgrep-sast", replace(original, env=bad))
    with pytest.raises(GateEnvError):
        declared_gate_env(GATE_SPECS["semgrep-sast"])
