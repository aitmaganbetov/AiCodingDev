# ADR-0015: Inline-комментарии ревью кода (Code Review Workspace)

- **Статус:** Accepted (утверждено владельцем 2026-11-09: решение D-9 — «бэкенд-фазы B1–B8 одобрены, B5 — с ADR»,
  `docs/planning/OPEN_QUESTIONS.md`, журнал решений)
- **Дата:** 2026-11-09
- **Связано:** UI_REDESIGN_PLAN §3.5, §3.10 G-11, §7.2 (фаза B5); ADR-0004 (централизованный ACP, human approvals),
  ADR-0008 (tenancy/RLS), ADR-0011 (SSE из `run_event_log`); API_DESIGN §5, §12; миграция 0019.

## Контекст

Экран Code Review Workspace показывает diff задачи/run (artifact `git_diff`, Monaco) и находки. Человеку нужны
inline-комментарии к строкам diff с тредами, «resolve», правками и отправкой ревью («Approve» / «Request changes»).
В БД такой модели не было (G-11). Без неё UI честно предлагает только `CHANGE_REQUEST`-поручение с `файл:строка`
в тексте.

Ключевые риски новой модели:

1. **Prompt injection.** Текст комментария — недоверенные данные человека (может быть скопирован из чужого PR,
   содержать «ignore previous instructions», поддельные ограждения, секреты). Он не должен попадать агенту в обход
   конвейера поручений (impact analysis, подтверждение, checkpoint, ограждение) и никогда не исполняется.
2. **Обход approvals/gates.** «Approve» в редакторе кода не должен утверждать что-либо, минуя `approval:decide`
   (§5.3: роль, four-eyes, step-up) и Quality Gates.
3. **Tenancy и неотказуемость.** Комментарии — деловые записи с автором; правки не должны стирать сказанное.
4. **Дрейф якоря.** Commit-ы двигаются (fix-циклы, новые попытки): строка, к которой привязан комментарий, может
   больше не существовать в актуальном diff.

## Решение

### 1. Модель данных (миграция 0019, модуль `collaboration`)

| Таблица | Класс | Назначение |
|---|---|---|
| `review_threads` | T | Тред: `repository_id` (обязателен) + якорь `commit_sha` (head diff/commit файла), `base_commit_sha` (база diff; обязательна для `side = base`), `side` (`base`/`head`), `file_path` (относительный, без `..`/`/` в начале/управляющих символов, ≤ 1024), `start_line..end_line` (≥ 1, ≤ 1000 строк); опционально `run_id`, `task_id` (составной FK `tasks(workspace_id, run_id, id)` — задача только того же run), `finding_id` (находка того же run). Статус `OPEN ⇄ RESOLVED` (FSM `allowed_transitions('review_thread')`), `resolved_by/at`, `created_by`, `version` (ETag). |
| `review_comments` | T | Комментарий треда: `author_id`, `body` (1…8000), `revision` (ETag комментария), `edited_at`. Первый комментарий создаётся вместе с тредом. |
| `review_comment_revisions` | T, append-only | Прежнее тело при каждой правке (`revision`, `body`, `authored_at`, `superseded_at`, `superseded_by`). |
| `review_submissions` | T, append-only | Отправленное ревью run: `verdict` (`APPROVE` / `REQUEST_CHANGES` / `COMMENT`), `body`, `task_id?`, ссылка на результат — `approval_id` (APPROVE) или `directive_id` (REQUEST_CHANGES); CHECK связывает вердикт и ссылку. |
| `review_submission_threads` | T, append-only | Треды, включённые в ревью. |

Инварианты в БД (защита второго рубежа, не только в коде API):

* **Автор — только пользователь сессии:** `created_by`, `author_id`, `submitted_by`, `resolved_by` равны
  `app_current_user()` (триггеры, `42501`). Агент/воркер комментариев не пишет и не читает: у `aicd_worker` нет прав
  на таблицы, RLS-политика выдана только `aicd_app`.
* **Якорь неизменяем** (repository/run/task/finding/commit/side/path/lines/author): триггер `42501`. Перенос
  комментария на новый commit — новый тред.
* **Правки версионируются:** тело правит только автор; триггер копирует прежнее тело в `review_comment_revisions`
  и увеличивает `revision`. Ни одна прикладная роль не имеет `DELETE`; удаление строк запрещено триггером.
  Окна правки нет — вместо «окна» полная история (`GET /review-comments/{id}/revisions`), UI помечает
  «изменено». Текст, уже отправленный агентам через поручение, — копия в `chat_messages` поручения и правкой не
  меняется (п. 4).
