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

FROM docker.io/library/node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /repo/sim
COPY sim/package.json sim/package-lock.json ./
RUN npm ci --no-audit --no-fund
# $lib -> ../vendor/frontend/web/src/lib (sim/tsconfig.json "paths")
COPY vendor/frontend/web/src/lib /repo/vendor/frontend/web/src/lib
COPY sim/ ./
RUN npm run build

FROM docker.io/library/node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
ARG SIM_COMMIT=unknown
ENV NODE_ENV=production \
    PORT=8080 \
    HOST=0.0.0.0 \
    SIM_WORKERS=2 \
    SIM_COMMIT=${SIM_COMMIT}
# Runtime is a standalone bundle: package managers are unnecessary attack surface.
RUN rm -rf /usr/local/lib/node_modules /opt/yarn-* \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg
WORKDIR /app
COPY --from=build --chown=node:node /repo/sim/dist/server.mjs /repo/sim/dist/server.mjs.map ./
USER node
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/health >/dev/null || exit 1
CMD ["node", "--enable-source-maps", "server.mjs"]
