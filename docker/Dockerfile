FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md README_CN.md /app/
COPY reuleauxcoder /app/reuleauxcoder
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY docker/config.host.yaml /app/docker/config.host.yaml.template
COPY artifacts/remote /app/artifacts/remote

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --index-url https://pypi.org/simple . \
    && SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && mkdir -p "${SITE_PACKAGES}/artifacts" \
    && ln -sfn /app/artifacts/remote "${SITE_PACKAGES}/artifacts/remote" \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/.rcoder

CMD ["/app/docker/entrypoint.sh"]
