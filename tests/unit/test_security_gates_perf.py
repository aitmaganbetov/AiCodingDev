"""Unit (P6-06): определение бенчмарка, распознавание заявлений об ускорении, статистика и вердикт PERF;
stdlib-измеритель `bench_runner` исполняется локально на крошечной команде."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aicd_verification import security_gates as sg
from aicd_verification.tools import perf


def test_parse_definition_defaults_and_limits() -> None:
    d = perf.parse_definition('{"command": ["python3", "bench.py"]}')
    assert (d.command, d.repeat, d.warmup, d.min_improvement_percent) == (("python3", "bench.py"), 7, 1, 5.0)
    assert d.budget_seconds == (7 + 1) * 2 * 60 + 30
    for bad in (
        "[]",
        "not json",
        '{"command": "python3 bench.py"}',  # строка вместо argv — shell не допускается
        '{"command": []}',
        '{"command": ["a"], "repeat": 3}',  # N < 5
        '{"command": ["a"], "repeat": true}',
        '{"command": ["a"], "min_improvement_percent": 0}',
    ):
        with pytest.raises(perf.PerfDefinitionError):
            perf.parse_definition(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ускорил парсер на 30%", 30.0),
        ("2x faster", 50.0),
        ("в 4 раза быстрее", 75.0),
        ("performance improved", None),
        ("100% faster", None),
    ],
)
def test_parse_claimed_improvement(text: str, expected: float | None) -> None:
    assert perf.parse_claimed_improvement(text) == expected


def test_speedup_claims() -> None:
    claims = perf.speedup_claims(
        [
            {"type": "implementation_completed", "summary": "Оптимизировал запросы, ускорение на 25%"},
            {"type": "tests_added", "summary": "добавлены тесты"},
        ],
        [{"kind": "benchmark", "path": "/output/bench.json"}, {"kind": "plan", "path": "/output/p.md"}],
    )
    assert [c.source for c in claims] == ["claim:implementation_completed", "declared_output:benchmark"]
    assert claims[0].claimed_improvement_percent == 25.0


def _report(base: list[float], cand: list[float], **over: object) -> str:
    data: dict[str, object] = {
        "harness": "aicd-bench/1",
        "error": None,
        "repeat": len(base),
        "warmup": 1,
        "command": ["x"],
        "environment": {"cpu_count": 2},
        "variants": {
            "baseline": {"samples_ns": [int(v * 1e6) for v in base], "exit_codes": [0] * len(base)},
            "candidate": {"samples_ns": [int(v * 1e6) for v in cand], "exit_codes": [0] * len(cand)},
        },
    }
    data.update(over)
    return json.dumps(data)


DEF = perf.BenchDefinition(command=("x",), repeat=5, min_improvement_percent=10)


def test_evaluate_pass_requires_threshold_and_separation() -> None:
    ok = perf.evaluate_bench_report(_report([100, 101, 99, 102, 100], [60, 61, 59, 60, 62]), DEF)
    assert ok.status == "PASS"
    assert ok.evidence is not None and ok.evidence["improvement_percent"] == pytest.approx(40.0, abs=0.5)
    assert ok.evidence["baseline"]["median_ms"] == 100.0 and "variance_ms2" in ok.evidence["candidate"]
    small = perf.evaluate_bench_report(_report([100] * 5, [95, 95, 96, 94, 95]), DEF)
    assert small.status == "FAIL"  # 5% < 10%
    noisy = perf.evaluate_bench_report(_report([60, 80, 100, 120, 140], [50, 70, 85, 110, 130]), DEF)
    assert noisy.status == "FAIL" and noisy.evidence is not None and not noisy.evidence["iqr_separated"]
    claim = perf.PerfClaim("claim:other", "2x", 50.0)
    stricter = perf.evaluate_bench_report(_report([100] * 5, [60, 61, 59, 60, 62]), DEF, [claim])
    assert stricter.status == "FAIL" and "required ≥ 50.0%" in stricter.message


def test_evaluate_errors() -> None:
    assert perf.evaluate_bench_report("", DEF).status == "ERROR"
    assert perf.evaluate_bench_report('{"harness": "aicd-bench/1", "error": "bad spec"}', DEF).status == "ERROR"
    too_few = perf.evaluate_bench_report(_report([100] * 3, [50] * 3), DEF)
    assert too_few.status == "ERROR"
    base_fails = json.loads(_report([100] * 5, [50] * 5))
    base_fails["variants"]["baseline"]["exit_codes"][0] = 1
    assert perf.evaluate_bench_report(json.dumps(base_fails), DEF).status == "ERROR"
    cand_fails = json.loads(_report([100] * 5, [50] * 5))
    cand_fails["variants"]["candidate"]["exit_codes"][2] = 3
    assert perf.evaluate_bench_report(json.dumps(cand_fails), DEF).status == "FAIL"


def test_bench_runner_harness_runs_locally(tmp_path: Path) -> None:
    base, cand = tmp_path / "base", tmp_path / "cand"
    for d in (base, cand):
        d.mkdir()
    spec = perf.BenchDefinition(command=(sys.executable, "-c", "pass"), repeat=5, warmup=1).spec(
        {"baseline": str(base), "candidate": str(cand)}
    )
    spec_path = tmp_path / "bench.json"
    spec_path.write_text(json.dumps(spec))
    res = subprocess.run(
        [sys.executable, "-I", "-S", "-c", sg.BENCH_SOURCE, str(spec_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    report = json.loads(res.stdout)
    assert report["order"][:4] == ["baseline", "candidate", "candidate", "baseline"]  # ABBA
    assert len(report["variants"]["candidate"]["samples_ns"]) == 5
    assert report["variants"]["baseline"]["warmup_exit_codes"] == [0]
    bad = subprocess.run(
        [sys.executable, "-I", "-S", "-c", sg.BENCH_SOURCE, str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert bad.returncode == 2 and json.loads(bad.stdout)["error"]
