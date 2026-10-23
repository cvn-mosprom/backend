# syntax=docker/dockerfile:1
#
# Sim service image. BUILD CONTEXT IS THE REPO ROOT (it needs vendor/frontend/web/src/lib next to sim/):
#
#   docker build -f sim/Dockerfile \
#     --build-arg SIM_COMMIT=$(git -C vendor/frontend rev-parse --short HEAD) -t mosprom-sim .
#
# compose:  sim: { build: { context: ., dockerfile: sim/Dockerfile, args: { SIM_COMMIT: "<vendor/frontend sha>" } } }
#
# The vendored lib is bundled into a single dist/server.mjs with esbuild, so the runtime stage has no node_modules and
# no vendor sources: only node + the bundle.

FROM node:22-alpine AS build
WORKDIR /repo/sim
COPY sim/package.json sim/package-lock.json ./
RUN npm ci --no-audit --no-fund
# $lib -> ../vendor/frontend/web/src/lib (sim/tsconfig.json "paths")
COPY vendor/frontend/web/src/lib /repo/vendor/frontend/web/src/lib
COPY sim/ ./
RUN npm run build

FROM node:22-alpine
ARG SIM_COMMIT=unknown
ENV NODE_ENV=production \
    PORT=8080 \
    HOST=0.0.0.0 \
    SIM_WORKERS=2 \
    SIM_COMMIT=${SIM_COMMIT}
WORKDIR /app
COPY --from=build --chown=node:node /repo/sim/dist/server.mjs /repo/sim/dist/server.mjs.map ./
USER node
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/health >/dev/null || exit 1
CMD ["node", "--enable-source-maps", "server.mjs"]
