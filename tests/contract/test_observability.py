"""P8-08: дашборды Grafana и правила алертов ссылаются только на существующие метрики кода (и их метки);
образы профиля observability закреплены по digest; `/metrics` не публикуется reverse-proxy."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client.metrics import MetricWrapperBase

from aicd_platform import metrics

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "infrastructure" / "monitoring"
RULES = MONITORING / "prometheus" / "rules" / "aicd-alerts.yml"
RULE_TESTS = MONITORING / "prometheus" / "tests" / "aicd-alerts.test.yml"
DASHBOARDS = sorted((MONITORING / "grafana" / "dashboards").glob("*.json"))
COMPOSE = ROOT / "infrastructure" / "docker" / "compose.yaml"

# Синтетические ряды Prometheus (не из кода приложения).
PROMETHEUS_BUILTIN = {"up"}
# Метки, добавляемые Prometheus при скрейпе/relabel, допустимые в выражениях.
TARGET_LABELS = {"job", "instance", "worker", "le"}

_SELECTOR = re.compile(r"\b(aicd_[a-z0-9_]+|up)\b(\{[^}]*\})?")
_LABEL = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)")
_BY = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")


def _families() -> dict[str, set[str]]:
    """Имя ряда (включая _total/_bucket/_sum/_count) → допустимые метки."""
    out: dict[str, set[str]] = {}
    for value in vars(metrics).values():
        if not isinstance(value, MetricWrapperBase):
            continue
        name = value._name  # имя семейства с namespace
        labels = set(value._labelnames)
        mtype = value._type
        names = {name}
        if mtype == "counter":
            names = {name if name.endswith("_total") else name + "_total"}
        elif mtype == "histogram":
            names = {name + s for s in ("_bucket", "_sum", "_count")}
            labels = labels | {"le"}
        for n in names:
            out[n] = labels
    return out


def _expressions() -> list[tuple[str, str]]:
    exprs: list[tuple[str, str]] = []
    for group in yaml.safe_load(RULES.read_text())["groups"]:
        for rule in group["rules"]:
            exprs.append((f"alert {rule['alert']}", rule["expr"]))
    for path in DASHBOARDS:
        for panel in json.loads(path.read_text())["panels"]:
            for target in panel.get("targets", []):
                exprs.append((f"{path.name}: {panel['title']}", target["expr"]))
    return exprs


def test_registry_is_consistent_with_exposition() -> None:
    families = _families()
    exposed = metrics.metric_names()
    assert len(families) >= 40
    assert set(families) <= exposed | {n for n in families if n.endswith(("_bucket", "_sum", "_count"))}


@pytest.mark.parametrize(("where", "expr"), _expressions(), ids=lambda v: str(v)[:60])
def test_expressions_reference_existing_metrics_and_labels(where: str, expr: str) -> None:
    families = _families()
    found = _SELECTOR.findall(expr)
    assert found, f"{where}: выражение без метрик приложения: {expr}"
    used: set[str] = set()
    for name, selector in found:
        if name in PROMETHEUS_BUILTIN:
            allowed = TARGET_LABELS
        else:
            assert name in families, f"{where}: метрики {name} нет в реестре aicd_platform.metrics"
            allowed = families[name] | TARGET_LABELS
            used |= families[name]
        for label in _LABEL.findall(selector):
            assert label[0] in allowed, f"{where}: у {name} нет метки {label[0]}"
    for group in _BY.findall(expr):
        for label in filter(None, (x.strip() for x in group.split(","))):
            assert label in used | TARGET_LABELS, f"{where}: группировка по неизвестной метке {label}"


def test_every_alert_has_metadata_and_tested_alerts_exist() -> None:
    rules = [r for g in yaml.safe_load(RULES.read_text())["groups"] for r in g["rules"]]
    names = {r["alert"] for r in rules}
    assert len(names) == len(rules)
    for rule in rules:
        assert rule["labels"]["severity"] in {"info", "warning", "critical"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
    tested = {
        t["alertname"] for case in yaml.safe_load(RULE_TESTS.read_text())["tests"] for t in case["alert_rule_test"]
    }
    assert tested <= names
    # ТЗ §16 / FR-OBS-04: auth failures, stuck leases, high error rates, storage capacity покрыты тестами promtool.
    assert {
        "AicdLoginFailuresSpike",
        "AicdStuckLeases",
        "AicdApiHighErrorRate",
        "AicdArtifactStorageLow",
        "AicdWebhookSignatureInvalid",
    } <= tested


def test_dashboards_are_valid_provisioning_json() -> None:
    uids = set()
    for path in DASHBOARDS:
        board: dict[str, Any] = json.loads(path.read_text())
        assert board["uid"] not in uids
        uids.add(board["uid"])
        ids = [p["id"] for p in board["panels"]]
        assert len(ids) == len(set(ids)), path.name
        for p in board["panels"]:
            if p["type"] != "row":
                assert p["datasource"]["uid"] == "aicd-prometheus", (path.name, p["title"])
    datasource = yaml.safe_load(
        (MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text()
    )
    assert datasource["datasources"][0]["uid"] == "aicd-prometheus"
    assert len(DASHBOARDS) >= 2


def test_observability_profile_is_pinned_and_not_public() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]
    for name in ("prometheus", "alertmanager", "grafana"):
        svc = services[name]
        assert svc["profiles"] == ["observability"], name
        assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), f"{name}: образ не закреплён по digest"
    # Prometheus/Alertmanager без опубликованных портов; Grafana — только loopback, без анонимного доступа.
    assert "ports" not in services["prometheus"] and "ports" not in services["alertmanager"]
    assert all(p.startswith("127.0.0.1:") for p in services["grafana"]["ports"])
    assert services["grafana"]["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert compose["networks"]["monitoring"]["internal"] is True
    nginx = (ROOT / "infrastructure" / "docker" / "web" / "nginx.conf").read_text()
    assert "location /metrics" not in nginx and "proxy_pass" in nginx
    locations = re.findall(r"location\s+(?:[=~^]+\s*)?(\S+)\s*\{", nginx)
    assert locations and all("metrics" not in loc and loc.startswith(("/api/", "/")) for loc in locations)
    assert all(loc.startswith("/api/") or loc == "/" for loc in locations), locations
