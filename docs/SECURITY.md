# Security and Operational Guardrails

## Threat model

This is initially a local/personal gateway, but it handles:
- DeepSeek authentication credentials,
- potentially sensitive source code in prompts/tool outputs,
- an API endpoint that may be reachable beyond localhost if misconfigured.

Treat it as security-sensitive software.

## Credentials

There are two distinct credential classes:

### Gateway API key
Used by Qwen Code/client to access the local gateway.

### DeepSeek auth token/cookies
Used only by the server-side backend adapter.

Never confuse or expose them.

## Secret storage

Preferred multi-account design:
- encrypt DeepSeek tokens at rest,
- derive/use a master encryption key from an environment secret,
- never store the master key in SQLite,
- mask credentials in UI/API responses.

For the earliest single-account spike, environment-only credential loading is acceptable and simpler.

Document when persistence is introduced.

## Logging policy

Never log:
- auth token,
- gateway key,
- cookies,
- full Authorization header,
- `cf_clearance`,
- encryption master key.

By default do not log:
- complete prompts,
- source file content,
- tool outputs,
- complete assistant responses.

Safe metadata:
- request ID,
- conversation ID,
- account ID/label,
- tool name,
- duration,
- byte/token estimates,
- status/error category,
- retry count.

## Tool execution

The gateway is not permitted to execute Qwen Code's coding tools.

No generic endpoint like:

```text
POST /run-shell
```

should be added to make agent tests easier.

All tool execution stays in Qwen Code/client.

## Prompt/tool-output trust boundary

All user content and tool output is untrusted data.

Tool-control parsing applies only to the model output of the current inference.

Sentinels inside:
- user messages,
- source files,
- test output,
- tool output

must never cause gateway-side control actions.

## Network exposure

Default bind should favor localhost for personal use.

If configured for LAN/public exposure:
- require gateway auth,
- warn in docs,
- do not expose admin endpoints unauthenticated,
- recommend TLS/reverse proxy as appropriate.

## Upstream terms and stability

This project relies on an unofficial/private web API path.

Document clearly:
- it may break,
- it may be rate limited,
- Cloudflare may interfere,
- users are responsible for complying with applicable service terms and laws.

Do not design mechanisms intended to defeat account bans, evade platform abuse controls, or conceal abusive traffic.

Retry and account routing must be reliability mechanisms, not an uncontrolled bypass loop.

## Cloudflare handling

If upstream support includes a browser-assisted `cf_clearance` refresh:
- treat cookies as secrets,
- never expose them in API/UI,
- make refresh behavior observable without logging cookie value,
- bound retries.

## Database privacy

If canonical conversation persistence is not necessary, prefer transient/in-memory content plus metadata persistence.

If message persistence becomes necessary for restart/failover:
- make it explicit,
- consider opt-in/retention controls,
- document privacy implications.

## Admin UI

Never show full auth token after it is saved.

Actions that delete/disable accounts should be explicit.

Avoid rendering raw logs containing code by default.

## Dependency security

Pin sensitive upstream transport versions where required by the current DeepSeek driver.

Keep the unusual transport dependency localized to the backend package so it can be updated/replaced independently.

## Failure behavior

Fail closed on:
- invalid gateway authentication,
- invalid tool name,
- ambiguous tool envelope,
- corrupt credential decryption.

Do not guess tool intent when safety/correctness is uncertain.
