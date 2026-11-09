"""P9-10: скрипты, которые платформа исполняет ВНУТРИ verification-контейнера как исходный текст
(`python3 -I -S -c <source>`): драйвер COVERAGE и измеритель PERF. Здесь они исполняются в процессе теста на
маленьких фикстурах — проверяется их собственная логика (отчёт только этого прогона — FP-08; формат маркера;
чередование ABBA, коды выхода, тайм-аут, отказ на неверной спецификации).
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aicd_verification import coverage_driver
from aicd_verification.runners import COVERAGE_DRIVER, COVERAGE_MARKER
from aicd_verification.security_gates import BENCH_SOURCE
from aicd_verification.tools import bench_runner


def _marker(out: str, nonce: str) -> Any:
    line = next(x for x in out.splitlines() if x.startswith(f"{COVERAGE_MARKER} {nonce} "))
    return json.loads(line.split(" ", 2)[2])


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [k for k in os.environ if k.startswith("COV_CORE_")]:  # не наследовать сбор покрытия pytest-cov
        monkeypatch.delenv(name)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef unused():\n    return 1\n")
    (tmp_path / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_sources_shipped_to_container_are_these_modules() -> None:
    assert Path(coverage_driver.__file__).read_text(encoding="utf-8") == COVERAGE_DRIVER
    assert Path(bench_runner.__file__).read_text(encoding="utf-8") == BENCH_SOURCE


def test_coverage_driver_reports_fresh_coverage_of_this_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = coverage_driver.main(["driver", "n0nce"])
    out = capsys.readouterr().out
    assert code == 0
    summary = _marker(out, "n0nce")
    assert summary is not None and summary["tool"]
    assert summary["files"]["calc.py"] == [3, 4], "add покрыта, unused — нет"
    covered, statements = summary["totals"]
    assert 0 < covered < statements


def test_coverage_driver_propagates_test_failure(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "test_calc.py").write_text("def test_fails():\n    assert False\n")
    code = coverage_driver.main(["driver", "x"])
    assert code != 0, "провал тестов не маскируется кодом выхода драйвера"
    assert _marker(capsys.readouterr().out, "x") is not None


def test_coverage_driver_rejects_planted_stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FP-08: отчёт, не созданный этим прогоном (mtime раньше старта), не засчитывается."""
    planted = tmp_path / "out"
    planted.mkdir()
    report = planted / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {"x.py": {"summary": {"covered_lines": 9, "num_statements": 9}}},
                "totals": {"covered_lines": 9, "num_statements": 9},
            }
        )
    )
    old = time.time() - 3600
    os.utime(report, (old, old))
    monkeypatch.setattr(
        "aicd_verification.coverage_driver.tempfile", SimpleNamespace(mkdtemp=lambda prefix: str(planted))
    )
    monkeypatch.setattr(
        "aicd_verification.coverage_driver.subprocess", SimpleNamespace(call=lambda cmd, env: 0)
    )  # pytest «прошёл», отчёта не создал
    assert coverage_driver.main(["driver", "s"]) == 0
    assert _marker(capsys.readouterr().out, "s") is None


def test_coverage_driver_unparsable_report_is_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(
        "aicd_verification.coverage_driver.tempfile", SimpleNamespace(mkdtemp=lambda prefix: str(out_dir))
    )

    def fake_pytest(cmd: list[str], env: dict[str, str]) -> int:
        assert env["COVERAGE_FILE"].startswith(str(out_dir)), "данные coverage — во временном каталоге"
        (out_dir / "coverage.json").write_text("{not json")
        return 0

    monkeypatch.setattr("aicd_verification.coverage_driver.subprocess", SimpleNamespace(call=fake_pytest))
    coverage_driver.main(["driver"])  # без nonce
    assert _marker(capsys.readouterr().out, "") is None


def _spec(tmp_path: Path, command: list[str], **extra: Any) -> str:
    a, b = tmp_path / "baseline", tmp_path / "candidate"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    spec = {
        "command": command,
        "repeat": 3,
        "warmup": 1,
        "run_timeout_seconds": 20,
        "variants": {"baseline": str(a), "candidate": str(b)},
        **extra,
    }
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(spec))
    return str(path)


def _report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return result


def test_bench_runner_measures_abba_pairs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = "import os, sys; sys.exit(0 if os.path.basename(os.getcwd()) in ('baseline', 'candidate') else 3)"
    assert bench_runner.main(_spec(tmp_path, [sys.executable, "-c", script])) == 0
    report = _report(capsys)
    assert report["harness"] == "aicd-bench/1" and report["error"] is None
    assert report["order"] == ["baseline", "candidate", "candidate", "baseline", "baseline", "candidate"]
    for name in ("baseline", "candidate"):
        v = report["variants"][name]
        assert len(v["samples_ns"]) == 3 and all(s > 0 for s in v["samples_ns"])
        assert v["exit_codes"] == [0, 0, 0] and v["warmup_exit_codes"] == [0] and v["stderr_tail"] == ""
    assert report["environment"]["clock"] == "time.perf_counter_ns"


def test_bench_runner_records_failures_and_timeouts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    failing = textwrap.dedent("import sys; sys.stderr.write('boom'); sys.exit(4)")
    assert bench_runner.main(_spec(tmp_path, [sys.executable, "-c", failing], repeat=1, warmup=0)) == 0
    v = _report(capsys)["variants"]["candidate"]
    assert v["exit_codes"] == [4] and v["stderr_tail"] == "boom"
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    assert bench_runner.main(_spec(tmp_path, slow, repeat=1, warmup=0, run_timeout_seconds=0.2)) == 0
    v = _report(capsys)["variants"]["baseline"]
    assert v["exit_codes"] == [-1] and v["stderr_tail"] == "timeout"
    assert bench_runner.main(_spec(tmp_path, ["/nonexistent/aicd-bench-tool"], repeat=1, warmup=0)) == 0
    v = _report(capsys)["variants"]["baseline"]
    assert v["exit_codes"] == [-2] and "FileNotFoundError" in v["stderr_tail"]


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ("not json", "invalid bench spec: JSONDecodeError"),
        ({"repeat": 1, "variants": {}}, "invalid bench spec: KeyError"),
        ({"command": [], "repeat": 1, "variants": {"a": "/"}}, "invalid bench spec: empty command/variants"),
        ({"command": ["true"], "repeat": 0, "variants": {"a": "/"}}, "invalid bench spec: empty command/variants"),
        ({"command": ["true"], "repeat": 1, "variants": {"a": "/nonexistent-aicd"}}, "variant a: directory"),
    ],
)
def test_bench_runner_rejects_invalid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spec: Any, error: str
) -> None:
    path = tmp_path / "bench.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    assert bench_runner.main(str(path)) == 2
    report = _report(capsys)
    assert report["harness"] == "aicd-bench/1" and report["error"].startswith(error)
    assert bench_runner.main(str(tmp_path / "missing.json")) == 2
    assert _report(capsys)["error"] == "invalid bench spec: FileNotFoundError"
