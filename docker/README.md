# ReuleauxCoder Docker Host

This directory contains a minimal containerized deployment for running ReuleauxCoder as a dedicated remote relay host.

## Files

- `Dockerfile`: builds the host image
- `docker-compose.yml`: starts the host container on port `8765`
- `config.host.yaml`: template rendered from environment variables at startup
- `.env.example`: example environment file
- `entrypoint.sh`: renders config and launches `rcoder --server`

## Usage

1. Create the environment file:

```bash
cd docker
cp .env.example .env
```

2. Edit `.env` and fill in:

- `RCODER_MODEL`
- `RCODER_BASE_URL`
- `RCODER_API_KEY`
- `RCODER_BOOTSTRAP_ACCESS_SECRET`

3. Start the host:

```bash
docker compose up -d --build
```

4. Check logs:

```bash
docker compose logs -f
```

The relay will listen on `0.0.0.0:8765` inside the container and be published as host port `8765`.
