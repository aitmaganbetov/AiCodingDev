# DR drill — AiCodingDev (P9-05, NFR-REL-03)

**Итог: PASS** · 2026-11-08T18:41:36.980Z · команда: `uv run python -m scripts.dr.drill` · сгенерировано скриптом (`scripts/dr/drill.py`), числа — измерены в этом прогоне.

## RPO / RTO (измерено)

| Показатель | Значение | Как измерено |
|---|---|---|
| RPO | **7.87 с** (потеряно 38 подтверждённых коммитов) | heartbeat-коммиты каждые 0.2 с до `docker kill`; последний подтверждённый 2026-11-08T18:42:31.853Z, последний восстановленный 2026-11-08T18:42:23.986Z |
| Граница RPO конфигурации | 15.0 с | `archive_timeout`=10 с + интервал `pg-wal-sync`=5.0 с (production по умолчанию: 60 с + 60 с) |
| RTO: PostgreSQL готов | 6.6 с | от аварии до promote + post-restore |
| **RTO: сервис готов** | **11.2 с** | от аварии до `/health/ready` = 200 (секреты, PG, Redis, S3+артефакты, API, воркеры) |
| RTO: run «в полёте» завершены | 16.4 с | от аварии до COMPLETED run №2 и №3 |

Время обнаружения аварии и решения оператора в RTO не входит (дрилл начинает восстановление сразу); шаги выполнялись скриптом без ручных пауз — у оператора по runbook RTO больше.

## Проверки

| Проверка | Итог | Детали |
|---|---|---|
| RPO в пределах archive_timeout + интервал wal-sync | PASS | RPO 7.9 с, потеряно 38 из 123 подтверждённых heartbeat-коммитов |
| счётчики строк всех таблиц aicd = снимок перед аварией | PASS | 78 таблиц, 729 строк; расхождения: нет |
| RLS: ENABLE+FORCE и роли сохранены, чужой workspace не виден, без контекста — 0 строк | PASS | {"no_context": 0, "ws_a_projects": 1, "ws_b_project_seen_from_a": 0, "ws_b_project_seen_from_b": 1, "forced": 61} |
| артефакты: объекты снимка восстановлены, sha256 = artifacts.sha256, повреждений нет | PASS | {"ok": 15, "rows_checked": 15, "missing": [], "mismatched": [], "missing_after_snapshot": [], "orphans": 0} |
| вход пользователя после восстановления (хэш пароля и сессии из БД) | PASS | POST /auth/login → 200 |
| health/ready приложения | PASS | {"status":"ok","checks":{"postgres":"ok","migrations":"ok","redis":"ok","object_storage":"ok"}} |
| run №1 COMPLETED и его артефакт скачивается через API с тем же sha256 | PASS | status=COMPLETED, download_sha_ok=True |
| run №2 (в полёте при бэкапе) продолжается после восстановления до COMPLETED | PASS | status=COMPLETED |
| run №3 (только в WAL) продолжается после восстановления до COMPLETED | PASS | status=COMPLETED |
| fencing token сдвинут после PITR (новые аренды > любых прежних + 1e6) | PASS | seq=1000004, max token сейчас=(1000010,) |
| логический дамп под aicd_maintenance (без суперпользователя, миграция 0016) восстанавливается (pg_restore) со строками на момент дампа | PASS | роль дампа: aicd_maintenance; расхождения: нет; pg_restore exit 0  |

## Шаги

| Шаг | Начало (UTC) | Длительность, с | Итог |
|---|---|---|---|
| стенд: сеть, секреты, PostgreSQL (архив WAL), Redis, S3 | 18:41:36.980 | 3.7 | ok |
| миграции Alembic (aicd_migrator) | 18:41:40.641 | 1.8 | ok |
| приложение: API + воркеры outbox/orchestrator/exec | 18:41:42.415 | 2.0 | ok |
| данные через приложение: владельцы (aicd CLI), probe CLI, проекты, run №1 | 18:41:44.419 | 11.3 | ok |
| run №2 «в полёте»: исполнитель остановлен | 18:41:55.708 | 6.0 | ok |
| бэкап: ключ (офлайн), репозиторий, pg-base, pg-dump, artifacts, secrets | 18:42:01.724 | 4.9 | ok |
| после бэкапа: run №3 (есть только в WAL), heartbeat, непрерывный pg-wal-sync | 18:42:06.581 | 25.2 | ok |
| АВАРИЯ: kill процессов и PostgreSQL, удаление томов/контейнеров/секретов/рабочих каталогов | 18:42:31.794 | 1.5 | ok |
| восстановление: секреты из бэкапа | 18:42:33.333 | 0.2 | ok |
| восстановление: PostgreSQL (base + WAL → promote), post-restore | 18:42:33.563 | 5.1 | ok |
| восстановление: Redis (пустой), S3 + артефакты, проверка sha256 | 18:42:38.653 | 2.7 | ok |
| восстановление: API и воркеры | 18:42:41.318 | 2.0 | ok |
| проверки: RPO, строки, RLS, артефакты, вход, run продолжаются | 18:42:43.268 | 7.3 | ok |

