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
| Вход из веб-интерфейса (device code): `POST /providers/{id}/login`, экран *Настройки → CLI-провайдеры* | **реализован** (см. §2a); запускает официальную команду входа CLI в PTY на хосте исполнения, учётные данные платформе не передаются |
| Runner-образы с официальными CLI | не собраны (P2-01) |
| Проверка статуса платформой | `POST /api/v1/providers/{id}/probe` (worker-exec) — **реальный** probe установленного CLI по CLI_ADAPTER_CONTRACT §5.2 (версия, `binary_sha256`, help-сканирование, матрица, статус входа); Fake CLI — только если официальный CLI на хосте не найден (`AICD_PROBE_ADAPTER`) |
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

## 2a. Вход из веб-интерфейса (device code)

Процедура §2 требует доступа к терминалу хоста исполнения. Для оператора без такого доступа тот же вход
запускается из UI: *Настройки → CLI-провайдеры → раздел «Вход по подписке» → «Войти»*. Права — только
`owner` (вход привязывает подписку к хосту, OQ-01/OQ-24) и свежая re-аутентификация (step-up, SC-IAM-08).

Что происходит:

1. API создаёт сессию входа (`cli_login_sessions`, миграция 0022) и ставит событие `provider.login_requested`
   в очередь exec; одна активная сессия на провайдера в workspace;
2. worker-exec запускает **официальную** команду входа (`claude auth login --claudeai`) в PTY на своём хосте;
3. UI показывает то, что напечатал сам CLI: ссылку входа и, если он его печатает, код устройства;
4. оператор открывает ссылку **в своём браузере** и подтверждает вход у провайдера своей учётной записью;
4a. если CLI просит ввести код из браузера (Claude Code: `Paste code here if prompted >`), UI показывает
   поле ввода (`ProviderLoginStatus.awaiting_code`); код уходит в stdin процесса CLI через pub/sub Redis и
   **нигде не сохраняется** — ни в БД, ни в audit (там только факт `provider.login_code_submitted`), ни в
   логах, ни в ответе API. Обменять его может только CLI: `code_verifier` потока PKCE не покидает CLI.
   Эхо PTY при этом выключено, а введённое значение вырезается из вывода, если CLI его отобразил;
5. CLI сохраняет токен в свой credential store; платформа переводит сессию в `AUTHENTICATED` и сразу
   выполняет probe, чтобы `auth_status` установки обновился.

Чего платформа не делает (ТЗ §1, ADR-0003, §0 выше): не показывает полей для токена, пароля или API-ключа и
не принимает их; не проксирует OAuth — браузерный поток идёт напрямую между оператором и провайдером;
не читает credential store CLI. Из вывода CLI перед записью в БД и показом удаляются управляющие
последовательности и все токеноподобные фрагменты (`aicd_cli_adapters.login.scrub`). Ссылка входа
сохраняется **без изменений** — оператор должен открыть ровно её; секрета в её query нет (`code_challenge`
публичен по построению PKCE, `state` защищает от CSRF, авторизационный код приходит самому CLI).

Ограничения: `--console`, `--with-api-key` и `--with-access-token` запрещены кодом (это биллинг API или
перенос чужого токена, а не вход по подписке). Команда входа провайдера исполняется только после сверки её
флагов с `--help` установленной версии CLI (`LoginProfile.confirmed`, P2-01): несверённый профиль отказывает
с объяснением. Сессия живёт 10 минут, отменяема кнопкой «Отменить вход» (worker снимает процесс CLI), по
истечении — `EXPIRED`. CLI должен быть **установлен на хосте worker-exec** (иначе `CLI_NOT_INSTALLED`); путь
к CLI вне `PATH` воркера задаётся `AICD_LOGIN_BINARIES` (`{"claude": "/opt/claude/bin/claude"}`), домашний
каталог с credential store — `AICD_LOGIN_HOME`.

## 3. Вход: Codex CLI

Команды проверены по `codex login --help` версии 0.167.0 (`codex-cli`) в окружении разработки:

```sh
sudo -iu aicd-runner                      # выделенный пользователь хоста исполнения (local-режим), не root
codex login --device-auth                 # вход учётной записью ChatGPT (подписка) по device code
codex login status                        # проверка
codex logout
```

`--device-auth` обязателен для входа из UI (§2a) и для хоста без браузера: CLI печатает ссылку
(`https://auth.openai.com/codex/device`) и одноразовый код (15 минут), которые оператор вводит в браузере
**своей** машины. Без этого флага `codex login` поднимает приёмник OAuth на самом хосте исполнения
(`localhost:1455`) и печатает ссылку на него — с другой машины она не открывается.

Не использовать `codex login --with-api-key`, `codex login --with-access-token` и `OPENAI_API_KEY`.

## 4. Docker-режим (целевой, после P2-01)

Том на провайдера, монтируется только в runner того же провайдера (container_policy: rw только для `aicd-cred-*`):

```sh
for p in claude codex; do
  docker volume create aicd-cred-$p
  # Том создаётся владельцем root; non-root runner писать в него не сможет, пока владелец не сменён.
  docker run --rm --user 0 --network none -v aicd-cred-$p:/v --entrypoint sh aicd/cli-runner:latest \
    -c 'chown <uid>:<gid> /v && chmod 700 /v'      # uid:gid = AICD_CLI_RUNNER_USER
done
export AICD_CLI_CREDENTIAL_VOLUMES='{"claude":"aicd-cred-claude","codex":"aicd-cred-codex"}'
```

