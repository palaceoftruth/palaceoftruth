# Build stage
FROM node:26-alpine@sha256:2d984a15c9b54fd0aeb608b8e0d0d83529eb34d2966db27a1fb4f1edc3d298a3 AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

FROM node:26-alpine AS development
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# Serve stage.
# nginx-unprivileged runs as a non-root user, writes its pid and temp paths
# under /tmp, and listens on 8080, so the pod needs neither uid 0 nor
# NET_BIND_SERVICE. Keep the mutable tag visible for maintainability but bind
# the build to the reviewed digest.
FROM nginxinc/nginx-unprivileged:alpine@sha256:a6c3ec0c0d249d68b0682df854d4a9e222b90fb607dc3fcf2f1d2fcbc85d347e
# Apply Alpine security updates the pinned base image predates, mirroring the
# backend runtime stage. The digest stays pinned for reproducibility, so
# without this the frontend keeps shipping whatever was current when that
# digest was cut. Trivy runs with ignore-unfixed, so it fails the build on
# exactly the packages this upgrade can fix, which is why upgrading beats a
# suppression. Upstream had not rebuilt for CVE-2026-14456 (openssl
# 3.5.7-r0 -> 3.5.8-r0) when this was added.
USER root
RUN apk --no-cache upgrade
USER nginx
COPY --from=builder /app/dist /usr/share/nginx/html
# Land the config directly in conf.d rather than in templates/. The entrypoint
# renders templates/ into conf.d at startup, which needs a writable conf.d; the
# config has no envsubst variables, so the template step buys nothing and only
# adds a write the read-only root filesystem has to work around.
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
