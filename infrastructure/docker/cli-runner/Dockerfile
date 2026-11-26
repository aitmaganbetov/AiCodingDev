# Runner-контейнер исполнения задач официальными CLI (ADR-0010, CLI_ADAPTER_CONTRACT §8).
#
# Образ содержит только сами CLI и инструменты, которыми агент пользуется в worktree. Учётных данных
# в образе нет и быть не может (ТЗ §1): credential store монтируется снаружи в HOME и принадлежит
# пользователю хоста, поэтому UID/GID задаются при сборке и должны совпадать с владельцем каталога.
#
#   docker build -t aicd/cli-runner:latest \
#     --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#     infrastructure/docker/cli-runner
#
# Версии CLI закреплены: profiles сверены именно с ними (`real/adapter.py`), обновление версии
# требует повторного probe и прогона набора `real-cli-smoke@1` (контракт §13.3).
FROM node:24-bookworm-slim

ARG CLAUDE_VERSION=2.1.319
ARG CODEX_VERSION=0.167.0
ARG UID=10001
ARG GID=10001

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates ripgrep python3 \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && test -x /usr/local/bin/claude \
    && test -x /usr/local/bin/codex

# Пользователь агента. node-образ уже занимает uid 1000 именем node — переиспользуем его, если
# запрошенный UID совпадает, иначе создаём отдельного пользователя.
RUN if getent passwd "${UID}" >/dev/null; then \
        usermod -l agent -d /home/agent -m "$(getent passwd "${UID}" | cut -d: -f1)" \
        && groupmod -n agent "$(getent group "${GID}" | cut -d: -f1)" || true; \
    else \
        groupadd -g "${GID}" agent && useradd -u "${UID}" -g "${GID}" -m -d /home/agent -s /usr/sbin/nologin agent; \
    fi \
    && mkdir -p /home/agent /workspace \
    && chown -R "${UID}:${GID}" /home/agent /workspace

# Сеть, монтирования, лимиты и пользователь процесса задаются платформой в ContainerSpec,
# а не образом: образ остаётся пригодным и для ro-worktree ролей без права записи.
USER ${UID}:${GID}
ENV HOME=/home/agent \
    LANG=C.UTF-8 \
    npm_config_update_notifier=false \
    DISABLE_AUTOUPDATER=1
WORKDIR /workspace
