"""PERF gate: заявления об ускорении, определение бенчмарка и статистический вердикт (P6-06).

Применимость: gate выполняется, только если агент заявил ускорение (QUALITY_GATES §3.1, §5.1).
Схема AgentResult 1.0 не имеет отдельного типа claim «ускорение», поэтому заявление распознаётся
детерминированно: claim любого типа, чей `summary` говорит об ускорении/производительности
(`speed-up`, `faster`, `ускор…`, `производительн…`, `latency`, `throughput`), либо
`declared_outputs` с `kind = benchmark`. Величина заявления («на 30%», «2x», «в 2 раза») становится
порогом, если она строже порога определения.

Бенчмарк описывает проект в `.aicd/perf.json` (команда argv без shell, N повторов, порог). Определение
читается **из base commit**: агент не может ослабить измерение тем же diff, который заявляет ускорение.
Нет определения на base → заявление непроверяемо → FAIL (никогда PASS).

Вердикт (QUALITY_GATES §5.1 PERF): baseline (base commit) и candidate (commit задачи) измеряются в одном
контейнере с чередованием, N ≥ 5. PASS ⇔ все прогоны успешны, улучшение медианы ≥ порога и
интерквартильные интервалы не пересекаются (Q1 baseline > Q3 candidate) — улучшение больше разброса.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

PERF_DEFINITION_PATH = ".aicd/perf.json"
MIN_REPEAT = 5
MAX_REPEAT = 50
DEFAULT_REPEAT = 7
DEFAULT_MIN_IMPROVEMENT_PERCENT = 5.0
MAX_COMMAND_ARGS = 64
MAX_ARG_LENGTH = 1024

_CLAIM_RE = re.compile(
    r"(?i)(speed[\s-]?up|faster|performance|perf\b|latency|throughput|optimi[sz]|"
    r"ускор|быстрее|производительн|оптимизир|латентн|пропускн)"
)
_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_TIMES_RE = re.compile(r"(?i)(?:в\s*)?(\d{1,3}(?:[.,]\d+)?)\s*(?:x|×|раз[а]?)\b")


class PerfDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class BenchDefinition:
    command: tuple[str, ...]
    repeat: int = DEFAULT_REPEAT
    warmup: int = 1
    run_timeout_seconds: int = 60
    min_improvement_percent: float = DEFAULT_MIN_IMPROVEMENT_PERCENT

    def spec(self, variants: Mapping[str, str]) -> dict[str, Any]:
        """Вход `bench_runner` (bench.json)."""
        return {
            "command": list(self.command),
            "repeat": self.repeat,
            "warmup": self.warmup,
            "run_timeout_seconds": self.run_timeout_seconds,
            "variants": dict(variants),
        }

    @property
    def budget_seconds(self) -> int:
        """Верхняя граница длительности измерения (для timeout gate)."""
        return (self.repeat + self.warmup) * 2 * self.run_timeout_seconds + 30


def parse_definition(text: str) -> BenchDefinition:
    """`.aicd/perf.json` → `BenchDefinition`; нарушение формата — `PerfDefinitionError`."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PerfDefinitionError(f"{PERF_DEFINITION_PATH} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PerfDefinitionError(f"{PERF_DEFINITION_PATH} must be a JSON object")
    command = data.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > MAX_COMMAND_ARGS
        or not all(isinstance(a, str) and a and len(a) <= MAX_ARG_LENGTH and "\x00" not in a for a in command)
    ):
        raise PerfDefinitionError("command must be a non-empty argv list of strings (no shell)")

    def _number(key: str, default: float, lo: float, hi: float) -> float:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
            raise PerfDefinitionError(f"{key} must be a number in [{lo}, {hi}]")
        return float(value)

    return BenchDefinition(
        command=tuple(command),
        repeat=int(_number("repeat", DEFAULT_REPEAT, MIN_REPEAT, MAX_REPEAT)),
        warmup=int(_number("warmup", 1, 0, 5)),
        run_timeout_seconds=int(_number("run_timeout_seconds", 60, 1, 600)),
        min_improvement_percent=_number("min_improvement_percent", DEFAULT_MIN_IMPROVEMENT_PERCENT, 0.5, 95),
    )


# --- Заявления агента ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PerfClaim:
    source: str  # claim:<type> | declared_output:benchmark
    summary: str
    claimed_improvement_percent: float | None


def parse_claimed_improvement(text: str) -> float | None:
    """«на 30%» → 30.0; «2x»/«в 2 раза» → 50.0 (время в 2 раза меньше). Нет числа → None."""
    times = _TIMES_RE.search(text)
    if times:
        factor = float(times.group(1).replace(",", "."))
        if factor > 1:
            return round((1 - 1 / factor) * 100, 2)
    pct = _PERCENT_RE.search(text)
    if pct:
        value = float(pct.group(1).replace(",", "."))
        if 0 < value < 100:
            return value
    return None


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def speedup_claims(claims: Iterable[Any] = (), declared_outputs: Iterable[Any] = ()) -> list[PerfClaim]:
    """Заявления об ускорении из AgentResult (`Claim`-объекты или dict)."""
    found: list[PerfClaim] = []
    for claim in claims:
        summary = str(_field(claim, "summary") or "")
        if summary and _CLAIM_RE.search(summary):
            found.append(PerfClaim(f"claim:{_field(claim, 'type')}", summary[:500], parse_claimed_improvement(summary)))
    for out in declared_outputs:
        if _field(out, "kind") == "benchmark":
            found.append(PerfClaim("declared_output:benchmark", str(_field(out, "path") or "")[:500], None))
    return found


