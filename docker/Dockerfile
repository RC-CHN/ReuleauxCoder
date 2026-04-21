FROM golang:1.23 AS peer-builder

WORKDIR /src/reuleauxcoder-agent
COPY reuleauxcoder-agent/go.mod reuleauxcoder-agent/go.sum ./
RUN go mod download
COPY reuleauxcoder-agent /src/reuleauxcoder-agent
RUN mkdir -p /out/linux/amd64 /out/linux/arm64 /out/darwin/amd64 /out/darwin/arm64 /out/windows/amd64 /out/windows/arm64 \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/linux/amd64/rcoder-peer ./cmd/reuleauxcoder-agent \
    && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o /out/linux/arm64/rcoder-peer ./cmd/reuleauxcoder-agent \
    && CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -o /out/darwin/amd64/rcoder-peer ./cmd/reuleauxcoder-agent \
    && CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -o /out/darwin/arm64/rcoder-peer ./cmd/reuleauxcoder-agent \
    && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o /out/windows/amd64/rcoder-peer.exe ./cmd/reuleauxcoder-agent \
    && CGO_ENABLED=0 GOOS=windows GOARCH=arm64 go build -o /out/windows/arm64/rcoder-peer.exe ./cmd/reuleauxcoder-agent

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md README_CN.md /app/
COPY reuleauxcoder /app/reuleauxcoder
COPY .rcoder/skills /app/.rcoder/skills
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY docker/config.host.yaml /app/docker/config.host.yaml.template
COPY --from=peer-builder /out /app/artifacts/remote

RUN pip install --no-cache-dir . \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/.rcoder

CMD ["/app/docker/entrypoint.sh"]