* **Согласованность ссылок:** составные FK `(workspace_id, …)` на все tenant-ссылки (ADR-0008); run треда
  привязан к тому же репозиторию, находка — к тому же run (триггер).

### 2. Кто может комментировать (RBAC, согласовано с API_DESIGN §5)

| Право | owner | admin | developer | reviewer | viewer | Операции |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `review:read` | ✔ | ✔ | ✔ | ✔ | ✔ | список/тред/история правок/список ревью |
| `review:write` | ✔ | ✔ | ✔ | ✔ | — | создать тред, ответить, править **своё**, resolve/unresolve любого треда |
| `review:submit` | ✔ | ✔ | ✔ | ✔ (частично) | — | отправить ревью; `x-permission-depends-on: verdict` |

`review:write` совпадает с `chat:write` (reviewer+): комментарий — та же форма высказывания, что сообщение чата, и
сам по себе ничего не меняет. Ответ (reply) не выделяется в отдельное право: разделение «reviewer создаёт —
developer отвечает» противоречило бы иерархии ролей §5 (`developer ⊃ reviewer`). Правка — только автор (иначе
`403`, отказ аудируется `authz.denied`, `permission = review:write:own`). Resolve/unresolve — любой reviewer+
(как в распространённых code review системах): это отметка обсуждения, не решение по коду.

`review:submit` по вердикту:

* `COMMENT` — reviewer+ (только запись ревью);
* `REQUEST_CHANGES` — те же правила, что поручение `CHANGE_REQUEST` (§5.4: developer+; reviewer ⇒ `403`);
* `APPROVE` — только решение существующего `PENDING` approval этого run, по правилам §5.3
  (`can_decide_approval`: минимальная роль по `action_type`, four-eyes, step-up для критического набора).
  Без `approval_id` «Approve» не существует: ревью не утверждает merge/план/gates в обход approvals.

### 3. Отправка ревью и интеграция с review flow

`POST /runs/{run_id}/reviews` (`submitReview`) — единственная точка воздействия комментариев на исполнение:

* **APPROVE** → `Directives.submit(APPROVE, target = approval)` — тот же путь, что `POST /approvals/{id}/decision`
  и поручение APPROVE (ролевые правила, four-eyes, step-up, аудит `approval.decide`, хук `on_approval_decided`).
  Quality Gates не затрагиваются: approval `MANUAL_REVIEW`/`MERGE` решается человеком, gates — Verification Engine.
* **REQUEST_CHANGES** → `Directives.submit(CHANGE_REQUEST, target = task | run, requires_impact_preview)` — обычный
  конвейер поручений (impact analysis → подтверждение/approval `CRITICAL_DIRECTIVE` → checkpoint → новая
  `plan_version` → задачи замены/fix-задачи → доставка/ack, API_DESIGN §12). Текст поручения строится
  детерминированно (`aicd_collaboration.review_comments.change_request_message`): заголовок с id ревью,
  для каждого треда — `путь:строки @ commit (сторона)` и тела комментариев (редактированные, маркеры `<<<`/`>>>`
  нейтрализованы), лимит 8000 символов (иначе `422`, без молчаливого усечения).
* **COMMENT** → только запись `review_submissions` и событие.

Ревью требует не терминальный run (как поручения: иначе `409`); `thread_ids` — открытые треды этого run.

### 4. Доставка агентам

**Комментарий пользователя никогда не доставляется агенту автоматически.** Его нет в ACP, Context Pack и
инструкциях задач; воркер не имеет прав на таблицы (БД-барьер, тест). Агент получает текст только после
явного «Request changes» — как текст поручения `CHANGE_REQUEST`, который:

1. вставляется в `tasks.instructions` задач замены только внутри `fence_untrusted` (метка по id поручения,
   `<<<`/`>>>` нейтрализуются) и в раздел `directives` Context Pack как недоверенный фрагмент;
2. при сборке промпта целиком ещё раз ограждается одноразовым nonce (`build_prompt`, SC-INJ-01), сканер
   инъекций помечает `suspected_injection`;
3. не меняет политику, права, сеть, approvals (policy вне контроля модели, SC-INJ-02) и не исполняется
   платформой (никаких shell/eval по тексту комментария).

### 5. Аудит

