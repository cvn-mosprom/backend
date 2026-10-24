# syntax=docker/dockerfile:1
#
# Caddy + the Mini App web build (SvelteKit adapter-static) on ONE origin. BUILD CONTEXT IS THE REPO ROOT
# (it needs vendor/frontend/web; web/Dockerfile.dockerignore keeps the context to exactly that):
#
#   docker compose build caddy          (docker-compose.yml: caddy -> context ".", dockerfile web/Dockerfile)
#
# VITE_API_URL=/ : the client picks HttpGameApi when VITE_API_URL is non-empty (lib/api/index.ts) and requests
# `${base}/api/...` with base = "" after trimming the trailing slash, i.e. relative URLs on the page's own origin.
# So the same image works for any DOMAIN, and prod needs no CORS. Override only for a split-origin build:
#   docker compose build --build-arg VITE_API_URL=https://api.example.com caddy
#
# Manual build without Docker (Node 22):  cd vendor/frontend/web && npx pnpm@10 install --frozen-lockfile &&
#   VITE_API_URL=/ npx pnpm@10 build        -> vendor/frontend/web/build (serve it with the Caddyfile's root).

FROM node:22-alpine AS web-build
ARG PNPM_VERSION=10
ARG VITE_API_URL=/
ENV CI=true \
    VITE_API_URL=${VITE_API_URL}
RUN npm install -g pnpm@${PNPM_VERSION} --no-audit --no-fund
WORKDIR /app
# lockfile first: the dependency layer is reused until package.json / pnpm-lock.yaml change
COPY vendor/frontend/web/package.json vendor/frontend/web/pnpm-lock.yaml vendor/frontend/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY vendor/frontend/web/ ./
RUN pnpm run build
# S15: the site media are served from SeaweedFS (compose: seaweedfs + media-sync; Caddyfile: /landing /labels /audio /icons ->
# /media/...). Drop them from the output so the image carries only the app and SeaweedFS is the single source. The prerendered
# page build/landing.html is a FILE next to the build/landing/ media dir and stays. Fonts stay (tiny, CSS-critical).
RUN rm -rf build/landing build/labels build/audio build/icons  && test -f build/landing.html && test -f build/index.html && test -d build/fonts

FROM caddy:2-alpine
COPY --from=web-build /app/build /srv
