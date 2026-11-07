# DEPENDENCY_GRAPH — зависимости модулей, фаз и задач

**Статус:** Proposed.

## 1. Зависимости модулей (compile-time)

```mermaid
flowchart BT
  platform[platform]
  contracts[contracts]
  security[security] --> platform
  identity[identity] --> platform & contracts
  projects[projects] --> identity & platform & contracts
  cli_adapters[cli_adapters] --> security & platform & contracts
  code_intelligence[code_intelligence] --> projects & security & platform & contracts
  agent_runtime[agent_runtime] --> contracts & platform
  collaboration[collaboration] --> identity & platform & contracts
  verification[verification] --> security & platform & contracts & projects
  standards[standards] --> verification & projects & platform
  orchestration[orchestration] --> projects & identity & cli_adapters & agent_runtime & code_intelligence & collaboration & verification & platform & contracts
  integrations_github[integrations_github] --> orchestration & projects & platform & security
  analytics[analytics] --> platform
  api[apps/api] --> orchestration & identity & projects & collaboration & verification & standards & code_intelligence & integrations_github & analytics & cli_adapters
  worker[apps/worker] --> orchestration & cli_adapters & verification & code_intelligence & collaboration & integrations_github
```

Обратная связь (`verification` → `orchestration`, `cli_adapters` → `orchestration`) — **только событиями**, не импортами (MODULE_BOUNDARIES §2).

## 2. Зависимости фаз

```mermaid
flowchart LR
  P0 --> P1
  P1 --> P2
  P1 --> P3
  P2 --> P3
  P3 --> P4
  P2 --> P4
  P1 --> P5
  P3 --> P5
  P3 --> P6
  P5 --> P6
  P3 --> P7
  P4 --> P7
  P5 --> P7
  P6 --> P7
  P3 --> P8
  P7 --> P8
  P4 & P5 & P6 & P7 & P8 --> P9
```

**Критический путь:** P0-10 → P1-01 → P1-07 → P1-08 → P3-01 → P3-02 → P3-03/P3-04 → P3-12 → P3-13 → P4-04 → P4-07 → P4-08 → P6-08 → P6-09 → P9-01.
**Ранний риск-спайк:** P2-01 (реальные CLI) стартует сразу после P0-10 параллельно P1, т.к. его результаты могут изменить ADR-0010 и контракт CLI.

## 3. Ключевые задачные зависимости

```mermaid
flowchart TD
  P0_10[P0-10 утверждение] --> P1_01[P1-01 скелет]
  P0_10 --> P2_01[P2-01 spike CLI]
  P1_01 --> P1_03[P1-03 contracts] & P1_04[P1-04 CI] & P1_06[P1-06 platform]
  P1_01 --> P1_02[P1-02 compose] --> P1_07[P1-07 alembic] --> P1_08[P1-08 IAM миграции]
  P1_08 --> P1_09[P1-09 auth] --> P1_10[P1-10 RBAC] --> P1_11[P1-11 API skeleton] --> P1_16[P1-16 cross-tenant]
  P1_03 --> P2_03[P2-03 CLIExecutor] --> P2_04[P2-04 Fake]
  P2_01 --> P2_05[P2-05 Codex] & P2_06[P2-06 Claude] & P2_07[P2-07 runner]
  P2_03 --> P2_05 & P2_06
  P2_07 --> P2_08[P2-08 egress]
  P1_08 --> P2_02[P2-02 CLI миграции] --> P2_09[P2-09 worktrees]
  P2_04 --> P2_10[P2-10 процессы] --> P2_11[P2-11 gateway]
  P1_08 --> P3_01[P3-01 orchestration миграции] --> P3_02[P3-02 FSM] --> P3_05[P3-05 DAG] --> P3_06[P3-06 scheduler]
  P3_01 --> P3_03[P3-03 outbox] & P3_04[P3-04 leases]
  P3_04 --> P3_06
  P3_06 & P2_11 --> P3_07[P3-07 retry]
  P3_03 --> P3_09[P3-09 SSE] & P3_11[P3-11 ledger]
  P3_11 & P2_09 --> P3_12[P3-12 recovery]
  P3_02 --> P3_10[P3-10 approvals]
  P3_12 & P3_10 --> P3_13[P3-13 directives]
  P2_07 --> P3_14[P3-14 verification v0]
  P3_13 & P3_14 --> P3_17[P3-17 E2E Fake]
  P3_01 --> P4_01[P4-01 миграции агентов] --> P4_02[P4-02 профили] --> P4_03[P4-03 prompt] & P4_06[P4-06 L1-L5]
  P4_01 & P3_03 --> P4_04[P4-04 ACP брокер] --> P4_05[P4-05 доставка] --> P4_10[P4-10 loops]
  P4_06 & P2_12[P2-12 providers API] --> P4_07[P4-07 router] --> P4_08[P4-08 independent review]
  P4_08 --> P4_13[P4-13 handoff + real CLI]
```

## 4. Внешние зависимости и предпосылки

| Предпосылка | Нужна для | Владелец |
|---|---|---|
| Машина Linux с Docker, установленными и авторизованными Codex CLI и Claude Code CLI (подписки) | P2-01, P2-13, P4-13, P9-01 | Владелец продукта |
| Ответы OQ-01, OQ-03 | P2 | Владелец продукта |
| Тестовая GitHub-организация и GitHub App | P8-02…P8-09 | Владелец продукта |
| Эталонное железо для baseline (OQ-14) | P9-04 | Владелец продукта |
