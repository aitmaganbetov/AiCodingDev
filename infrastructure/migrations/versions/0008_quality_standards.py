"""Качество, evidence и стандарты (P6-01, P6-10): test_results, findings, evidence_links, реестр стандартов.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §12.5 (test_results), §12.6 (findings), §12.8
(evidence_links), §13.1–13.4 (standards, standard_controls, project_controls, compliance_results),
§18.3 (переходы finding), §18.11 (полиморфная ссылка evidence_links), §19.3–19.4 (RLS, классы T/G).
quality_profiles, quality_gates, gate_executions, evidence созданы миграцией 0003 и здесь расширяются
только дополнениями QUALITY_GATES.md §9, принятыми в §12.1/§12.4 документа:
  * gate_executions.error_code (TOOL_UNAVAILABLE, TIMEOUT, INFRA, SHA_MISMATCH, REPORT_UNPARSABLE,
    EMPTY_SUITE + коды Verification Engine WORKTREE_DIRTY, NO_BASE_COMMIT, UNSUPPORTED_GATE);
  * artifacts.kind: безымянный artifacts_kind_check (0001) заменён на ck_artifacts_kind
    с 'gate_log', 'applicability_report', 'signature'.
CREATE TABLE перенесены из документа без изменений. Дополнительно (защитный слой поверх кода):
  * allowed_transitions: ck_allowed_transitions_entity дополнен 'finding'; строки Finding FSM
    (в §12.6 FSM не было — добавлены в §18.3 документа этой миграцией) + trg_findings_transition;
  * trg_findings_guard: FALSE_POSITIVE/ACCEPTED_RISK — только человек из контекста сессии
    (resolved_by = app_current_user()), VERIFIED_FIXED — только с VERIFIED evidence;
  * trg_test_results_gate_consistency (§12.5) и обратная проверка на gate_executions (PASS при
    failed+errors > 0 невозможен);
  * test_results, evidence_links — append-only (UPDATE запрещён; удаление — каскад/retention);
  * compliance_results неизменяемы, кроме однократной установки superseded_at; PASS требует VERIFIED
    evidence того же run; gate_execution — того же run;
  * project_controls: утверждение (approved_by/approved_at) и изменение утверждённой строки — только
    человек из контекста сессии; система (worker) ведёт лишь черновики (§13.3).
SECURITY DEFINER (§19.5, как 0003/0005): sys_resolve_evidence_workspace(uuid) — workspace evidence только для
члена этого workspace (GET /evidence/{id}: не член ⇒ 404, AC-12); владелец aicd_sysfn, EXECUTE — только aicd_app.
FK, отложенные до Phase 6 (заголовок 0007: evidence_links ↔ architecture_decision), реализованы
триггером §18.11 trg_evidence_links_target (статический CASE по target_type, цель — в том же workspace);
'pull_request' проверяется динамически, когда появится таблица pull_requests (Phase 8), до этого отклоняется.
Seed (P6-10, RISK-13): standards и standard_controls — только идентификаторы, редакции, ссылки на разделы
(номера глав ASVS, CWE ID, практики SSDF, характеристики ISO/IEC 25010) и **собственные краткие
формулировки платформы**; тексты стандартов не хранятся. quality_gates не сидируются (P6-02/P6-03).
Классы (§19.4): standards, standard_controls — G (только SELECT app/worker, запись — миграции);
остальные 6 таблиц — T (RLS ENABLE+FORCE, политика p_<t>_tenant для app/worker).
Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).

Обратимость: downgrade удаляет новые таблицы, функции, строки finding в allowed_transitions,
колонку gate_executions.error_code и возвращает CHECK allowed_transitions/artifacts к виду 0007
(строки artifacts новых видов должны быть удалены заранее).
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

LICENSE_NOTE = (
    "Хранятся только код, редакция и ссылки на разделы. Формулировки контролей — собственные краткие "
    "формулировки платформы, не текст стандарта. Редакция требует подтверждения при утверждении "
    "применимости для проекта. Автоматические проверки не являются сертификацией."
)

# (id, code, edition, title — описание платформы, reference_uri)
STANDARDS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("01", "ISO/IEC/IEEE 29148", "2018", "Инженерия требований", None),
    ("02", "ISO/IEC/IEEE 42010", "2022", "Описание архитектуры", None),
    ("03", "ISO/IEC/IEEE 12207", "2017", "Процессы жизненного цикла ПО", None),
    ("04", "ISO/IEC 25010", "2023", "Модель качества продукта", None),
    ("05", "ISO/IEC 25023", "2016", "Измерение качества продукта", None),
    ("06", "IEEE 1028", "2008", "Ревью и аудиты ПО", None),
    ("07", "ISO/IEC 27001", "2022", "Система менеджмента информационной безопасности", None),
    ("08", "ISO/IEC 27002", "2022", "Меры обеспечения информационной безопасности", None),
    (
        "09",
        "NIST SP 800-218",
        "1.1",
        "Безопасная разработка ПО (SSDF)",
        "https://csrc.nist.gov/pubs/sp/800/218/final",
    ),
    (
        "10",
        "OWASP ASVS",
        "4.0.3",
        "Проверка безопасности приложений",
        "https://owasp.org/www-project-application-security-verification-standard/",
    ),
    ("11", "CWE", "4", "Перечень слабостей ПО", "https://cwe.mitre.org/"),
    ("12", "ISO/IEC/IEEE 29119", "2022", "Тестирование ПО", None),
    ("13", "ISO/IEC 9075", "2023", "Язык SQL", None),
    ("14", "SLSA", "1.0", "Целостность цепочки поставки сборки", "https://slsa.dev/"),
    ("15", "OpenSSF Scorecard", "5", "Практики безопасности open source", "https://openssf.org/"),
    ("16", "ISO/IEC/IEEE 14764", "2022", "Сопровождение ПО", None),
    ("17", "ISO/IEC/IEEE 15289", "2019", "Информационные элементы жизненного цикла", None),
    ("18", "ISO/IEC/IEEE 26514", "2022", "Пользовательская документация", None),
)

# (id, standard code, control_code, clause_ref, description — формулировка платформы, verification_method,
#  default_agent_role, default_gate_type). Правила применимости — aicd_standards.catalog (по control_code).
CONTROLS: tuple[tuple[str, str, str, str, str, str, str, str | None], ...] = (
    ("01", "ISO/IEC/IEEE 29148", "REQ-29148-01", "requirement attributes",
     "У каждого требования есть уникальный ID и хотя бы один проверяемый критерий приёмки.",
     "document_check", "requirements", "POLICY"),
    ("02", "ISO/IEC/IEEE 29148", "REQ-29148-02", "requirements traceability",
     "Каждое требование прослеживается до задачи и теста.",
     "automated_gate", "requirements", "POLICY"),
    ("03", "ISO/IEC/IEEE 42010", "ARCH-42010-01", "architecture decisions and rationale",
     "Для значимого архитектурного решения есть ADR: контекст, варианты, последствия.",
     "document_check", "architect", "POLICY"),
    ("04", "ISO/IEC/IEEE 42010", "ARCH-42010-02", "stakeholders and concerns",
     "Перечислены заинтересованные стороны и их ключевые вопросы к архитектуре.",
     "manual", "architect", None),
    ("05", "ISO/IEC/IEEE 12207", "LC-12207-01", "technical processes",
     "Изменение прошло цикл план → реализация → проверка → ревью, и каждый шаг оставил запись.",
     "automated_gate", "planner", "REVIEW"),
    ("06", "ISO/IEC 25010", "QM-25010-01", "maintainability",
     "Изменённый код проходит линтер без нарушений.",
     "automated_gate", "qa", "LINT"),
    ("07", "ISO/IEC 25010", "QM-25010-02", "maintainability",
     "Форматирование изменённого кода соответствует принятому в проекте.",
     "automated_gate", "qa", "FORMAT"),
    ("08", "ISO/IEC 25010", "QM-25010-03", "maintainability",
     "Статическая проверка типов проходит без ошибок.",
     "automated_gate", "qa", "TYPECHECK"),
    ("09", "ISO/IEC 25010", "QM-25010-04", "reliability",
     "Изменённая функциональность покрыта автотестами, и они проходят на commit run.",
     "automated_gate", "qa", "UNIT"),
    ("10", "ISO/IEC 25010", "QM-25010-05", "performance efficiency",
     "Заявленное ускорение подтверждено воспроизводимым замером.",
     "automated_gate", "performance", "PERF"),
    ("11", "ISO/IEC 25023", "QM-25023-01", "maintainability measures",
     "Покрытие ядра тестами измерено и не ниже порога профиля.",
     "automated_gate", "qa", "COVERAGE"),
    ("12", "IEEE 1028", "REV-1028-01", "technical review",
     "Ревью выполнил не автор изменения; замечания и вердикт записаны.",
     "automated_gate", "reviewer", "REVIEW"),
    ("13", "ISO/IEC 27001", "SEC-27001-01", "Annex A 8.4",
     "Изменения не выходят за разрешённую область рабочего дерева.",
     "automated_gate", "security", "SCOPE"),
    ("14", "ISO/IEC 27002", "SEC-27002-01", "5.17",
     "В репозитории и диффе нет секретов: ключей, паролей, токенов.",
     "automated_gate", "security", "SECRETS"),
    ("15", "ISO/IEC 27002", "SEC-27002-02", "8.28",
     "Код проверен статическим анализатором безопасности без блокирующих находок.",
     "automated_gate", "security", "SAST"),
    ("16", "NIST SP 800-218", "SSDF-RV.1-01", "RV.1",
     "Зависимости проверены на известные уязвимости до выпуска.",
     "automated_gate", "security", "SCA"),
    ("17", "NIST SP 800-218", "SSDF-PW.7-01", "PW.7",
     "Исходный код проанализирован на уязвимости автоматическим анализатором.",
     "automated_gate", "security", "SAST"),
    ("18", "NIST SP 800-218", "SSDF-PW.8-01", "PW.8",
     "Исполняемый код протестирован, результат сохранён как доказательство.",
     "automated_gate", "qa", "UNIT"),
    ("19", "OWASP ASVS", "ASVS-V2-01", "V2",
     "Эндпоинты входа защищены от перебора: лимит попыток или блокировка.",
     "agent_review", "security", "REVIEW"),
    ("20", "OWASP ASVS", "ASVS-V5-01", "V5",
     "Входные данные проверяются на границе доверия, вывод кодируется по контексту.",
     "agent_review", "security", "REVIEW"),
    ("21", "OWASP ASVS", "ASVS-V14-01", "V14",
     "Конфигурация контейнеров и IaC проверена на небезопасные настройки.",
     "automated_gate", "devops", "IAC"),
    ("22", "CWE", "CWE-INJ-01", "CWE-89, CWE-78",
     "Нет открытых находок класса инъекций (SQL, команды ОС) уровня high и выше.",
     "automated_gate", "security", "SAST"),
    ("23", "CWE", "CWE-XSS-01", "CWE-79",
     "Нет открытых находок межсайтового скриптинга уровня high и выше.",
     "automated_gate", "security", "SAST"),
    ("24", "CWE", "CWE-PATH-01", "CWE-22",
     "Нет открытых находок выхода за пределы каталога уровня high и выше.",
     "automated_gate", "security", "SAST"),
    ("25", "ISO/IEC/IEEE 29119", "TEST-29119-01", "test documentation",
     "Есть план тестирования, связанный с критериями приёмки, и отчёт о его выполнении.",
     "document_check", "qa", "POLICY"),
    ("26", "ISO/IEC/IEEE 29119", "TEST-29119-02", "dynamic testing",
     "Интеграционные тесты выполнены на commit run и проходят.",
     "automated_gate", "qa", "INTEGRATION"),
    ("27", "ISO/IEC 9075", "SQL-9075-01", "schema definition",
     "Миграции схемы применяются и откатываются на чистой базе без ошибок.",
     "automated_gate", "database", "MIGRATION"),
    ("28", "SLSA", "SLSA-BUILD-01", "Build track",
     "Сборка выполняется в контролируемом окружении и воспроизводима.",
     "automated_gate", "devops", "BUILD"),
    ("29", "SLSA", "SLSA-PROV-01", "provenance",
     "Артефакт выпуска подписан, подпись проверяема.",
     "automated_gate", "devops", "SIGN"),
    ("30", "OpenSSF Scorecard", "OSSF-SBOM-01", "SBOM check",
     "Для выпуска сформирован перечень компонентов (SBOM).",
     "automated_gate", "devops", "SBOM"),
    ("31", "ISO/IEC/IEEE 14764", "MAINT-14764-01", "modification analysis",
     "Для изменения при сопровождении записан анализ влияния.",
     "document_check", "maintenance", "POLICY"),
    ("32", "ISO/IEC/IEEE 15289", "DOC-15289-01", "information items",
     "Созданы и сохранены обязательные артефакты процесса: план и отчёт проверки.",
     "document_check", "documentation", "POLICY"),
    ("33", "ISO/IEC/IEEE 26514", "DOC-26514-01", "information for users",
     "Изменение публичного API или поведения отражено в документации.",
     "agent_review", "documentation", "REVIEW"),
)  # fmt: skip

GATE_TYPES = (
    "'FORMAT','LINT','TYPECHECK','BUILD','UNIT','INTEGRATION','E2E','SAST','SCA','SECRETS',"
    "'IAC','POLICY','MIGRATION','PERF','REVIEW','COVERAGE','SBOM','SIGN','SCOPE'"
)
ARTIFACT_KINDS_0001 = (
    "'git_diff','cli_raw_log','cli_output','test_report','coverage_report','scan_report','sbom',"
    "'context_pack','checkpoint_state','documentation','knowledge','report','archive','other'"
)
ARTIFACT_KINDS = ARTIFACT_KINDS_0001 + ",'gate_log','applicability_report','signature'"


def standard_id(n: str) -> str:
    return f"01a1c008-0000-7000-8000-0000000000{n}"


def control_id(n: str) -> str:
    return f"01a1c008-0001-7000-8000-0000000000{n}"


def _q(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


TABLES = rf"""
-- §12.5 test_results (T)
CREATE TABLE test_results (
  id                  uuid7        NOT NULL,
  workspace_id        uuid         NOT NULL,
  gate_execution_id   uuid         NOT NULL,
  suite               text         NOT NULL CHECK (length(suite) BETWEEN 1 AND 500),
  passed              int          NOT NULL CHECK (passed >= 0),
  failed              int          NOT NULL CHECK (failed >= 0),
  skipped             int          NOT NULL CHECK (skipped >= 0),
  errors              int          NOT NULL DEFAULT 0 CHECK (errors >= 0),
  duration_ms         bigint       CHECK (duration_ms >= 0),
  coverage_percent    numeric(5,2) CHECK (coverage_percent BETWEEN 0 AND 100),
  report_artifact_id  uuid         NOT NULL,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_test_results PRIMARY KEY (id),
  CONSTRAINT uq_test_results_suite UNIQUE (gate_execution_id, suite),
  CONSTRAINT fk_test_results_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_test_results_report FOREIGN KEY (workspace_id, report_artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT
);

-- §12.6 findings (T)
CREATE TABLE findings (
  id                   uuid7              NOT NULL,
  workspace_id         uuid               NOT NULL,
  run_id               uuid               NOT NULL,
  task_id              uuid,
  source               text               NOT NULL CHECK (source IN ('gate','agent_review','manual','github')),
  source_gate_id       uuid,              -- определение gate (quality_gates.id)
  gate_execution_id    uuid,              -- конкретный запуск gate
  severity             finding_severity_t NOT NULL,
  rule_id              text               NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 255),
  cwe_id               text               CHECK (cwe_id ~ '^CWE-[0-9]{{1,5}}$'),
  title                text               NOT NULL CHECK (length(title) BETWEEN 1 AND 1000),
  file_path            text               CHECK (file_path !~ '(^/|(^|/)\.\.(/|$))'),
  line                 int                CHECK (line >= 1),
  status               finding_status_t   NOT NULL DEFAULT 'OPEN',
  fingerprint          sha256_hex         NOT NULL,     -- дедупликация: rule_id + путь + нормализованный фрагмент
  evidence_id          uuid,
  fixed_in_commit_sha  git_sha,
  resolved_by          uuid,
  resolution_reason    text               CHECK (length(resolution_reason) <= 4000),
  created_at           timestamptz        NOT NULL DEFAULT now(),
  updated_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_findings PRIMARY KEY (id),
  CONSTRAINT uq_findings_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_findings_run_fingerprint UNIQUE (run_id, fingerprint),
  CONSTRAINT fk_findings_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate FOREIGN KEY (source_gate_id) REFERENCES quality_gates(id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate_execution_gate FOREIGN KEY (gate_execution_id, source_gate_id)
      REFERENCES gate_executions(id, gate_id) ON DELETE RESTRICT,     -- согласованность: запуск принадлежит именно этому gate
  CONSTRAINT fk_findings_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_resolved_by FOREIGN KEY (resolved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_findings_gate_source CHECK ((source = 'gate') = (gate_execution_id IS NOT NULL AND source_gate_id IS NOT NULL)),
  CONSTRAINT ck_findings_line CHECK (line IS NULL OR file_path IS NOT NULL),
  CONSTRAINT ck_findings_verified_fixed CHECK (status <> 'VERIFIED_FIXED' OR (evidence_id IS NOT NULL AND fixed_in_commit_sha IS NOT NULL)),
  CONSTRAINT ck_findings_human_resolution CHECK (
      status NOT IN ('FALSE_POSITIVE','ACCEPTED_RISK') OR (resolved_by IS NOT NULL AND resolution_reason IS NOT NULL))
);
CREATE INDEX ix_findings_run_open ON findings (workspace_id, run_id, severity) WHERE status IN ('OPEN','ACKNOWLEDGED','FIXED');
CREATE INDEX ix_findings_task ON findings (workspace_id, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX ix_findings_gate_execution ON findings (gate_execution_id) WHERE gate_execution_id IS NOT NULL;

-- §12.8 evidence_links (T)
CREATE TABLE evidence_links (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  evidence_id   uuid        NOT NULL,
  target_type   text        NOT NULL CHECK (target_type IN
                  ('task','gate_execution','finding','compliance_result','test_result','agent_message','approval',
                   'pull_request','architecture_decision','agent_decision')),
  target_id     uuid        NOT NULL,
  relation      text        NOT NULL CHECK (relation IN ('supports','refutes','produced_by','verifies','references')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_evidence_links PRIMARY KEY (id),
  CONSTRAINT uq_evidence_links UNIQUE (evidence_id, target_type, target_id, relation),
  CONSTRAINT fk_evidence_links_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_evidence_links_target ON evidence_links (workspace_id, target_type, target_id);

-- §13.1 standards (G)
CREATE TABLE standards (
  id             uuid7       NOT NULL,
  code           text        NOT NULL CHECK (code ~ '^[A-Za-z0-9/ ._:-]{{2,100}}$'),   -- 'ISO/IEC 25010', 'OWASP ASVS', 'NIST SP 800-218'
  edition        text        NOT NULL CHECK (length(edition) BETWEEN 1 AND 50),       -- '2023', '4.0.3', '1.1'
  title          text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  reference_uri  text        CHECK (reference_uri ~ '^https://'),
  license_note   text        CHECK (length(license_note) <= 2000),
  active         boolean     NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_standards PRIMARY KEY (id),
  CONSTRAINT uq_standards_code_edition UNIQUE (code, edition)
);

-- §13.2 standard_controls (G)
CREATE TABLE standard_controls (
  id                   uuid7        NOT NULL,
  standard_id          uuid         NOT NULL,
  clause_ref           text         NOT NULL CHECK (length(clause_ref) BETWEEN 1 AND 100),
  control_code         text         NOT NULL CHECK (control_code ~ '^[A-Za-z0-9._-]{{1,64}}$'),
  description          text         NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
  verification_method  text         NOT NULL CHECK (verification_method IN ('automated_gate','agent_review','manual','document_check')),
  default_agent_role   agent_role_t,
  default_gate_type    text         CHECK (default_gate_type IN
                         ({GATE_TYPES})),
  active               boolean      NOT NULL DEFAULT true,
  created_at           timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_standard_controls PRIMARY KEY (id),
  CONSTRAINT uq_standard_controls_code UNIQUE (standard_id, control_code),
  CONSTRAINT fk_standard_controls_standard FOREIGN KEY (standard_id) REFERENCES standards(id) ON DELETE RESTRICT,
  CONSTRAINT ck_standard_controls_automated CHECK (verification_method = 'manual' OR default_gate_type IS NOT NULL)
);

-- §13.3 project_controls (T)
CREATE TABLE project_controls (
  workspace_id   uuid         NOT NULL,
  project_id     uuid         NOT NULL,
  control_id     uuid         NOT NULL,
  applicability  text         NOT NULL CHECK (applicability IN ('applicable','not_applicable','tailored')),
  rationale      text         CHECK (length(rationale) <= 4000),
  agent_role     agent_role_t,             -- ответственная роль (трассировка ТЗ §13)
  approved_by    uuid,
  approved_at    timestamptz,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  updated_at     timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_controls PRIMARY KEY (project_id, control_id),
  CONSTRAINT fk_project_controls_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_controls_control FOREIGN KEY (control_id) REFERENCES standard_controls(id) ON DELETE RESTRICT,
  CONSTRAINT fk_project_controls_approved_by FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_project_controls_rationale CHECK (applicability = 'applicable' OR rationale IS NOT NULL),
  CONSTRAINT ck_project_controls_approval CHECK ((approved_by IS NULL) = (approved_at IS NULL))
);
CREATE INDEX ix_project_controls_ws_project ON project_controls (workspace_id, project_id) WHERE approved_by IS NOT NULL;

-- §13.4 compliance_results (T)
CREATE TABLE compliance_results (
  id                 uuid7               NOT NULL,
  workspace_id       uuid                NOT NULL,
  run_id             uuid                NOT NULL,
  control_id         uuid                NOT NULL,
  status             compliance_status_t NOT NULL,
  evidence_id        uuid,
  gate_execution_id  uuid,
  reviewed_by        uuid,
  notes              text                CHECK (length(notes) <= 4000),
  checked_at         timestamptz         NOT NULL DEFAULT now(),
  superseded_at      timestamptz,
  CONSTRAINT pk_compliance_results PRIMARY KEY (id),
  CONSTRAINT uq_compliance_results_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_compliance_results_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_control FOREIGN KEY (control_id) REFERENCES standard_controls(id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_reviewed_by FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_compliance_results_pass_evidence CHECK (status NOT IN ('PASS','FAIL') OR evidence_id IS NOT NULL),
  CONSTRAINT ck_compliance_results_na CHECK (status <> 'NOT_APPLICABLE' OR notes IS NOT NULL)
);
CREATE UNIQUE INDEX uq_compliance_results_current ON compliance_results (run_id, control_id) WHERE superseded_at IS NULL;
CREATE INDEX ix_compliance_results_run ON compliance_results (workspace_id, run_id, status);

-- Дополнения QUALITY_GATES.md §9 к таблицам 0001/0003 (§12.1, §12.4 документа)
ALTER TABLE gate_executions ADD COLUMN error_code text CONSTRAINT ck_gate_executions_error_code CHECK (error_code IN
  ('TOOL_UNAVAILABLE','TIMEOUT','INFRA','SHA_MISMATCH','REPORT_UNPARSABLE','EMPTY_SUITE','WORKTREE_DIRTY',
   'NO_BASE_COMMIT','UNSUPPORTED_GATE','APPROVAL_REQUIRED','THRESHOLD','EVIDENCE_REJECTED','STALE_EVIDENCE'));
ALTER TABLE artifacts DROP CONSTRAINT artifacts_kind_check;
ALTER TABLE artifacts ADD CONSTRAINT ck_artifacts_kind CHECK (kind IN ({ARTIFACT_KINDS}));
"""

TRIGGERS = r"""
-- Стандартные триггеры (§1.5, §4)
CREATE TRIGGER trg_findings_updated_at BEFORE UPDATE ON findings FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_project_controls_updated_at BEFORE UPDATE ON project_controls FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['test_results','findings','evidence_links','project_controls','compliance_results']
  LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF workspace_id ON %I '
                   'FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace()', 'trg_' || t || '_immutable_ws', t);
  END LOOP;
END $$;
-- Append-only (результаты процесса и трассировка не переписываются; удаление — каскад evidence/retention).
CREATE TRIGGER trg_test_results_append_only BEFORE UPDATE ON test_results
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
CREATE TRIGGER trg_evidence_links_append_only BEFORE UPDATE ON evidence_links
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();

-- §18.3: Finding FSM (QUALITY_GATES §5.3: FIXED требует VERIFIED_FIXED; FALSE_POSITIVE/ACCEPTED_RISK — человек).
ALTER TABLE allowed_transitions DROP CONSTRAINT ck_allowed_transitions_entity;
ALTER TABLE allowed_transitions ADD CONSTRAINT ck_allowed_transitions_entity
  CHECK (entity IN ('run','task','execution','directive','approval','external_action','indexing_job','finding'));
INSERT INTO allowed_transitions (entity, from_status, to_status)
SELECT 'finding', v.from_status, unnest(v.to_statuses)
  FROM (VALUES
    ('__new__',        ARRAY['OPEN']),
    ('OPEN',           ARRAY['ACKNOWLEDGED','FIXED','VERIFIED_FIXED','FALSE_POSITIVE','ACCEPTED_RISK']),
    ('ACKNOWLEDGED',   ARRAY['OPEN','FIXED','VERIFIED_FIXED','FALSE_POSITIVE','ACCEPTED_RISK']),
    ('FIXED',          ARRAY['VERIFIED_FIXED','OPEN']),   -- OPEN: повторный прогон gate воспроизвёл находку
    ('VERIFIED_FIXED', ARRAY['OPEN']),                    -- регрессия на более позднем commit run
    ('FALSE_POSITIVE', ARRAY['OPEN']),                    -- решение человека отозвано
    ('ACCEPTED_RISK',  ARRAY['OPEN'])                     -- принятие риска отозвано/истекло
  ) AS v(from_status, to_statuses);
CREATE TRIGGER trg_findings_transition BEFORE INSERT OR UPDATE OF status ON findings
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('finding');

-- §12.6: закрыть находку как FALSE_POSITIVE/ACCEPTED_RISK может только человек — пользователь сессии
-- (агент/система без app.user_id не может, ТЗ §10); VERIFIED_FIXED опирается на VERIFIED evidence.
CREATE FUNCTION trg_fn_findings_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('FALSE_POSITIVE','ACCEPTED_RISK')
     AND (TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status
          OR NEW.resolved_by IS DISTINCT FROM OLD.resolved_by)
     AND NEW.resolved_by IS DISTINCT FROM app_current_user() THEN
    RAISE EXCEPTION 'finding %: % requires resolution by the session user', NEW.id, NEW.status USING ERRCODE = '42501';
  END IF;
  IF NEW.status = 'VERIFIED_FIXED' AND NOT EXISTS (
       SELECT 1 FROM evidence e WHERE e.workspace_id = NEW.workspace_id AND e.id = NEW.evidence_id
          AND e.verification_status = 'VERIFIED') THEN
    RAISE EXCEPTION 'finding %: VERIFIED_FIXED requires VERIFIED evidence', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_findings_guard BEFORE INSERT OR UPDATE OF status, resolved_by, evidence_id ON findings
  FOR EACH ROW EXECUTE FUNCTION trg_fn_findings_guard();

-- §12.5: если failed + errors > 0, gate_execution не может быть PASS (AC-11) — проверка с обеих сторон.
CREATE FUNCTION trg_fn_test_results_gate_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.failed + NEW.errors > 0 AND EXISTS (
       SELECT 1 FROM gate_executions g WHERE g.workspace_id = NEW.workspace_id AND g.id = NEW.gate_execution_id
          AND g.status = 'PASS') THEN
    RAISE EXCEPTION 'test results with failures contradict PASS of gate execution %', NEW.gate_execution_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_test_results_gate_consistency BEFORE INSERT ON test_results
  FOR EACH ROW EXECUTE FUNCTION trg_fn_test_results_gate_consistency();
CREATE FUNCTION trg_fn_gate_executions_test_results() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'PASS' AND EXISTS (
       SELECT 1 FROM test_results t WHERE t.workspace_id = NEW.workspace_id AND t.gate_execution_id = NEW.id
          AND t.failed + t.errors > 0) THEN
    RAISE EXCEPTION 'gate execution %: PASS contradicts failed tests', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_gate_executions_test_results BEFORE UPDATE OF status ON gate_executions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_gate_executions_test_results();

-- §18.11: полиморфная ссылка evidence_links(target_type, target_id) — цель существует в том же workspace.
-- Функция исполняется с правами вызывающего: под RLS цель чужого workspace не видна.
CREATE FUNCTION trg_fn_evidence_links_target() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ok boolean;
BEGIN
  CASE NEW.target_type
    WHEN 'task' THEN
      SELECT EXISTS (SELECT 1 FROM tasks t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'gate_execution' THEN
      SELECT EXISTS (SELECT 1 FROM gate_executions t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'finding' THEN
      SELECT EXISTS (SELECT 1 FROM findings t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'compliance_result' THEN
      SELECT EXISTS (SELECT 1 FROM compliance_results t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'test_result' THEN
      SELECT EXISTS (SELECT 1 FROM test_results t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'agent_message' THEN
      SELECT EXISTS (SELECT 1 FROM agent_messages t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'approval' THEN
      SELECT EXISTS (SELECT 1 FROM approvals t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'architecture_decision' THEN
      SELECT EXISTS (SELECT 1 FROM architecture_decisions t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'agent_decision' THEN
      SELECT EXISTS (SELECT 1 FROM agent_decisions t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.target_id) INTO v_ok;
    WHEN 'pull_request' THEN
      -- pull_requests появится в Phase 8 (§16.3); до этого ссылка отклоняется.
      IF to_regclass('pull_requests') IS NULL THEN
        v_ok := false;
      ELSE
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM pull_requests t WHERE t.workspace_id = $1 AND t.id = $2)'
          INTO v_ok USING NEW.workspace_id, NEW.target_id;
      END IF;
    ELSE
      v_ok := false;
  END CASE;
  IF NOT v_ok THEN
    RAISE EXCEPTION 'evidence_links: % % not found in this workspace', NEW.target_type, NEW.target_id
      USING ERRCODE = '23503';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_evidence_links_target BEFORE INSERT ON evidence_links
  FOR EACH ROW EXECUTE FUNCTION trg_fn_evidence_links_target();

-- §13.4: результат неизменяем (новый вердикт — новая строка + superseded_at прежней); PASS опирается на
-- VERIFIED evidence; evidence и gate_execution принадлежат тому же run.
CREATE FUNCTION trg_fn_compliance_results_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF (to_jsonb(NEW) - 'superseded_at') IS DISTINCT FROM (to_jsonb(OLD) - 'superseded_at')
       OR OLD.superseded_at IS NOT NULL OR NEW.superseded_at IS NULL THEN
      RAISE EXCEPTION 'compliance results are immutable except setting superseded_at once' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.evidence_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM evidence e WHERE e.workspace_id = NEW.workspace_id AND e.id = NEW.evidence_id
          AND e.run_id = NEW.run_id AND (NEW.status <> 'PASS' OR e.verification_status = 'VERIFIED')) THEN
    RAISE EXCEPTION 'compliance result: evidence % is not VERIFIED evidence of run %', NEW.evidence_id, NEW.run_id
      USING ERRCODE = '23514';
  END IF;
  IF NEW.gate_execution_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM gate_executions g WHERE g.workspace_id = NEW.workspace_id AND g.id = NEW.gate_execution_id
          AND g.run_id = NEW.run_id) THEN
    RAISE EXCEPTION 'compliance result: gate execution % does not belong to run %', NEW.gate_execution_id, NEW.run_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_compliance_results_guard BEFORE INSERT OR UPDATE ON compliance_results
  FOR EACH ROW EXECUTE FUNCTION trg_fn_compliance_results_guard();

-- §13.3: применимость утверждает человек (ТЗ §13). Утвердить, изменить или удалить утверждённую строку
-- может только пользователь сессии (approved_by = app_current_user()); система ведёт лишь черновики.
CREATE FUNCTION trg_fn_project_controls_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('UPDATE','DELETE') AND OLD.approved_by IS NOT NULL AND app_current_user() IS NULL THEN
    RAISE EXCEPTION 'approved applicability of control % can be changed only by a user', OLD.control_id
      USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.approved_by IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
          OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
          OR NEW.applicability IS DISTINCT FROM OLD.applicability
          OR NEW.rationale IS DISTINCT FROM OLD.rationale
          OR NEW.agent_role IS DISTINCT FROM OLD.agent_role)
     AND NEW.approved_by IS DISTINCT FROM app_current_user() THEN
    RAISE EXCEPTION 'applicability of control % must be approved by the session user', NEW.control_id
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_project_controls_approval BEFORE INSERT OR UPDATE OR DELETE ON project_controls
  FOR EACH ROW EXECUTE FUNCTION trg_fn_project_controls_approval();
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4); standards/standard_controls — G (без RLS, доступ ограничен GRANT).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['test_results','findings','evidence_links','project_controls','compliance_results']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;
"""

GRANTS = r"""
REVOKE ALL ON test_results, findings, evidence_links, standards, standard_controls, project_controls,
              compliance_results
  FROM PUBLIC;

-- G-справочник стандартов (§19.4, OQ-DB-15): чтение app/worker; запись — только миграции/admin CLI.
GRANT SELECT ON standards, standard_controls TO aicd_app, aicd_worker;

-- api: отчёты качества/compliance (GET /runs/{id}/quality|compliance|report, /evidence/{id});
-- решения человека по находкам (ACKNOWLEDGED, FALSE_POSITIVE, ACCEPTED_RISK, повторное открытие);
-- утверждение применимости контролей для проекта (admin, ТЗ §13).
GRANT SELECT ON test_results, findings, evidence_links, project_controls, compliance_results TO aicd_app;
GRANT UPDATE (status, resolved_by, resolution_reason) ON findings TO aicd_app;
GRANT INSERT, UPDATE ON project_controls TO aicd_app;

-- workers (worker-verify, compliance): результаты прогонов, находки, трассировка, вердикты контролей.
-- test_results/evidence_links только добавляются; compliance_results — новая строка + superseded_at.
GRANT SELECT, INSERT ON test_results, evidence_links, compliance_results TO aicd_worker;
GRANT SELECT, INSERT, UPDATE ON findings TO aicd_worker;
GRANT UPDATE (superseded_at) ON compliance_results TO aicd_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON project_controls TO aicd_worker;   -- только черновики (триггер)

-- maintenance: retention и архив run (§20.2–20.3).
GRANT SELECT ON test_results, findings, evidence_links, standards, standard_controls, project_controls,
               compliance_results
  TO aicd_maintenance;
"""


SYSFN = r"""
-- §19.5: workspace evidence, только если текущий пользователь — член этого workspace (GET /evidence/{id}).
CREATE FUNCTION sys_resolve_evidence_workspace(p_evidence_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT e.workspace_id
    FROM aicd.evidence e
    JOIN aicd.workspace_members m ON m.workspace_id = e.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE e.id = p_evidence_id
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_evidence_workspace(uuid) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_evidence_workspace(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_evidence_workspace(uuid) TO aicd_app;
-- Только колонки, которых у aicd_sysfn ещё не было (downgrade отзывает ровно их).
GRANT SELECT (id, workspace_id) ON evidence TO aicd_sysfn;
"""


def seed_sql() -> str:
    standards = ",\n".join(
        f"  ('{standard_id(n)}', {_q(code)}, {_q(edition)}, {_q(title)}, {_q(uri)}, {_q(LICENSE_NOTE)})"
        for n, code, edition, title, uri in STANDARDS
    )
    by_code = {code: standard_id(n) for n, code, *_ in STANDARDS}
    controls = ",\n".join(
        f"  ('{control_id(n)}', '{by_code[std]}', {_q(clause)}, {_q(code)}, {_q(desc)}, {_q(method)}, {_q(role)},"
        f" {_q(gate)})"
        for n, std, code, clause, desc, method, role, gate in CONTROLS
    )
    return (
        "-- Seed реестра стандартов (P6-10, §13.1–13.2): собственные формулировки платформы (RISK-13).\n"
        "INSERT INTO standards (id, code, edition, title, reference_uri, license_note) VALUES\n"
        f"{standards};\n"
        "INSERT INTO standard_controls (id, standard_id, clause_ref, control_code, description,"
        " verification_method, default_agent_role, default_gate_type) VALUES\n"
        f"{controls};\n"
    )


DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_resolve_evidence_workspace(uuid);
REVOKE SELECT (id, workspace_id) ON evidence FROM aicd_sysfn;
DROP TABLE IF EXISTS compliance_results, project_controls, standard_controls, standards, evidence_links, findings,
                     test_results CASCADE;
DROP TRIGGER IF EXISTS trg_gate_executions_test_results ON gate_executions;
DROP FUNCTION IF EXISTS trg_fn_gate_executions_test_results();
DROP FUNCTION IF EXISTS trg_fn_test_results_gate_consistency();
DROP FUNCTION IF EXISTS trg_fn_findings_guard();
DROP FUNCTION IF EXISTS trg_fn_evidence_links_target();
DROP FUNCTION IF EXISTS trg_fn_compliance_results_guard();
DROP FUNCTION IF EXISTS trg_fn_project_controls_approval();
ALTER TABLE gate_executions DROP COLUMN IF EXISTS error_code;
ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS ck_artifacts_kind;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_kind_check CHECK (kind IN
  ('git_diff','cli_raw_log','cli_output','test_report','coverage_report','scan_report','sbom',
   'context_pack','checkpoint_state','documentation','knowledge','report','archive','other'));

DELETE FROM allowed_transitions WHERE entity = 'finding';
ALTER TABLE allowed_transitions DROP CONSTRAINT IF EXISTS ck_allowed_transitions_entity;
ALTER TABLE allowed_transitions ADD CONSTRAINT ck_allowed_transitions_entity CHECK (entity IN
  ('run','task','execution','directive','approval','external_action','indexing_job'));
"""


def upgrade() -> None:
    for block in (TABLES, TRIGGERS, RLS, GRANTS, SYSFN, seed_sql()):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