Том — **домашний каталог** CLI в runner-контейнере (`/home/agent`): в нём лежат и учётные данные, и
конфигурация CLI. Каталог хоста (`AICD_CLI_CREDENTIAL_DIRS`) в Docker-режиме не годится: его пришлось бы
монтировать bind'ом на запись, а политика контейнеров допускает единственный rw-bind — worktree задачи.

Вход из UI (§2a) и проверка статуса входа при заданном `AICD_CLI_CREDENTIAL_VOLUMES` выполняются не на
хосте, а в одноразовом контейнере runner-образа над тем же томом
(`aicd_cli_adapters.container_launch`): `docker run --rm` с non-root uid, `cap-drop ALL`,
`no-new-privileges`, read-only rootfs, лимитами памяти/pids, сетью `AICD_CLI_RUNNER_NETWORK` и
единственным монтированием — томом учётных данных. Для входа добавляются TTY и stdin (device code CLI
печатает только в TTY; в stdin уходит лишь код авторизации из UI). Поэтому воркеру exec нужен клиент
`docker` (`AICD_CLI_DOCKER_BINARY` или PATH) — через docker-socket-proxy оверлея `compose.sandbox.yaml`
интерактивный контейнер не запустить; в таком развёртывании вход выполняет оператор той же командой
вручную (`docker run -it … aicd/cli-runner claude auth login --claudeai`).

Учётные данные, оставшиеся в `~/.claude`/`~/.codex` хоста, в том **не переносятся** (платформа и её
инструменты не читают и не копируют credential store, ТЗ §1): после перехода на тома вход выполняется заново.

## 5. Проверка из платформы

```sh
curl -fsS -b cookies.txt -H "X-Workspace-Id: <ws>" http://localhost:8080/api/v1/providers            # установки и auth_status
curl -fsS -b cookies.txt -X POST -H "X-Workspace-Id: <ws>" -H "X-CSRF-Token: <csrf>" -H "Idempotency-Key: $(uuidgen)" \
  -H "Origin: http://localhost:8080" http://localhost:8080/api/v1/providers/<provider_id>/probe       # 202 + status_url
```

(Обычно — экран CLI Providers → «Проверить».) Метрика `aicd_provider_available{provider}`; алерт `AicdProviderUnavailable`.

Probe читает только то, что CLI печатает сам: `--version`, справку (`--help`, help подкоманд) и официальную
status-команду входа (`codex login status`, `claude auth status --json`). Учётные данные не читаются, а из
вывода status-команды сохраняется только статус и неидентифицирующий тип плана: в нём бывают e-mail и
название организации (ТЗ §7.1). Вход по API-ключу авторизованной установкой **не** считается (ТЗ §1).

Путь к CLI берётся из `AICD_LOGIN_BINARIES`, иначе из `PATH` воркера; в production поиск по `PATH` запрещён
(§5.2 п. 1, защита от подмены бинаря) — путь задаёт оператор. `AICD_PROBE_ADAPTER`: `auto` (по умолчанию —
реальный CLI, если найден, иначе Fake CLI), `real`, `fake`.

**Статус `UNTESTED` и как его снять.** Версия CLI считается проверенной, только когда для неё есть строка
`SUPPORTED` в `packages/contracts/cli-compatibility.yaml`, а строка появляется только по результатам набора
`real-cli-smoke@1` на реальной установке (CLI_ADAPTER_CONTRACT §5.3, §13.2). До этого UI показывает
предупреждение «версия CLI не проверена на совместимость»; исполнение не блокируется (policy
`allow_untested_cli_versions` для профилей `basic`/`strict`, в `critical` — блокируется).

Набор запускается на хосте исполнения после входа в оба CLI (§2/§2a) и идёт тем же путём, что задачи:
фабрика адаптера воркера, runner-контейнер, том учётных данных. Он тратит несколько коротких запросов
подписки на провайдера (минимальная задача, read-only профиль, отмена, выбор модели) и не проверяет лимиты.

```sh
source <env воркера>                       # те же AICD_* переменные, что у worker-exec
AICD_REAL_CLI=1 AICD_REAL_CLI_EVIDENCE=reports/real-cli/smoke-$(date +%F).json \
  uv run pytest tests/real_cli -m real_cli
uv run python scripts/cli_matrix.py reports/real-cli/smoke-$(date +%F).json   # строки SUPPORTED из evidence
```

`scripts/cli_matrix.py` записывает строку ровно на проверенную версию (`==X.Y.Z`) и только если пройдены все
проверки набора; файл evidence коммитится вместе с матрицей. После этого — перезапуск worker-exec и
«Перепроверить» в UI: установка получает `SUPPORTED`. Обновление CLI (в том числе версии в runner-образе)
возвращает `UNTESTED` до нового прогона — это и есть назначение матрицы.

Probe записывает одну действующую установку провайдера на хост: строка с прежним бинарём (например,
Fake CLI до перехода на официальный) выключается и в UI больше не показывается.

## 6. Когда выполнять вход

* первичная установка хоста исполнения;
* `auth_status` = `AUTH_REQUIRED`/`EXPIRED` (задачи `BLOCKED(AUTH_REQUIRED)`, уведомление в чате/Approvals);
  вход выполняется процедурой §2 (терминал хоста) либо §2a (из UI) — результат одинаков;
* после восстановления из бэкапа (тома учётных данных не бэкапятся — [backups.md §1](backups.md));
* после подозрения на утечку: `logout`, смена пароля/сессий у провайдера, затем вход ([incident-response.md](incident-response.md)).
