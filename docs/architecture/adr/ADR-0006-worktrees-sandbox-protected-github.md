# ADR-0006: Изолированные Git worktrees, sandbox и защищённые GitHub-действия

- **Статус:** Accepted (ТЗ §2 п.6, §15, §22)
- **Дата:** 2026-11-07

## Решение
1. Для каждой пишущей задачи создаётся отдельный `git worktree` на ветке `aicd/<run_short>/<task_short>-<worktree_short>` (уникальна на попытку) от `base_commit_sha` run'а; запись CLI — только внутрь этого worktree (bind mount в runner, ADR-0010).
2. Основной клон репозитория (`local_root_ref`) — только для чтения агентами; изменения интегрируются оркестратором через merge/rebase в интеграционную ветку run'а после PASS gates.
3. Удаление worktree — после `COLLECTED` и сохранения diff как artifact; осиротевшие worktree обнаруживает recovery reconciler (`worktrees.status = ORPHANED`).
4. GitHub: push веток и создание PR — через GitHub App с минимальными правами; **merge, изменение branch protection, деплой — только с approval** и с уважением branch protection/required reviews; безусловный auto-merge запрещён.
5. Все внешние Git/GitHub действия проходят через ledger `external_actions` (ADR-0012).

## Связанные требования
FR-SBX-*, FR-GH-*, AC-04, AC-09, AC-13.
