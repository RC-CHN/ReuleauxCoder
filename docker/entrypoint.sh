#!/bin/sh
set -eu

: "${RCODER_MODEL:?RCODER_MODEL is required}"
: "${RCODER_BASE_URL:?RCODER_BASE_URL is required}"
: "${RCODER_API_KEY:?RCODER_API_KEY is required}"
: "${RCODER_BOOTSTRAP_ACCESS_SECRET:?RCODER_BOOTSTRAP_ACCESS_SECRET is required}"

envsubst < /app/docker/config.host.yaml.template > /app/docker/config.host.yaml
exec rcoder --config /app/docker/config.host.yaml --server
