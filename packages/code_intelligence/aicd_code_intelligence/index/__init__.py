"""Индекс кода по commit (P5-02…P5-05; ADR-0005, DATABASE_DESIGN §10, DATA_FLOW §9).

Подмодули:

* `languages` / `policy` — определение языка, правила исключения (binary/oversized/vendored/generated), тесты;
* `gitsource` — дерево файлов commit (`git ls-tree`) и чтение blob (`git cat-file --batch`) без checkout;
* `symbols` — символы (tree-sitter для Python/JS/TS/PHP; лёгкая структура SQL/YAML/JSON/Markdown);
* `graph` — импорты, их разрешение в файлы, test links;
* `chunks` — фрагменты для FTS (`text_ref = git:<blob>#Lx-Ly`, текст в БД не копируется);
* `builder` — чистое (без БД) построение снимка с инкрементальным переиспользованием по blob/sha256;
* `search` — ripgrep с безопасным argv по экспорту снимка + SQL FTS/pg_trgm;
* `store` / `jobs` — запись в PostgreSQL и жизненный цикл `indexing_jobs` (`run_indexing_job`).

Всё, кроме `store`/`jobs` и SQL-части `search`, не требует БД и покрыто unit-тестами.
"""

from __future__ import annotations

INDEX_VERSION = 1
"""Версия формата/алгоритма индексатора (`code_index_snapshots.index_version`).

Увеличивается при любом изменении, меняющем символы/граф/фрагменты: снимки другой версии
не используются как база инкрементального построения."""
