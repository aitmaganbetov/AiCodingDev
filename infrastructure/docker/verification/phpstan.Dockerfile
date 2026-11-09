# syntax=docker/dockerfile:1.7
# Образ gate PHPStan (OQ-43 (a), ключ образа `php`, aicd_verification.images.TOOL_IMAGE_KEYS["phpstan"]).
# Gate вызывает `phpstan analyse --error-format=json --no-progress --no-interaction --memory-limit=1G`
# (aicd_verification.runners) в каталоге checkout, сеть none, read-only rootfs.
#
# phpstan.phar — официальный релиз PHPStan (GitHub Releases), закреплён по sha256, вычисленному при закреплении
# (2026-11-08); PGP-подпись релиза (`phpstan.phar.asc`) в окружении сборки не проверялась — нет ключа автора
# в доверенном keyring (проверка — шаг ревью при смене версии, docs/runbooks/release.md).
# Composer не ставится: автозагрузку зависимостей проекта (vendor/) готовит шаг prepare (OQ-43 (b)).
#
# Сборка:
#   docker build -f infrastructure/docker/verification/phpstan.Dockerfile \
#     -t aicd/phpstan:2.3.5 infrastructure/docker/verification
FROM php:8.3-cli-alpine3.22@sha256:2f2e706ad96f295e2bc2b36dcc88e7413b7270fcd2b1532f3cfb4b6ebeca74ca
ADD --chmod=0555 --checksum=sha256:4b3ed7850a20b092331bd1040e4f54a1d3c0559b4cf006431073997ebcb4324b \
    https://github.com/phpstan/phpstan/releases/download/2.3.5/phpstan.phar /usr/local/bin/phpstan
USER 65532:65532
ENTRYPOINT []
CMD ["phpstan", "--version"]
