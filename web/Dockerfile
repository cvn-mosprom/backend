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

FROM docker.io/library/node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS web-build
ARG PNPM_VERSION=11.25.0
ARG FRONTEND_REVISION
ARG ROUTING_ZONE
ENV VITE_FRONTEND_REVISION=${FRONTEND_REVISION} \
    VITE_ROUTING_ZONE=${ROUTING_ZONE} \
    VITE_REGIONAL_ROUTING=1
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

FROM docker.io/library/caddy:2-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
COPY --from=web-build /app/build /srv
