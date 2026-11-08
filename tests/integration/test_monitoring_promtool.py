"""P8-08: «тестовый алерт срабатывает» — `promtool check rules` и `promtool test rules` в официальном образе
prom/prometheus, закреплённом по digest (тот же, что в compose профиля observability).

Пропускается, если Docker или образ недоступны (образы тестом не скачиваются:
`docker pull prom/prometheus:v3.15.1@sha256:…` — см. docs/runbooks/observability.md).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "infrastructure" / "monitoring"
DOCKER_SOCK = "/var/run/docker.sock"
pytestmark = pytest.mark.docker


def _image(service: str) -> str:
    compose = yaml.safe_load((ROOT / "infrastructure" / "docker" / "compose.yaml").read_text())
    return str(compose["services"][service]["image"])


def _image_available(ref: str) -> bool:
    if not Path(DOCKER_SOCK).exists() or shutil.which("docker") is None:
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            return c.get("/_ping").status_code == 200 and c.get(f"/images/{ref}/json").status_code == 200
    except httpx.HTTPError:
        return False


PROMETHEUS = _image("prometheus")
ALERTMANAGER = _image("alertmanager")


def _run(image: str, entrypoint: str, *args: str, root: Path = MONITORING) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "-v", f"{root}:/monitoring:ro", "-w", "/monitoring",
            "--entrypoint", entrypoint, image, *args,
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


@pytest.mark.skipif(not _image_available(PROMETHEUS), reason=f"Docker daemon / образ {PROMETHEUS} недоступны")
def test_promtool_rules_are_valid_and_alerts_fire() -> None:
    check = _run(PROMETHEUS, "promtool", "check", "rules", "prometheus/rules/aicd-alerts.yml")
    assert check.returncode == 0, check.stdout + check.stderr
    tested = _run(PROMETHEUS, "promtool", "test", "rules", "prometheus/tests/aicd-alerts.test.yml")
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "SUCCESS" in tested.stdout


@pytest.mark.skipif(not _image_available(PROMETHEUS), reason=f"Docker daemon / образ {PROMETHEUS} недоступны")
def test_promtool_detects_broken_alert_expectation(tmp_path: Path) -> None:
    """Контроль самого теста: неверное ожидание (алерт не должен сработать) promtool отвергает."""
    spec = yaml.safe_load((MONITORING / "prometheus" / "tests" / "aicd-alerts.test.yml").read_text())
    broken = spec["tests"][0]
    broken["alert_rule_test"] = [{"eval_time": "15m", "alertname": "AicdLoginFailuresSpike", "exp_alerts": []}]
    spec["tests"] = [broken]
    shutil.copytree(MONITORING / "prometheus" / "rules", tmp_path / "prometheus" / "rules")
    (tmp_path / "prometheus" / "tests").mkdir()
    (tmp_path / "prometheus" / "tests" / "broken.test.yml").write_text(yaml.safe_dump(spec, allow_unicode=True))
    tmp_path.chmod(0o755)
    result = _run(PROMETHEUS, "promtool", "test", "rules", "prometheus/tests/broken.test.yml", root=tmp_path)
    assert result.returncode != 0
    assert "AicdLoginFailuresSpike" in result.stdout + result.stderr


@pytest.mark.skipif(not _image_available(ALERTMANAGER), reason=f"Docker daemon / образ {ALERTMANAGER} недоступны")
def test_alertmanager_config_is_valid() -> None:
    result = _run(ALERTMANAGER, "amtool", "check-config", "alertmanager/alertmanager.yml")
    assert result.returncode == 0, result.stdout + result.stderr
