# ADR-0010: Runner-контейнеры для CLI-исполнений и egress proxy

- **Статус:** Accepted (утверждено владельцем 2026-11-07)
- **Дата:** 2026-11-07

## Контекст
ТЗ §3, §15: Docker с жёсткими ограничениями, запись только в назначенный worktree, egress policy, лимиты, без privileged, без production secrets; локальный fallback только по явной политике. Одновременно CLI требует доступа к собственным учётным данным подписки и к эндпоинтам провайдера.

## Решение
1. Каждое `cli_execution` — эфемерный контейнер из образа `aicd-runner-<provider>:<digest>` (официальный CLI установлен из официального источника, pinned версия, SBOM).
2. Жёсткие параметры: non-root UID, `--cap-drop=ALL`, `--security-opt no-new-privileges`, seccomp default, read-only rootfs + `tmpfs /tmp`, `--pids-limit`, `--memory`, `--cpus`, wall-clock timeout, без `--privileged`, без хостовых namespaces, без Docker socket внутри.
3. Mounts: worktree задачи (rw), Context Pack (ro), том учётных данных провайдера `aicd-cred-<provider>` (rw, т.к. CLI может обновлять токены; монтируется **только** в runner этого провайдера, никогда — в verification-контейнеры и не копируется в артефакты).
4. Сеть: отдельная Docker-сеть без прямого выхода; egress только через `egress-proxy` с allowlist доменов провайдера (+ разрешённые approval'ом); `unrestricted network` — только через approval `UNRESTRICTED_NETWORK`.
5. `worker-exec` управляет Docker через `docker-socket-proxy` с разрешёнными только create/start/stop/inspect/logs/remove; API-процесс к Docker доступа не имеет.
6. Hardening-опция: rootless Docker или gVisor (`runsc`) — рекомендуется, проверяется совместимость с CLI в Phase 2.
7. Local fallback (без Docker) — только при политике проекта + approval `LOCAL_EXECUTION_FALLBACK`; помечается в evidence и UI.

## Риски / открытые вопросы
- Встроенные sandbox-механизмы CLI (например, собственный seatbelt/landlock-режим) могут конфликтовать с контейнером → выбрать режим в Phase 2 по результатам probe.
- Хранение токенов в томе vs OS keychain (macOS) — на macOS-хостах контейнер не видит keychain → OQ-03.
