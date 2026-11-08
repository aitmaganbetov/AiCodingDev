# Runbook: сборка, SBOM, подпись и provenance релиза (P9-08)

**Требования:** ТЗ §14–15 (SBOM, artifact integrity, secure CI), NFR-SEC-05, SC-SUP-*, RG (release gate).
**Статус:** проверено конвейером в окружении разработки — [reports/release/SUPPLY_CHAIN_VERIFY.md](../../reports/release/SUPPLY_CHAIN_VERIFY.md).
Ничего не публикуется в реестры и не развёртывается: результат — каталог релиза (архивы образов + метаданные).

## 1. Сборка

```sh
uv run python -m scripts.release.pipeline --version 1.0.0-rc1 --out /srv/aicd-release/1.0.0-rc1 \
  --key-dir /media/release-keys --password-file /media/release-keys/password [--ca-bundle /etc/ssl/corp-ca.pem]
```

Шаги: `docker build` образов `infrastructure/docker/backend.Dockerfile` и `web.Dockerfile` (базовые образы только по digest —
иначе отказ; метки OCI с версией и коммитом) → `docker save` → SBOM CycloneDX (syft, образ по digest из
`aicd_verification.tools.images`, сеть контейнера `none`): исходники backend (`pyproject.toml`+`uv.lock`), web
(`package.json`+`pnpm-lock.yaml`) и каждый образ → подпись каждого архива и SBOM (`cosign sign-blob`, Sigstore bundle) →
SLSA v1 provenance (`cosign attest-blob --type slsaprovenance1`: коммит, dirty, Dockerfile, build args, базовые образы и
lock-файлы с sha256, image_id) и SBOM-аттестация образа (`--type cyclonedx`) → `release-manifest.json`, `SHA256SUMS` (подписан).

`releasable=false` в манифесте, если рабочее дерево было грязным или образ собран с `--no-git-in-backend`
(флаг только для проверки конвейера без доступа к deb.debian.org: без git привязка репозиториев не работает).

## 2. Проверка (перед установкой и в release gate)

```sh
uv run python -m scripts.release.verify --release /srv/aicd-release/1.0.0-rc1 --public-key /etc/aicd/cosign.pub \
  --expect-commit <sha> --tamper-test --report reports/release/SUPPLY_CHAIN_VERIFY.md
```

Открытый ключ — из доверенного источника (опубликован владельцем, sha256 сверяется), а не из каталога релиза. Проверка
офлайн, доверие — только ключу (без Rekor/Fulcio: `--insecure-ignore-tlog=true`). `--tamper-test` доказывает отказ проверки
при подмене байта образа, SBOM, SHA256SUMS, provenance другого образа и чужом ключе. Установка — [upgrades.md](upgrades.md)
(`docker load`, сверка `image_id`).

## 3. Управление ключом подписи (keyless недоступен офлайн)

* Пара ключей cosign (ECDSA P-256, закрытый ключ зашифрован паролем): `--generate-key` при первой сборке в `--key-dir`
  на **съёмном/офлайн носителе** или в HSM-обёртке; пароль — отдельно (менеджер паролей), два хранителя.
* `cosign.pub` публикуется владельцем (README релиза, внутренний портал) с sha256; потребители закрепляют его.
* Ключ не попадает в репозиторий, образы, артефакты, CI-логи; сборка выполняется на доверенной машине сборки (не на хосте
  исполнения агентов).
* Ротация: ежегодно или при компрометации — новая пара, публикация нового `cosign.pub` (старый остаётся для проверки
  прежних релизов, при компрометации — объявляется отозванным, релизы переподписываются новым ключом).
* Keyless (Fulcio/Rekor) и публикация в реестр с `cosign sign` образа — при появлении сетевого доступа и реестра (решение владельца).

## 4. Образы verification, собираемые платформой (OQ-43 (a))

