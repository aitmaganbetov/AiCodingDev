#!/usr/bin/env python3
"""Генерация Pydantic v2 моделей из JSON Schema контрактов (ADR-0013, P1-18).

`--check` — сгенерировать во временный каталог и сравнить с закоммиченными файлами (drift-check CI).
Источник — packages/contracts/schemas/<область>/<версия>/*.schema.json,
результат — packages/contracts/aicd_contracts/generated/<область>_v<версия>/<имя>.py.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "packages" / "contracts" / "schemas"
OUT = ROOT / "packages" / "contracts" / "aicd_contracts" / "generated"
# Области, для которых модели нужны коду; остальные добавляются по мере появления потребителей.
AREAS = ["cli/1.0"]


def _module_name(schema: Path) -> str:
    return schema.name.removesuffix(".schema.json").replace("-", "_")


def generate(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "__init__.py").write_text('"""Сгенерировано scripts/gen_contract_models.py — не редактировать."""\n')
    for area in AREAS:
        pkg = target / area.replace("/", "_v").replace(".", "_")
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text('"""Сгенерировано scripts/gen_contract_models.py — не редактировать."""\n')
        for schema in sorted((SCHEMAS / area).glob("*.schema.json")):
            os.environ.setdefault("PYTHONWARNINGS", "ignore::FutureWarning")
            subprocess.run(  # noqa: S603 — фиксированный argv
                [
                    sys.executable,
                    "-m",
                    "datamodel_code_generator",
                    "--input",
                    str(schema),
                    "--input-file-type",
                    "jsonschema",
                    "--output",
                    str(pkg / f"{_module_name(schema)}.py"),
                    "--output-model-type",
                    "pydantic_v2.BaseModel",
                    "--target-python-version",
                    "3.12",
                    "--use-standard-collections",
                    "--use-union-operator",
                    "--use-annotated",
                    "--field-constraints",
                    "--use-double-quotes",
                    "--disable-timestamp",
                    "--enum-field-as-literal",
                    "all",
                    "--use-schema-description",
                    "--formatters",
                    "black",
                ],
                check=True,
            )
    # Единое форматирование по конфигу проекта независимо от каталога вывода.
    cfg = str(ROOT / "pyproject.toml")
    subprocess.run([sys.executable, "-m", "ruff", "check", "--config", cfg, "--fix", "-q", str(target)], check=False)  # noqa: S603
    subprocess.run([sys.executable, "-m", "ruff", "format", "--config", cfg, "-q", str(target)], check=True)  # noqa: S603


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        if OUT.exists():
            shutil.rmtree(OUT)
        generate(OUT)
        print(f"Модели сгенерированы в {OUT.relative_to(ROOT)}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "generated"
        generate(fresh)
        diff = filecmp.dircmp(fresh, OUT)
        problems: list[str] = []

        def walk(d: filecmp.dircmp[str], prefix: str = "") -> None:
            problems.extend(f"{prefix}{n}" for n in d.left_only + d.right_only + d.diff_files)
            for name, sub in d.subdirs.items():
                if name != "__pycache__":
                    walk(sub, f"{prefix}{name}/")

        walk(diff)
        problems = [p for p in problems if "__pycache__" not in p]
        if problems:
            print("Сгенерированные модели расходятся со схемами:", *problems, sep="\n  ", file=sys.stderr)
            return 1
    print("OK: сгенерированные модели соответствуют схемам")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
