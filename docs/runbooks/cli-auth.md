# Runbook: авторизация Codex CLI и Claude Code CLI оператором (P9-06)

**Требования:** ТЗ §1, §3, §15, §19 AC-16; ADR-0003; CLI_ADAPTER_CONTRACT §14; SECURITY_ARCHITECTURE §6.2; SC-CLI-03, SC-CLI-05, SC-CLI-09.
**Статус:** IN_REVIEW — требует ревью оператором на машине с установленными официальными CLI (P2-01).

## 0. Правила (не обсуждаются)

* Только **официальные** CLI (OpenAI Codex CLI, Anthropic Claude Code CLI) и только **подписочный вход** самого CLI,
  выполняемый человеком-оператором интерактивно. Платформа не инициирует вход, не проксирует OAuth/браузерный поток,
  не принимает токены через UI/API и не читает файлы учётных данных (кроме отпечатков для детектора утечек).
* **Запрещено:** API-ключи (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `codex login --with-api-key`, `claude auth login --console`
  — биллинг Anthropic Console/API), прокси/подмена эндпоинтов, перехват трафика CLI, неофициальные клиенты, общие или
  «ротируемые» аккаунты, смена аккаунта для обхода лимитов. При `RATE_LIMITED` — ждать cooldown; при `AUTH_REQUIRED` —
  задача `BLOCKED` с причиной, обходных путей нет (AC-16).
* Одна подписка — один хост исполнения (`max_concurrent_sessions=1` до юридического подтверждения, OQ-01/OQ-24).
  Соответствие условиям провайдеров — ответственность владельца подписки (SECURITY_ARCHITECTURE Q-7).
* Учётные данные CLI живут только в томе `aicd-cred-<provider>` (Docker-режим) или в OS credential store / домашнем
  каталоге выделенного пользователя (local-режим); никогда — в PG, Redis, артефактах, бэкапах, verification-контейнерах.

## 1. Текущее состояние реализации (честно)

| Элемент | Состояние |
|---|---|
| Адаптер исполнения | только Fake CLI (`AICD_EXEC_ADAPTER=fake`, запрещён в production); реальные адаптеры Codex/Claude — после P2-01/P2-05 (машина с авторизованными CLI) |
| `aicd cli login <provider>` (SECURITY_ARCHITECTURE §6.2: официальный CLI в одноразовом контейнере с томом `aicd-cred-<provider>` и TTY оператора) | **не реализован** — до готовности runner-образов CLI |
| Runner-образы с официальными CLI | не собраны (P2-01) |
| Проверка статуса платформой | `POST /api/v1/providers/{id}/probe` (worker-exec, `check_auth` адаптера) — работает для Fake CLI |
| Возврат задач `BLOCKED(AUTH_REQUIRED)` в `READY` после входа | не реализован (см. [recovery.md §2](recovery.md)) |

Поэтому ниже — процедура для хоста исполнения в local-режиме (выделенный пользователь `aicd-runner`) и целевая процедура
Docker-режима; обе требуют подтверждения оператором при подключении реальных CLI.

## 2. Вход: Claude Code CLI

Команды проверены по `claude --help` версии 2.1.319 (Claude Code) в окружении разработки:

```sh
sudo -iu aicd-runner                      # выделенный пользователь хоста исполнения (local-режим), не root
claude auth login --claudeai              # вход по подписке Claude (значение по умолчанию); откроет страницу входа
claude auth status --json                 # проверка: вошли по подписке
claude auth logout                        # выход (смена владельца подписки, компрометация)
```

Не использовать `claude auth login --console` (Anthropic Console — API-биллинг) и переменную `ANTHROPIC_API_KEY`.
Хост без браузера: вход выполняется на этом же хосте через ссылку/код, которые печатает сама команда входа (способ,
предусмотренный CLI); перенос файлов учётных данных с другой машины не допускается.

## 3. Вход: Codex CLI

Codex CLI в окружении разработки не установлен — команды ниже взяты из документации OpenAI и **должны быть сверены
оператором** с `codex --help` установленной версии (P2-01):

```sh
sudo -iu aicd-runner
codex login                               # вход с учётной записью ChatGPT (подписка)
codex login status                        # проверка
codex logout
```

Не использовать `codex login --with-api-key` и `OPENAI_API_KEY`.

## 4. Docker-режим (целевой, после P2-01)

Том на провайдера, монтируется только в runner того же провайдера (container_policy: rw только для `aicd-cred-*`):

```sh
docker volume create aicd-cred-claude
docker volume create aicd-cred-codex
```

Вход — официальный CLI из runner-образа провайдера в одноразовом контейнере с этим томом в домашнем каталоге и TTY
оператора (будущая `aicd cli login <provider>`). Пока runner-образов нет, Docker-режим не используется.

## 5. Проверка из платформы

```sh
curl -fsS -b cookies.txt -H "X-Workspace-Id: <ws>" http://localhost:8080/api/v1/providers            # установки и auth_status
curl -fsS -b cookies.txt -X POST -H "X-Workspace-Id: <ws>" -H "X-CSRF-Token: <csrf>" -H "Idempotency-Key: $(uuidgen)" \
  -H "Origin: http://localhost:8080" http://localhost:8080/api/v1/providers/<provider_id>/probe       # 202 + status_url
```

(Обычно — экран CLI Providers → «Проверить».) Метрика `aicd_provider_available{provider}`; алерт `AicdProviderUnavailable`.

## 6. Когда выполнять вход

* первичная установка хоста исполнения;
* `auth_status` = `AUTH_REQUIRED`/`EXPIRED` (задачи `BLOCKED(AUTH_REQUIRED)`, уведомление в чате/Approvals);
* после восстановления из бэкапа (тома учётных данных не бэкапятся — [backups.md §1](backups.md));
* после подозрения на утечку: `logout`, смена пароля/сессий у провайдера, затем вход ([incident-response.md](incident-response.md)).
