"""Модуль standards (MODULE_BOUNDARIES §3, SYSTEM_ARCHITECTURE): реестр стандартов, контроли,
применимость к проекту и оценка соответствия (Standards & Compliance Engine, QUALITY_GATES §8).

Слой доменов (наравне с verification): не импортирует orchestration, verification и приложения;
результаты gates/evidence/findings читает из БД (миграции 0003/0008). Таблицы — миграция 0008
(P6-01, DATABASE_DESIGN §13). Seed реестра — собственные формулировки платформы (RISK-13).

* `catalog` — правила применимости контролей seed по стеку и режиму run;
* `applicability` — чистые функции: теги стека, черновики применимости, релевантность режиму;
* `compliance` — чистая оценка: контроль → вердикт с трассировкой standard → … → verdict;
* `store` — доступ к БД (загрузка входов, запись черновиков/утверждений и вердиктов).
"""

OWNED_TABLES: frozenset[str] = frozenset({"standards", "standard_controls", "project_controls", "compliance_results"})
