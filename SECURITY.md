# Security Policy

## Scope

This project is a local-first gateway for Qwen Code and an unofficial DeepSeek Web backend. It is intended for local/private deployment and controlled LAN use. Direct public-internet exposure is out of scope.

## Reporting a vulnerability

Do not open a public issue for credentials, cookies, authentication bypasses, or other sensitive security findings. Contact the repository owner privately through the GitHub Security Advisories workflow if it is enabled, or use a private maintainer contact before disclosing details publicly.

Include:

- affected version or commit;
- reproduction steps that do not include real credentials;
- impact and prerequisites;
- a proposed mitigation, if known.

Never include DeepSeek tokens, gateway keys, cookies, raw prompts, source code, or unredacted diagnostics in a report.

## Service and Terms of Service warning

This project is not affiliated with or endorsed by DeepSeek and does not use the
official DeepSeek API. It adapts access to `chat.deepseek.com` into an
unofficial API. Before using the project, review and comply with DeepSeek's
current Terms of Service, acceptable-use rules, and applicable laws. You are
responsible for your account, credentials, requests, and any consequences of
use. Service behavior, availability, and compatibility may change without
notice.

## Security boundaries

- Qwen Code executes filesystem, shell, and other coding tools; the gateway does not.
- Gateway API authentication is required unless local development explicitly enables the no-auth option.
- Non-loopback admin routes require the gateway bearer key.
- Diagnostics are opt-in and may contain request bodies; keep them private.
- DeepSeek credentials and cookies must remain in local environment/configuration and must never be committed.

For operational controls and threat-model details, see [`docs/SECURITY.md`](docs/SECURITY.md).