## Окружение и факты

* Образы (по digest): postgres `postgres:17-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac`, redis `redis:7-alpine@sha256:c5d34a98396a4775e223c74e929162c35c5a2182901ba26b50ade939941ce14b`, S3 `rustfs/rustfs@sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2`; `postgres (PostgreSQL) 17.11`; миграции до `0016`.
* Базовый бэкап: `{'backup_id': '20261108T184202Z-9fd7', 'start_lsn': '0/3000028', 'end_lsn': '0/3000120', 'start_wal_segment': '000000010000000000000003', 'size_bytes': 5698427}`.
* Логический дамп: роль `aicd_maintenance`.
* Артефакты при бэкапе: `{'backup_id': '20261108T184205Z-528e', 'objects': 14, 'new_objects': 13, 'rows_checked': 14, 'ok': 14}`; при восстановлении: `{'restored': 15, 'snapshot': '20261108T184228Z-d89f', 'verify': {'ok': 15, 'rows_checked': 15, 'missing': [], 'mismatched': [], 'missing_after_snapshot': [], 'orphans': 0}}`.
* Секретов в бэкапе: 9; учётные данные CLI не копируются (их нет в каталоге секретов, проверка имён — `scripts/backup/secrets_bundle.py`).
* Восстановление PG: `{'backup_id': '20261108T184202Z-9fd7', 'wal_staged': {'files': 4, 'segments': 3, 'last_segment': '000000010000000000000005'}, 'pg_verifybackup': 'backup successfully verified', 'post_restore': {'fencing_seq': 1000004, 'timeline': 2, 'alembic_revision': '0016'}}`.
* pg-wal-sync во время окна: `{'total': 3, 'ok': 3, 'last_ok_at': '2026-11-08T18:42:28.600Z'}`.
* Probe CLI (Fake CLI, development): ['claude:AUTHENTICATED', 'codex:AUTHENTICATED']; run №1 `COMPLETED`, run №2 при бэкапе `RUNNING` → `COMPLETED`, run №3 → `COMPLETED`.
* RLS до: `{'rls_forced_tables': 61, 'tables': 78, 'bypassrls': {'aicd_admin': True, 'aicd_app': False, 'aicd_maintenance': True, 'aicd_migrator': False, 'aicd_sysfn': True, 'aicd_worker': False}}`; после: `{'rls_forced_tables': 61, 'tables': 78, 'bypassrls': {'aicd_admin': True, 'aicd_app': False, 'aicd_maintenance': True, 'aicd_migrator': False, 'aicd_sysfn': True, 'aicd_worker': False}, 'probe': {'no_context': 0, 'ws_a_projects': 1, 'ws_b_project_seen_from_a': 0, 'ws_b_project_seen_from_b': 1}}`.

## Ограничения дрилла

* Одна машина: «авария» удаляет контейнеры, тома PostgreSQL/архива WAL/S3, секреты и рабочие каталоги, но репозиторий бэкапов и ключ — на том же хосте (в эксплуатации — внешнее хранилище и офлайн-хранилище ключа).
* CLI — Fake CLI (реальные Codex/Claude CLI в окружении не авторизованы); после настоящего восстановления оператор заново входит в CLI (тома учётных данных не бэкапятся, `docs/runbooks/cli-auth.md`).
* Gates исполняются LocalProcessRunner без gates «только в контейнере» (как в live-стенде E2E).
* Локальные Git-репозитории проектов — данные оператора вне бэкапа платформы (каталог `AICD_REPOS_DIR`).
