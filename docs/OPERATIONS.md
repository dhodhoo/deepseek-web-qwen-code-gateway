# Operations Guide

M13 provides a Docker-first operator path for the local DeepSeek Qwen Gateway.
The image contains the gateway code and vendored upstream client, but never a
`.env` file, DeepSeek token, gateway key, or cookie file.

## Prerequisites

- Docker Engine or Docker Desktop with Compose v2 (`docker compose version`).
- A copied `.env` file with the backend credentials/configuration.

## Start

From the repository root:

```text
copy .env.example .env
```

Edit `.env` and set at least:

```dotenv
DEEPSEEK_AUTH_TOKEN=replace-with-your-token
DEEPSEEK_GATEWAY_API_KEY=replace-with-a-local-client-key
```

Then build and start:

```text
docker compose up -d --build
```

The M13 exit path is `docker compose up -d` after the image has been built and
credentials have been supplied. The service publishes `127.0.0.1:8000` by
default. Change `GATEWAY_PUBLISH_HOST` or `GATEWAY_PUBLISH_PORT` in `.env` if
the host binding must differ; the container always listens on port 8000.

Verify the service:

```text
curl http://127.0.0.1:8000/health
```

Open the local admin UI at `http://127.0.0.1:8000/admin`. Admin routes are
unauthenticated only when the application bind host is loopback. Docker sets
its internal bind to `0.0.0.0`, so the Compose admin surface requires the
configured `DEEPSEEK_GATEWAY_API_KEY` bearer key. Send it as
`Authorization: Bearer <key>` when calling the admin API or place the same key
in the browser client used for remote administration.

Do not publish the admin surface to the LAN or internet without the gateway
key and an appropriate TLS/reverse-proxy boundary.

## Fake-backend smoke test

The image can be checked without a DeepSeek credential:

```dotenv
GATEWAY_BACKEND=fake
GATEWAY_ALLOW_NO_AUTH=1
```

Use this only for a local smoke test. Restore `GATEWAY_BACKEND=deepseek_web`,
`GATEWAY_ALLOW_NO_AUTH=0`, and the real credentials before normal operation.

## Qwen Code setup

Point Qwen Code's OpenAI-compatible provider at the published API root, not at
the chat-completions route:

```text
baseUrl: http://127.0.0.1:8000/v1
apiKey: replace-with-the-value-of-DEEPSEEK_GATEWAY_API_KEY
model: deepseek-web
```

The exact provider configuration syntax depends on the Qwen Code version; see
`docs/QWEN_CODE_INTEGRATION.md` for the current wiring notes. Qwen Code remains
the tool executor. The gateway never executes repository tools.

## Lifecycle and logs

```text
docker compose ps
docker compose logs -f gateway
docker compose restart gateway
docker compose down
```

Changing `.env` requires recreation so Compose injects the new environment:

```text
docker compose up -d --force-recreate
```

Use `docker compose down` to stop the service without deleting the named
volume. Do not use `docker compose down -v` unless deleting diagnostics and
future operator state is intentional.

## Persistent volume and diagnostics

The named volume `gateway-data` is mounted at
`/var/lib/deepseek-qwen-gateway`. M13 uses its `diagnostics` subdirectory for
opt-in sanitized request diagnostics when `GATEWAY_DIAGNOSTICS_DIR` is set by
Compose. Conversation state, account health/cooldowns, and metrics remain
in-memory by design and are lost on restart/replacement. The volume is not a
conversation database.

Diagnostics contain request bodies by design. Treat the volume as sensitive,
restrict host access, and keep `GATEWAY_DIAGNOSTICS_DIR` empty when diagnostics
are not needed. Never copy `.env`, token values, or cookie files into the image.

## Troubleshooting

### Container is unhealthy

Inspect logs and the health response:

```text
docker compose ps
docker compose logs --tail=200 gateway
curl -i http://127.0.0.1:8000/health
```

`/health` can be `200` while the configured DeepSeek account is unusable; check
`/admin/accounts` and the account/backend error in the logs. A missing or
invalid DeepSeek token fails configuration at startup. For a credential-free
container check, use the fake-backend smoke configuration above.

### Port already in use

Set a different host port in `.env`, for example:

```dotenv
GATEWAY_PUBLISH_PORT=18000
```

Then recreate the service and use `http://127.0.0.1:18000`.

### Qwen Code receives authentication errors

`DEEPSEEK_GATEWAY_API_KEY` authenticates Qwen Code to the gateway; it is not the
DeepSeek token. Ensure Qwen Code's `apiKey` matches it exactly and that the
base URL ends in `/v1`. `/admin/*` is local-admin surface and does not use this
key, consistent with the M12 contract.

### DeepSeek upstream or Cloudflare errors

Check the configured token/cookies and the normalized error category. Do not
paste tokens or cookie values into logs, tickets, or chat. Retry behavior and
account cooldown/failover semantics are documented in `docs/API_CONTRACT.md`.

### Changes do not appear

Rebuild after source changes:

```text
docker compose build --no-cache gateway
docker compose up -d --force-recreate
```

The running container must be recreated after changing image code or `.env`.
