"""Вспомогательные модули security/perf/supply-chain gates (P6-04, P6-06, P6-07).

* `images`       — закреплённые по digest образы инструментов;
* `findings`     — нормализованная находка, severity, пороги профилей, fingerprint;
* `parsers`      — разбор отчётов Semgrep/Gitleaks/Trivy/OSV-Scanner/Checkov/conftest/Syft/Cosign;
* `perf`         — определение бенчмарка, заявления об ускорении, статистика и вердикт PERF;
* `bench_runner` — stdlib-скрипт измерений, исполняемый внутри verification-контейнера;
* `rules/`       — ruleset Semgrep и конфигурация Gitleaks, поставляемые платформой.
"""
