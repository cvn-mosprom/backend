#
# Caddy + the Mini App web build (SvelteKit adapter-static) on ONE origin. BUILD CONTEXT IS THE REPO ROOT
# (it needs vendor/frontend/web; web/Dockerfile.dockerignore keeps the context to exactly that):
#
#   docker compose build caddy          (docker-compose.yml: caddy -> context ".", dockerfile web/Dockerfile)
#
# VITE_API_URL=/ : the client picks HttpGameApi when VITE_API_URL is non-empty (lib/api/index.ts) and requests
# `${base}/api/...` with base = "" after trimming the trailing slash, i.e. relative URLs on the page's own origin.
# So the same image works for any DOMAIN, with CORS configured for the trusted regional API origins. Override for a split-origin build:
#   docker compose build --build-arg VITE_API_URL=https://api.example.com caddy
#
# Manual build without Docker (Node 22):  cd vendor/frontend/web && npx pnpm@11.25.0 install --frozen-lockfile &&
#   VITE_API_URL=/ npx pnpm@11.25.0 build        -> vendor/frontend/web/build (serve it with the Caddyfile's root).

FROM --platform=$BUILDPLATFORM docker.io/library/node:25-alpine@sha256:bdf2cca6fe3dabd014ea60163eca3f0f7015fbd5c7ee1b0e9ccb4ced6eb02ef4 AS web-build
ARG PNPM_VERSION=11.25.0
ARG FRONTEND_REVISION
ARG ROUTING_ZONE
ARG REGIONAL_ROUTING=1
ARG VITE_TG_APP_URL=
ARG VITE_HR_DEMO=0
ARG VITE_TELEGRAM_ONLY=0
ARG VITE_BOT_USERNAME=
ARG EXTERNAL_MEDIA=0
ENV VITE_FRONTEND_REVISION=${FRONTEND_REVISION} \
    VITE_ROUTING_ZONE=${ROUTING_ZONE} \
    VITE_REGIONAL_ROUTING=${REGIONAL_ROUTING} \
    VITE_TG_APP_URL=${VITE_TG_APP_URL} \
    VITE_HR_DEMO=${VITE_HR_DEMO} \
    VITE_TELEGRAM_ONLY=${VITE_TELEGRAM_ONLY} \
    VITE_BOT_USERNAME=${VITE_BOT_USERNAME}
ARG VITE_API_URL=/
ENV CI=true \
    VITE_API_URL=${VITE_API_URL}
RUN npm install -g pnpm@${PNPM_VERSION} --no-audit --no-fund
WORKDIR /app
# lockfile first: the dependency layer is reused until package.json / pnpm-lock.yaml change
COPY vendor/frontend/web/package.json vendor/frontend/web/pnpm-lock.yaml vendor/frontend/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY vendor/frontend/web/ ./
COPY web/generate-csp.mjs /tmp/generate-csp.mjs
RUN pnpm run build && node /tmp/generate-csp.mjs /app/build /tmp/content-security-policy.caddy
# S15: the site media are served from SeaweedFS (compose: seaweedfs + media-sync; Caddyfile: /landing /labels /audio /icons ->
# /media/...). Drop them from the output so the image carries only the app and SeaweedFS is the single source. The prerendered
# root landing build/index.html and SPA fallback build/200.html stay. Fonts stay (tiny, CSS-critical).
RUN if [ "$EXTERNAL_MEDIA" = "1" ]; then rm -rf build/landing build/labels build/audio build/icons; fi \
    && test -f build/200.html && test -f build/index.html && test -d build/fonts

# Rebuild the upstream Caddy command with patched Go and the reviewed module lock.
FROM --platform=$BUILDPLATFORM docker.io/library/golang:1.27.3-alpine3.24@sha256:b8c9b82affbbb82a020bbcf998dad2ba9516944ac7e883524642a481a9505e9c AS caddy-build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
COPY web/caddy/go.mod web/caddy/go.sum ./
RUN go mod download && go mod verify
ARG TARGETOS
ARG TARGETARCH
RUN GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -mod=readonly -trimpath -ldflags="-s -w" -o /out/caddy github.com/caddyserver/caddy/v2/cmd/caddy

# Minimal Alpine runtime excludes curl/libbrotli and all Go build tooling.
FROM docker.io/library/alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
COPY --from=caddy-build /out/caddy /usr/bin/caddy
COPY --from=web-build /app/build /srv
COPY --from=web-build /tmp/content-security-policy.caddy /etc/caddy/content-security-policy.caddy
RUN addgroup -g 10004 web && adduser -D -H -u 10004 -G web web \
    && mkdir -p /data/caddy /config/caddy \
    && chown -R web:web /data /config \
    && test -s /etc/ssl/certs/ca-certificates.crt
USER 10004:10004
WORKDIR /srv
EXPOSE 80 443 443/udp
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