# --- Статистика и вердикт ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleStats:
    n: int
    median_ms: float
    mean_ms: float
    stdev_ms: float
    variance_ms2: float
    q1_ms: float
    q3_ms: float
    min_ms: float
    max_ms: float

    @property
    def cv(self) -> float:
        return self.stdev_ms / self.mean_ms if self.mean_ms else 0.0

    def as_json(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "median_ms": round(self.median_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "stdev_ms": round(self.stdev_ms, 3),
            "variance_ms2": round(self.variance_ms2, 3),
            "q1_ms": round(self.q1_ms, 3),
            "q3_ms": round(self.q3_ms, 3),
            "min_ms": round(self.min_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "cv": round(self.cv, 4),
        }


def sample_stats(samples_ns: Iterable[int]) -> SampleStats:
    values = [v / 1_000_000 for v in samples_ns]
    if len(values) < 2:
        raise ValueError("at least two samples are required")
    q1, _q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return SampleStats(
        n=len(values),
        median_ms=statistics.median(values),
        mean_ms=statistics.fmean(values),
        stdev_ms=statistics.stdev(values),
        variance_ms2=statistics.variance(values),
        q1_ms=q1,
        q3_ms=q3,
        min_ms=min(values),
        max_ms=max(values),
    )


@dataclass(frozen=True)
class PerfVerdict:
    status: str  # PASS | FAIL | ERROR
    message: str
    error_code: str | None = None
    evidence: Mapping[str, Any] | None = None


def evaluate_bench_report(
    text: str,
    definition: BenchDefinition,
    claims: Iterable[PerfClaim] = (),
) -> PerfVerdict:
    """Вердикт по JSON `bench_runner`. Невалидный отчёт/сбой baseline → ERROR; сбой candidate или
    недостаточное/статистически незначимое улучшение → FAIL."""
    try:
        report = json.loads(text.strip().splitlines()[-1]) if text.strip() else None
    except ValueError:
        report = None
    if not isinstance(report, dict) or report.get("harness") != "aicd-bench/1":
        return PerfVerdict("ERROR", "benchmark harness report is missing or unparsable", "REPORT_UNPARSABLE")
    if report.get("error"):
        return PerfVerdict("ERROR", f"benchmark harness error: {str(report['error'])[:300]}", "INFRA")
    variants = report.get("variants") or {}
    base, cand = variants.get("baseline"), variants.get("candidate")
    if not isinstance(base, dict) or not isinstance(cand, dict):
        return PerfVerdict("ERROR", "benchmark report lacks baseline/candidate", "REPORT_UNPARSABLE")
    base_codes = list(base.get("exit_codes") or []) + list(base.get("warmup_exit_codes") or [])
    cand_codes = list(cand.get("exit_codes") or []) + list(cand.get("warmup_exit_codes") or [])
    if any(c != 0 for c in base_codes):
        return PerfVerdict("ERROR", "benchmark command fails on the base commit (no baseline)", "INFRA")
    if any(c != 0 for c in cand_codes):
        return PerfVerdict("FAIL", "benchmark command fails on the candidate commit")
    try:
        bs = sample_stats(base.get("samples_ns") or [])
        cs = sample_stats(cand.get("samples_ns") or [])
    except (TypeError, ValueError, statistics.StatisticsError):
        return PerfVerdict("ERROR", "benchmark report has too few samples", "REPORT_UNPARSABLE")
    if min(bs.n, cs.n) < MIN_REPEAT:
        return PerfVerdict("ERROR", f"benchmark has {min(bs.n, cs.n)} samples (< {MIN_REPEAT})", "REPORT_UNPARSABLE")
    claimed = [c.claimed_improvement_percent for c in claims if c.claimed_improvement_percent is not None]
    required = max([definition.min_improvement_percent, *claimed])
    improvement = (1 - cs.median_ms / bs.median_ms) * 100 if bs.median_ms > 0 else 0.0
    separated = bs.q1_ms > cs.q3_ms
    evidence = {
        "baseline": bs.as_json(),
        "candidate": cs.as_json(),
        "improvement_percent": round(improvement, 2),
        "required_improvement_percent": round(required, 2),
        "iqr_separated": separated,
        "claims": [
            {"source": c.source, "summary": c.summary, "claimed_improvement_percent": c.claimed_improvement_percent}
            for c in claims
        ],
        "repeat": report.get("repeat"),
        "warmup": report.get("warmup"),
        "command": report.get("command"),
        "environment": report.get("environment"),
        "method": "interleaved ABBA, median of N, IQR separation",
    }
    summary = (
        f"median {bs.median_ms:.2f} ms → {cs.median_ms:.2f} ms ({improvement:+.1f}%, required ≥ {required:.1f}%,"
        f" n={cs.n}, IQR {'separated' if separated else 'overlapping'})"
    )
    if improvement >= required and separated:
        return PerfVerdict("PASS", f"speed-up confirmed: {summary}", evidence=evidence)
    return PerfVerdict("FAIL", f"speed-up claim not confirmed: {summary}", evidence=evidence)