Каждая команда пишет `audit_events` в той же транзакции (без тела комментария — только ссылки и якорь):
`review.thread.create`, `review.comment.create`, `review.comment.edit` (metadata `revision`),
`review.thread.resolve`, `review.thread.unresolve`, `review.submit` (metadata `verdict`, `thread_ids`,
`directive_id`/`approval_id`); вложенные действия — собственными событиями (`directive.submit`, `approval.decide`).
Отказы по роли/«не своё» — `authz.denied`.

### 6. События (SSE)

Outbox-событие `review.comment` (агрегат `review_thread`) на каждое изменение: `action`
(`thread_created`/`comment_created`/`comment_edited`/`thread_resolved`/`thread_unresolved`/`review_submitted`),
`thread_id`, `comment_id?`, `review_id?`, `repository_id`, `task_id?`, якорь, `status`, `actor_id`, `occurred_at`.
**Тело комментария в событие не входит** (клиент перечитывает тред по REST — там действует редакция и RBAC).
Проекция в `run_event_log` и SSE `GET /runs/{id}/events` — для тредов с `run_id` (домен
`sse_event_type_persisted_t` расширен миграцией 0019); треды только репозитория видны через REST.

### 7. Retention, tenancy, редакция секретов

* **Retention:** треды, комментарии, история правок и ревью — деловые записи (как findings/approvals): задачей
  retention не удаляются; архив run их не трогает (ссылаются на `runs`/`user_directives`, которые сохраняются);
  входят в логический бэкап (`aicd_maintenance` — `SELECT`). Удаление по запросу субъекта — будущая процедура
  `DATA_DELETION` (owner, four-eyes).
* **Tenancy:** `workspace_id` + RLS `ENABLE`/`FORCE`, составные FK, резолверы `sys_resolve_review_thread_workspace` /
  `sys_resolve_review_comment_workspace` (SECURITY DEFINER, только член workspace) для плоских URI; чужой ресурс —
  `404`, неотличимый от несуществующего (ST-TEN-04).
* **Редакция:** тело редактируется `redact_text` при записи и повторно на всех путях чтения (тред, список, история
  правок, текст поручения) — та же политика, что Live Agent Chat.

### 8. Дрейф якоря (outdated)

Якорь неизменяем; строки не перепривязываются (переотображение строк между commit-ами в v1 не делается). В ответе
API тред несёт `outdated` и `current_commit_sha`:

* если клиент передал `head_sha` (commit просматриваемого diff) — `outdated = commit_sha ≠ head_sha`;
* иначе для треда задачи — head-commit последнего worktree задачи, для треда run — последнего worktree run в этом
  репозитории; `outdated = commit_sha ≠ current_commit_sha`;
* текущий commit неизвестен — `outdated = null` (не «false»).

UI показывает устаревший тред на исходной позиции исходного commit с пометкой «устарел».

## Альтернативы

* **Комментарии как `chat_messages`** — чат привязан к run и потокам ролей, без якоря файла/строки и без resolve;
  правка сообщений чата не предусмотрена. Отвергнуто.
* **Автоматическая доставка комментариев агенту (ACP `QUESTION`/Context Pack)** — канал воздействия в обход impact
  analysis и подтверждения, расширяет поверхность prompt injection. Отвергнуто (п. 4).
* **Окно правки (например, 15 мин) без истории** — неотказуемость хуже, чем версионирование. Отвергнуто.
* **Перепривязка якоря по diff (line mapping)** — сложно и неточно без хранения diff; отложено, `outdated` честно
  сообщает о дрейфе.
* **«Approve» как самостоятельное утверждение** — обход §5.3 и gates. Отвергнуто.

## Последствия

+ Inline-обсуждение кода с историей и аудитом; воздействие на агентов — только через проверенный конвейер
  поручений и approvals. + Изоляция tenant и автор-инварианты продублированы в БД.
− Пять новых tenant-таблиц и 10 операций API; `outdated` вычисляется при чтении (без перепривязки строк);
SSE только для тредов run.

## Связанные требования

ТЗ §8.2 (поручения), §10 (недоверенные данные), §11 (Code Review), §15 (RBAC, аудит), §20 (prompt injection,
cross-tenant, secret leakage); AC-06, AC-12, AC-15; SC-INJ-01, SC-INJ-02, SC-INJ-05, SC-TEN-01, SC-TEN-04,
SC-SEC-03, SC-AUD-03; угроза TH-66 (THREAT_MODEL §5.11).
