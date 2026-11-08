# Runbook: обновление офлайн-БД уязвимостей (OQ-43 (f))

**Требования:** ТЗ §14 (SCA без сети в gate), QUALITY_GATES §4.1, `aicd_verification.security_gates` (`trivy_db_status`,
`osv_db_status`: нет БД или старше 7 суток → gate `ERROR(DB_UNAVAILABLE)`, никогда `PASS`). **Статус:** IN_REVIEW.

Gates `trivy-vuln` и `osv-scanner-sca` работают с `network none` и читают БД из `/aicd-db` (только чтение). Обновление —
единственный шаг с egress, отдельный, одобренный и аудируемый:

| БД | Источник (egress allowlist) | Формат в `<db-root>` |
|---|---|---|
| OSV | `osv-vulnerabilities.storage.googleapis.com` (`<экосистема>/all.zip`, md5 сверяется с `x-goog-hash`) | `osv/osv-scalibr/<экосистема>/all.zip` + `osv/aicd-osv-db.json` |
| Trivy | реестр БД Trivy (`mirror.gcr.io/aquasec/trivy-db:2`; `--trivy-db-repository` для внутреннего зеркала) через закреплённый образ `aquasec/trivy` | `trivy/db/trivy.db` + `metadata.json` |

## 1. Одобрение (четыре глаза)

Ответственный за безопасность (не исполнитель) оформляет JSON, срок действия — 24 ч:

```json
{"approved_by": "security-officer@corp.example", "approved_at": "2026-11-08T17:00:00Z",
 "reason": "еженедельное обновление офлайн-БД SCA", "ticket": "OPS-1234", "scope": ["osv", "trivy"]}
```

Инструмент отказывает, если одобрение неполное, просрочено, не покрывает запрошенные БД или одобривший совпадает с
пользователем ОС, выполняющим обновление. sha256 файла одобрения пишется в журнал аудита и в манифест OSV.

## 2. Обновление

```sh
uv run python -m scripts.vulndb.update --db-root /var/lib/aicd/vulndb --approval-file approval.json \
  --osv-ecosystems PyPI,npm,Go,Packagist --trivy [--ca-bundle /etc/ssl/corp-ca.pem]
```

* Загрузка — в staging-каталог; проверки: размер ≤ 1 GiB, md5 от сервера, целостность zip, имена записей
  (`*.json` без путей), формат OSV; для Trivy — `metadata.json` и свежесть (`trivy_db_status`). Только затем атомарная
  замена `<db-root>/<osv|trivy>`; предыдущая версия — `<db-root>/.previous/<вид>` (откат: переименовать обратно).
* Журнал: `<db-root>/audit.jsonl` — время, пользователь ОС, хост, одобрение (кто, тикет, sha256), sha256/размер каждой БД,
  итог (в т. ч. неудачи).
* Права: каталоги `0755`, файлы `0644` (gate-контейнеры читают от непривилегированного uid); каталог монтируется в
  verification только для чтения.
* Сеть: на хосте обновления разрешить только хосты из таблицы (egress-proxy `infrastructure/docker/egress-proxy`
  `allowlist.d`), не на хостах исполнения агентов.

Проверено в окружении разработки (2026-11-08): OSV PyPI (26 829 записей, md5 сервера сверен) + Trivy (повторное
использование свежей БД, `--download-db-only` вернул 0); `osv-scanner scan source --offline` с этой БД на
`requirements.txt` с `jinja2==2.10` нашёл 6+ GHSA — БД рабочая; `uv.lock` платформы — 0 уязвимостей.

## 3. Расписание и мониторинг

* Еженедельно (БД старше 7 суток → gates `ERROR`): задание с новым одобрением или постоянным одобрением, перевыпускаемым
  ответственным раз в сутки (политика владельца).
* Неудача — `"result": "failure"` в `audit.jsonl` и код 1; gates продолжают работать на прежней БД, пока она свежая.
* Связь с approvals платформы (тип `VULN_DB_UPDATE` в `approvals`) — требует миграции перечисления; пока одобрение — файл
  (решение владельца, OPEN_QUESTIONS OQ-43).