| Образ | Рецепт | Проверка |
|---|---|---|
| `aicd/migrations-python:17-3.12` | `infrastructure/docker/verification/migrations-python.Dockerfile` (PostgreSQL 17 из образа сервиса + Python 3.12 alpine 3.24, зависимости `--require-hashes`) | `uv run python -m scripts.release.check_migrations_image aicd/migrations-python:17-3.12` — драйвер MIGRATION gate: upgrade→downgrade→upgrade, схемы совпадают |
| `aicd/phpstan:2.3.5` | `infrastructure/docker/verification/phpstan.Dockerfile` (php:8.3-cli-alpine по digest + phpstan.phar по sha256) | `docker run --rm --network none -v <проект>:/w:ro -w /w aicd/phpstan:2.3.5 phpstan analyse --error-format=json --no-progress` |
| `aicd/osv-scanner:v2.7.0` | `infrastructure/docker/verification/osv-scanner.Dockerfile` | `tests/security/test_security_gates_docker.py` |

Подключение к воркеру: digest собранного образа (`docker image inspect --format '{{.Id}}'`) — в
`AICD_VERIFICATION_IMAGES='{"migrations-python": "aicd/migrations-python:17-3.12@sha256:…", "php": "aicd/phpstan:2.3.5@sha256:…"}'`
(поверх таблицы `aicd_verification.images`; в production — только по digest). Для профиля server образы публикуются во
внутренний реестр и digest фиксируется оттуда.

## 5. Предрелизная проверка развёртывания: TLS/HSTS на edge (TH-46, OQ-48 (b))

Принятие риска TH-46 (MITM) владельцем — при условии обязательной проверки внешнего входа. Выполняется на целевом
развёртывании после настройки TLS-терминатора перед `web`, перед выпуском (RG-11, [RELEASE_CHECKLIST](../release/RELEASE_CHECKLIST.md))
и после каждого изменения edge (сертификат, прокси, заголовки):

```sh
uv run python -m scripts.release.preflight_tls --url https://aicd.example.internal [--ca-file /etc/aicd/corp-ca.pem] \
  [--http-url http://aicd.example.internal/] [--min-days 14] [--json > reports/release/preflight_tls.json]
```

| Проверка | Требование (SECURITY_ARCHITECTURE §14, SC-NET-03, SC-OBS-03) |
|---|---|
| `tls-versions` | TLS 1.0 и 1.1 отклоняются, TLS 1.2+ принимается (если локальный OpenSSL не может предложить 1.0/1.1 — FAIL «не проверено»: запустить с другой машины) |
| `certificate` | цепочка доверена (системные CA или `--ca-file`), имя хоста совпадает, до истечения ≥ 14 дней (`--min-days`) |
| `hsts` | `Strict-Transport-Security: max-age` ≥ 31536000 и `includeSubDomains` |
| `http-redirect` | `http://<хост>/` → 301/302/307/308 на `https://<тот же хост>` |
| `security-headers` | CSP (с `frame-ancestors`), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` — те же значения, что `add_header` в `infrastructure/docker/web/nginx.conf` |
| `metrics-not-public` | `/metrics`, `/api/metrics` снаружи не отдают метрики Prometheus (404/401/403 или fallback SPA) |

Код выхода 0 — все `PASS`; 1 — есть `FAIL` (выпуск/ввод в эксплуатацию блокируется до исправления edge); 2 — ошибка
аргументов. Запросы идут напрямую (без `*_PROXY`). Пример конфигурации терминатора (nginx): `ssl_protocols TLSv1.2 TLSv1.3;`,
`add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`, отдельный `server { listen 80; return 308 https://$host$request_uri; }`;
заголовки `web` проксируются без изменений. Внутренний TLS профиля server (PG `verify-full`, Redis TLS) скрипт не
проверяет — по чек-листу развёртывания (SECURITY_ARCHITECTURE §14). Тесты: `tests/unit/test_preflight_tls.py`
(локальный TLS-сервер с сертификатом сгенерированного CA: успешный случай и каждый отказ).
