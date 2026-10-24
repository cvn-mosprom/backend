# Закреплённая сборка Caddy

Используется официальный `github.com/caddyserver/caddy/v2/cmd/caddy` версии **2.11.7**, без изменения исходников Caddy и без дополнительных модулей. `web/Dockerfile` собирает его Go **1.27.3** с `golang.org/x/net` **0.60.0**. `go.mod` и `go.sum` фиксируют полный граф зависимостей; сборка выполняет `go mod verify` и `go build -mod=readonly`. Не запускайте `go mod tidy` для этого lock-модуля без локальных Go-исходников: main-пакет находится в upstream-модуле.

Готовый официальный Caddy 2.11.7 был собран Go 1.26.8 и содержал x/net 0.59.0. Обновления только Docker-тега не устраняли их CVE. Итоговый минимальный Alpine 3.24 runtime содержит системный CA bundle; curl, libbrotli, Go compiler и Go module cache в него не копируются. Стандартные модули Caddy и встроенные часовые пояса сохраняются.

Для обновления версий используйте закреплённый Go builder из Dockerfile и выполните в этом каталоге:

```sh
go get github.com/caddyserver/caddy/v2/cmd/caddy@v2.11.7 golang.org/x/net@v0.60.0
go mod verify
```

Затем проверьте diff обоих lock-файлов, повторите build, Trivy, production browser smoke и E2E. Обновление Go требует нового digest builder-образа. CI выпускает linux/amd64; сборка Caddy использует BUILDPLATFORM и TARGETARCH для cross-compilation.

Источники: [официальный main Caddy](https://github.com/caddyserver/caddy/blob/v2.11.7/cmd/caddy/main.go), [Go security releases](https://go.dev/doc/devel/release), [официальные Docker-образы Python](https://hub.docker.com/_/python).
