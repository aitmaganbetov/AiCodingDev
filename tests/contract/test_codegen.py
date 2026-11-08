"""P1-18: сгенерированные Pydantic-модели контрактов не расходятся с JSON Schema (drift-check, ADR-0013)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generated_contract_models_are_up_to_date() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_contract_models.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"gen_contract_models.py --check:\n{result.stdout}\n{result.stderr}"
