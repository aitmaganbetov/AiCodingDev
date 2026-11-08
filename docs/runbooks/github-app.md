# Runbook: GitHub App — установка, ротация ключа и webhook secret (P9-06)

**Требования:** ТЗ §15 (подпись webhook, дедупликация, branch protection), SC-GH-01…06, SC-SEC-06, OQ-47, API_DESIGN §15c.
**Статус:** IN_REVIEW (ревью оператором; в окружении разработки проверено только против GitHub-двойника тестов —
`tests/integration/test_github_*.py`, реальная App не создавалась).

## 1. Создание App (владелец организации GitHub)

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App:

| Поле | Значение |
|---|---|
| Webhook URL | `https://<публичный origin>/api/v1/integrations/github/webhook` (TLS-терминатор перед `web`) |
| Webhook secret | случайный ≥ 32 байт: `head -c 48 /dev/urandom \| base64 \| tr -d '/+=\n'` |
| Repository permissions | Contents: Read & write; Pull requests: Read & write; Checks: Read; Commit statuses: Read & write (OQ-47 (1) — публикация результатов gates); Metadata: Read. **Не выдавать:** Workflows, Administration, Secrets, Actions (write) (SC-GH-04) |
| Subscribe to events | Pull request, Pull request review, Check run, Check suite, Status, Push (обрабатываются также `installation`, `installation_repositories`, `ping`; прочее отклоняется) |
| Where can be installed | Only on this account |

После создания: App ID, slug, сгенерировать private key (`.pem`).

## 2. Подключение к платформе

Секреты — файлы (Docker secrets), не переменные окружения со значениями:

```sh
install -m 0444 app.private-key.pem infrastructure/docker/secrets/github_app_private_key.pem
printf '%s' '<webhook secret>' > infrastructure/docker/secrets/github_webhook_secret && chmod 0444 infrastructure/docker/secrets/github_webhook_secret
shred -u app.private-key.pem          # исходник ключа не оставлять
```

Override оператора (`infrastructure/docker/compose.override.yaml`, вне git): `api` получает только webhook secret
(SECURITY_ARCHITECTURE §6.1, OQ-47 (2)); закрытый ключ — только процессу worker-orchestrator.

```yaml
services:
  api:
    environment:
      AICD_GITHUB_APP_ID: "<app id>"
      AICD_GITHUB_APP_SLUG: "<slug>"
      AICD_GITHUB_WEBHOOK_SECRET_FILE: /run/secrets/github_webhook_secret
    secrets: [github_webhook_secret]
secrets:
  github_webhook_secret: { file: ./secrets/github_webhook_secret }
  github_app_private_key: { file: ./secrets/github_app_private_key.pem }
```

Воркер оркестратора (как развёрнут у оператора): `AICD_GITHUB_APP_ID`, `AICD_GITHUB_APP_SLUG`,
`AICD_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github_app_private_key`, при необходимости `AICD_EGRESS_PROXY_URL`.
В production базовые URL — только `https://` (`AICD_GITHUB_API_URL`, `AICD_GITHUB_WEB_URL`; проверяется при старте).

Установка в workspace: UI Settings → GitHub → «Установить» (`POST /api/v1/integrations/github/installations/setup` —
одноразовый state 10 мин) → выбор репозиториев на GitHub → возврат; проверка установки/доступа выполняется воркером
(`GET /api/v1/integrations/github/verifications/{id}`). Проверка вебхука: «Redeliver» ping в настройках App → в
`github_webhook_deliveries` статус принят, метрика `aicd_webhook_signature_invalid_total` не растёт.

## 3. Ротация webhook secret (без простоя, SC-SEC-06)

Dangerous action: approval `SECRET_CHANGE` (owner, four-eyes) — оформить до начала.

1. Сгенерировать новый секрет; текущий файл сохранить как предыдущий:
   ```sh
   cd infrastructure/docker/secrets
   cp github_webhook_secret github_webhook_secret_previous && chmod 0444 github_webhook_secret_previous
   printf '%s' '<new secret>' > github_webhook_secret.new && chmod 0444 github_webhook_secret.new && mv github_webhook_secret.new github_webhook_secret
   ```
2. В override `api`: `AICD_GITHUB_WEBHOOK_SECRET_PREVIOUS_FILE: /run/secrets/github_webhook_secret_previous` + secret; применить
   `docker compose -f infrastructure/docker/compose.yaml -f infrastructure/docker/compose.override.yaml up -d api`
   (оба секрета активны: подпись проверяется любым).
3. Сменить секрет в настройках App на GitHub; Redeliver ping → принят.
4. Через ≥ 1 ч (повторные доставки GitHub) убрать `..._PREVIOUS_FILE` и файл предыдущего секрета, `up -d api`.
5. Алерт `AicdWebhookSignatureInvalid` в процессе ротации — признак пропущенного шага 2.

## 4. Ротация закрытого ключа App

1. GitHub → App → Private keys → Generate a new private key (у App может быть несколько активных ключей).
2. Заменить файл `github_app_private_key.pem` (0444), перезапустить worker-orchestrator.
3. Проверка: повторная проверка установки в UI (verification → `COMPLETED`), push/PR тестового run.
4. На GitHub удалить старый ключ. Installation tokens живут ≤ 1 ч и только в памяти — отзывать нечего.
5. При компрометации — сначала удалить старый ключ на GitHub (запросы сломаются до шага 2), затем шаги 1–3;
   инцидент по [incident-response.md](incident-response.md).

## 5. Отключение

Uninstall App в организации → событие `installation` (deleted) деактивирует установку; удалить секреты и override;
перезапустить api/воркеры.
