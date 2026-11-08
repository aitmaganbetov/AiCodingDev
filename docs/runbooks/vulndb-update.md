# Runbook: обновление офлайн-БД уязвимостей (OQ-43 (f), OQ-51 (b))

**Требования:** ТЗ §14 (SCA без сети в gate), ТЗ §15 (опасные действия — с одобрением), QUALITY_GATES §4.1,
SECURITY_ARCHITECTURE §11 (`VULN_DB_UPDATE`), `aicd_verification.security_gates` (`trivy_db_status`, `osv_db_status`:
нет БД или старше 7 суток → gate `ERROR(DB_UNAVAILABLE)`, никогда `PASS`). **Статус:** IN_REVIEW.

Gates `trivy-vuln` и `osv-scanner-sca` работают с `network none` и читают БД из `/aicd-db` (только чтение). Обновление —
единственный шаг с egress, отдельный, выполняется только по **утверждённому approval платформы `VULN_DB_UPDATE`**:

| БД | Источник (egress allowlist) | Формат в `<db-root>` |
|---|---|---|
| OSV | `osv-vulnerabilities.storage.googleapis.com` (`<экосистема>/all.zip`, md5 сверяется с `x-goog-hash`) | `osv/osv-scalibr/<экосистема>/all.zip` + `osv/aicd-osv-db.json` |
| Trivy | реестр БД Trivy (`mirror.gcr.io/aquasec/trivy-db:2`; `--trivy-db-repository` для внутреннего зеркала) через закреплённый образ `aquasec/trivy` | `trivy/db/trivy.db` + `metadata.json` |

## 1. Approval `VULN_DB_UPDATE` (миграция 0016)

| Свойство | Значение |
|---|---|
| Мин. роль утверждающего | admin (`approval_min_role`) |
| Four-eyes / step-up | да / да (критический набор `FOUR_EYES_ACTIONS`): инициатор не утверждает свой запрос; решение — повторный ввод пароля не старше 10 мин |
| Срок | 24 ч от запроса; утверждённое, но не использованное за срок — недействительно |
| Привязка | sha256 канонического JSON предмета: виды БД (`osv`, `trivy`), экосистемы OSV, реестр БД Trivy, egress-хосты. Другой состав — отказ |
| Использование | однократно (`sys_vulndb_authorize` пишет аудит `vulndb.update_authorized`; повтор — отказ) |
| Аудит | `approval.request` (инициатор, `via=aicd_maintenance`), `approval.decide`, `vulndb.update_authorized`, `vulndb.update_finished` (success/failure, sha256 БД) |

Approval относится к workspace и проекту (обычно служебный проект «Платформа» workspace администраторов) и виден на
экране Approvals. Вызовы `sys_vulndb_*` доступны только роли `aicd_maintenance` (утилита на хосте оператора).
Офлайн-обхода (файла одобрения) нет: без платформы gates всё равно не работают, а одобрение должно быть проверяемым.

## 2. Процедура

```sh
U="uv run python -m scripts.vulndb.update"
DB="--pg-container aicodingdev-postgres-1"   # или --maintenance-dsn-file <файл с DSN aicd_maintenance>

# 1) запрос (инициатор — участник workspace, от имени которого запрашивается обновление)
$U request --workspace-id <ws> --project-id <проект> --requested-by you@corp.example \
  --osv-ecosystems PyPI,npm,Go,Packagist --trivy $DB             # → approval_id
# 2) другой участник с ролью admin/owner утверждает его в UI Approvals (step-up)
# 3) обновление — тот же состав аргументов, что в запросе
$U apply --approval-id <approval_id> --db-root /var/lib/aicd/vulndb \
  --osv-ecosystems PyPI,npm,Go,Packagist --trivy $DB [--ca-bundle /etc/ssl/corp-ca.pem]
```

* `apply` до загрузки вызывает `sys_vulndb_authorize`: не `APPROVED`, истёк, уже использован → 42501; другой предмет → 22023;
  ничего не скачивается.
* Загрузка — в staging-каталог; проверки: размер ≤ 1 GiB, md5 от сервера, целостность zip, имена записей (`*.json` без путей),
  формат OSV; для Trivy — `metadata.json` и свежесть. Затем атомарная замена `<db-root>/<osv|trivy>`; предыдущая версия —
  `<db-root>/.previous/<вид>` (откат: переименовать обратно). Итог — в аудит платформы и `<db-root>/audit.jsonl`.
* Права: каталоги `0755`, файлы `0644` (gate-контейнеры читают от непривилегированного uid), монтирование только для чтения.
* Сеть: на хосте обновления разрешить только хосты из таблицы (egress-proxy `infrastructure/docker/egress-proxy/allowlist.d`),
  не на хостах исполнения агентов.

Проверено: поток approval (запрос → four-eyes отказ инициатору → developer ниже admin → утверждение admin →
несовпадающий предмет 22023 → авторизация → повтор 42501 → итог; истёкшее approval → 42501) —
`tests/integration/test_vulndb_approval.py`; загрузка OSV PyPI (26 829 записей, md5 сверен) и Trivy, рабочая офлайн-БД
(`osv-scanner --offline` находит GHSA для `jinja2==2.10`) — прогон 2026-11-08 (до перехода на approval платформы; логика
загрузки не менялась).

## 3. Расписание и мониторинг

* Еженедельно (БД старше 7 суток → gates `ERROR`): запрос планируется заранее, утверждение — дежурным admin.
* Неудача — `vulndb.update_finished` с `outcome=failure` в аудите и код 1; gates продолжают работать на прежней БД, пока она свежая.
