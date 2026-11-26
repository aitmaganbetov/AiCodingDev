# Образ gates Python (ключ образа `python`, aicd_verification.images): python:3.12-slim + git.
#
# Зачем отдельный образ. Базовый `python:3.12-slim` не содержит `git`, а тесты и сборка большинства
# Python-проектов его вызывают (фикстуры с `git init`, `setuptools-scm`, проверки рабочего дерева).
# Без него UNIT/COVERAGE дают FAIL по причине окружения, а не по коду проекта: вердикт честный, но
# бесполезный. Зависимости самого проверяемого проекта ставит шаг prepare (QUALITY_GATES §4.3),
# этот образ их не содержит.
#
# Что НЕ добавляется: сетевые утилиты (curl, wget, ssh) — gate-контейнер работает с `network none`,
# и лишние клиенты в нём только расширяют поверхность атаки недоверенного кода репозитория.
#
# Сборка (BuildKit не требуется, секретов и mount'ов в рецепте нет):
#   DOCKER_BUILDKIT=0 docker build -f infrastructure/docker/verification/python.Dockerfile \
#     -t aicd/verify-python:3.12-git infrastructure/docker/verification
# Подключение: AICD_VERIFICATION_IMAGES='{"python": "aicd/verify-python:3.12-git"}'
# Для профиля server образ публикуется во внутренний реестр и подключается по digest
# (`require_image_digest=True` отвергает ссылку без `@sha256:`).
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
# `git` + корневые сертификаты (git читает их при проверке подписей объектов). Версия фиксируется
# снимком apt базового образа; точные версии пакетов попадают в evidence через digest образа.
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb; \
    git --version
# Процесс gate запускается от non-root uid из ContainerSpec.user (политика контейнеров отвергает root),
# а рабочее дерево принадлежит пользователю воркера — git иначе считает его «подозрительным» и
# отказывается работать. Исключение выдано **только** точке монтирования checkout, без wildcard:
# `safe.directory = *` доверял бы и любому репозиторию, который код проекта создаст в произвольном
# месте, вместе с исполняемыми настройками его `.git/config` (core.pager, core.fsmonitor).
RUN printf '[safe]\n\tdirectory = /workspace\n' > /etc/gitconfig
