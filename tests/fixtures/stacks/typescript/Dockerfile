FROM --platform=linux/amd64 node:20-alpine AS build
RUN npm ci
FROM nginx:1.27-alpine
COPY --from=build /app/out /usr/share/nginx/html
