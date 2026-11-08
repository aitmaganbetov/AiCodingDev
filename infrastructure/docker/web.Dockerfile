# syntax=docker/dockerfile:1.7
# SPA (ADR-0014) + reverse proxy того же origin для /api (cookie SameSite=Strict, без CORS).
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS build
RUN corepack enable
WORKDIR /src
COPY packages/contracts ./packages/contracts
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./apps/web/
WORKDIR /src/apps/web
# Необязательный CA корпоративного прокси: --secret id=ca_bundle,src=<file>; в образ не попадает.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    pnpm install --frozen-lockfile
COPY apps/web ./
RUN pnpm check:api && pnpm build

FROM nginx:1.27-alpine
COPY infrastructure/docker/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/apps/web/dist /usr/share/nginx/html
EXPOSE 8080
