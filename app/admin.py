"""Admin dashboard views (M12, ADR-039).

The M12 admin UI is a pure CLIENT of the gateway's admin JSON surface:
this module holds the structurally secret-free view builders and the
single self-contained HTML page served at ``GET /admin`` (inline CSS +
vanilla JS, no external assets, no build step — local-first).

Masking is structural, not best-effort (ADR-039 point 2/3):

* :func:`build_summary` aggregates fleet state + counters — ids,
  derived states, counts, durations only;
* :func:`build_sessions_view` serializes conversation METADATA (ids,
  account binding, counters, timestamps) — never message content, tool
  arguments or anything else from the canonical histories;
* :func:`build_settings_view` renders every secret as PRESENCE ONLY
  (``gateway_auth`` mode; account count) — :class:`pydantic.SecretStr`
  values are never read here.

The route handlers themselves live next to the rest of the route table
in :func:`app.server.create_app`; they are thin wrappers around these
builders plus the existing :class:`~app.accounts.AccountRouter`
lifecycle methods (``set_enabled`` / ``reset`` — the "M12 surface"
named since M10).
"""

from __future__ import annotations

from typing import Any

from .accounts import (
    ACCOUNT_COOLDOWN,
    ACCOUNT_DISABLED,
    ACCOUNT_HEALTHY,
    ACCOUNT_INVALID,
    AccountRouter,
)
from .config import GatewaySettings
from .conversation import ConversationStore
from .metrics import MetricsCollector

__all__ = [
    "ADMIN_PAGE_HTML",
    "build_sessions_view",
    "build_settings_view",
    "build_summary",
]


def build_summary(
    router: AccountRouter,
    store: ConversationStore,
    metrics: MetricsCollector,
    health_payload: dict[str, Any],
) -> dict[str, Any]:
    """Dashboard aggregate (GET /admin/summary payload, ADR-039).

    ``health_payload`` is the exact ``/health`` response (fleet-aware
    since M10) so the dashboard card and the probe endpoint can never
    disagree. Everything else is derived live from the router, the
    store and the metrics snapshot — counters and derived states only,
    structurally secret-free.
    """
    rows = router.summary(store)
    by_state = {
        state: 0
        for state in (
            ACCOUNT_HEALTHY,
            ACCOUNT_COOLDOWN,
            ACCOUNT_INVALID,
            ACCOUNT_DISABLED,
        )
    }
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
    conversations = store.conversations()
    snapshot = metrics.snapshot()
    return {
        "health": health_payload,
        "backend_type": router.backend_type,
        "accounts": {"total": len(rows), "by_state": by_state},
        "conversations": len(conversations),
        "active_sessions": sum(
            1
            for conversation in conversations
            if conversation.backend_session_id is not None
        ),
        "uptime_seconds": snapshot["uptime_seconds"],
        "metrics": {
            "requests": snapshot["requests"],
            "backend_attempts": snapshot["backend_attempts"],
            "backend_failures": dict(snapshot["backend_failures"]),
            "transport_retries": snapshot["transport_retries"],
            "session_failovers": snapshot["session_failovers"],
            "tool_turns": snapshot["tool_turns"],
        },
    }


def build_sessions_view(store: ConversationStore) -> list[dict[str, Any]]:
    """Sanitized session list (GET /admin/sessions payload, ADR-039).

    Newest-updated first. Metadata ONLY: ids, account/session binding,
    status, message/tool-call counters, timestamps. The canonical
    message content itself (prompts, tool arguments, tool results) is
    deliberately NOT serialized by any admin surface — raw conversation
    content stays off every observability path (ADR-020 spirit).
    """
    rows: list[dict[str, Any]] = []
    for conversation in reversed(store.conversations()):
        messages = conversation.messages
        rows.append(
            {
                "conversation_id": conversation.conversation_id,
                "backend_account_id": conversation.backend_account_id,
                "backend_session_id": conversation.backend_session_id,
                "linked": conversation.backend_session_id is not None,
                "status": conversation.status,
                "message_count": len(messages),
                "tool_call_count": sum(
                    len(message.tool_calls or ()) for message in messages
                ),
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
            }
        )
    return rows


def build_settings_view(settings: GatewaySettings) -> dict[str, Any]:
    """Read-only masked settings (GET /admin/settings payload, ADR-039).

    Secrets render as PRESENCE ONLY: the gateway key becomes the auth
    mode (``configured`` / ``open`` / ``unset``) and the account tokens
    become a mode + count. Runtime mutation is out of scope — settings
    are env-derived and frozen at startup (a restart applies new
    values; the M13 Docker-era config story).
    """
    if settings.gateway_api_key is not None:
        gateway_auth = "configured"
    elif settings.allow_no_auth:
        gateway_auth = "open"
    else:
        gateway_auth = "unset"
    accounts = settings.deepseek_accounts
    diagnostics_dir = settings.diagnostics_dir
    return {
        "backend_type": settings.backend_type,
        "model_id": settings.model_id,
        "host": settings.host,
        "port": settings.port,
        "gateway_auth": gateway_auth,
        "accounts": {
            "mode": "multi" if accounts else "single",
            "count": len(accounts) if accounts else 1,
        },
        "diagnostics": {
            "enabled": diagnostics_dir is not None,
            "dir": str(diagnostics_dir) if diagnostics_dir is not None else None,
        },
        "reliability": {
            "max_retries": settings.max_retries,
            "retry_backoff_seconds": settings.retry_backoff_seconds,
            "upstream_timeout_seconds": settings.upstream_timeout_seconds,
            "account_cooldown_seconds": settings.account_cooldown_seconds,
        },
    }


# ---------------------------------------------------------------------------
# The self-contained dashboard page (GET /admin)
#
# One HTML document, inline CSS + vanilla JS, zero external assets — the
# page must work offline on a local gateway. The JS is a stateless poller
# over the /admin/* JSON endpoints; every dynamic value is escaped before
# interpolation. No secret can appear here: the page embeds nothing but
# static markup.
# ---------------------------------------------------------------------------

ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSeek Qwen Gateway &mdash; Admin</title>
<style>
  :root {
    --bg: #0f1420; --panel: #171e2e; --border: #2a3550;
    --text: #dbe2f0; --muted: #8a94ad; --accent: #5aa9ff;
    --good: #3fb96f; --bad: #e5534b; --warn: #d29922; --off: #6e7681;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
           padding: 12px 20px; border-bottom: 1px solid var(--border);
           background: var(--panel); position: sticky; top: 0; z-index: 1; }
  header h1 { font-size: 16px; margin: 0; letter-spacing: .3px; }
  nav { display: flex; gap: 6px; flex-wrap: wrap; }
  nav button { background: transparent; color: var(--muted);
               border: 1px solid var(--border); border-radius: 6px;
               padding: 6px 12px; cursor: pointer; font: inherit; }
  nav button.active { color: var(--text); border-color: var(--accent);
                      background: rgba(90, 169, 255, .12); }
  #refresh-info { margin-left: auto; color: var(--muted); font-size: 12px; }
  main { padding: 20px; max-width: 1200px; margin: 0 auto; }
  section.panel { display: none; }
  section.panel.active { display: block; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill,
           minmax(220px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid var(--border);
          border-radius: 8px; padding: 12px 14px; }
  .card-title { color: var(--muted); font-size: 12px; text-transform:
                uppercase; letter-spacing: .6px; margin-bottom: 6px; }
  .card-value { font-size: 15px; overflow-wrap: anywhere; }
  .card.good .card-value { color: var(--good); font-weight: 600; }
  .card.bad .card-value { color: var(--bad); font-weight: 600; }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--border); border-radius: 8px;
          overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px;
           border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-size: 12px; text-transform: uppercase;
       letter-spacing: .6px; }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 12px;
           border: 1px solid; }
  .st-healthy  { color: var(--good); border-color: var(--good); }
  .st-cooldown { color: var(--warn); border-color: var(--warn); }
  .st-invalid  { color: var(--bad);  border-color: var(--bad); }
  .st-disabled { color: var(--off);  border-color: var(--off); }
  td.actions button { background: transparent; color: var(--accent);
                      border: 1px solid var(--accent); border-radius: 6px;
                      padding: 3px 10px; margin-right: 6px; cursor: pointer;
                      font: inherit; font-size: 12px; }
  td.actions button:hover { background: rgba(90, 169, 255, .15); }
  .error { color: var(--bad); min-height: 18px; margin-bottom: 8px; }
  .note { color: var(--muted); font-size: 12px; }
  h2 { font-size: 14px; color: var(--muted); margin: 18px 0 8px; }
</style>
</head>
<body>
<header>
  <h1>DeepSeek Qwen Gateway</h1>
  <nav>
    <button class="tab active" data-tab="dashboard">Dashboard</button>
    <button class="tab" data-tab="accounts">Accounts</button>
    <button class="tab" data-tab="sessions">Sessions</button>
    <button class="tab" data-tab="metrics">Metrics</button>
    <button class="tab" data-tab="settings">Settings</button>
  </nav>
  <span id="refresh-info"></span>
</header>
<main>
  <section id="tab-dashboard" class="panel active"></section>
  <section id="tab-accounts" class="panel"></section>
  <section id="tab-sessions" class="panel"></section>
  <section id="tab-metrics" class="panel"></section>
  <section id="tab-settings" class="panel"></section>
</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value === null || value === undefined ? "" : value)
  .replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));

function adminHeaders() {
  const key = sessionStorage.getItem("dsqg-admin-key");
  return key ? {"Authorization": "Bearer " + key} : {};
}
async function adminFetch(url, options) {
  options = options || {};
  options.headers = Object.assign({}, adminHeaders(), options.headers || {});
  let response = await fetch(url, options);
  if (response.status === 401 && !sessionStorage.getItem("dsqg-admin-key")) {
    const key = window.prompt("Gateway API key required for remote admin access:");
    if (key) {
      sessionStorage.setItem("dsqg-admin-key", key);
      options.headers = Object.assign({}, adminHeaders(), options.headers || {});
      response = await fetch(url, options);
    }
  }
  return response;
}
async function jget(url) {
  const response = await adminFetch(url);
  if (!response.ok) throw new Error(url + " -> HTTP " + response.status);
  return response.json();
}
async function jpost(url) {
  const response = await adminFetch(url, {method: "POST"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error((body.error && body.error.message) ||
                    url + " -> HTTP " + response.status);
  }
  return body;
}

function card(title, value, cls) {
  return '<div class="card ' + (cls || "") + '">' +
         '<div class="card-title">' + title + '</div>' +
         '<div class="card-value">' + value + '</div></div>';
}
function fmtTs(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "\\u2014"; }
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return (h ? h + "h " : "") + (m ? m + "m " : "") + (sec % 60) + "s";
}
function fmtClasses(bucket) {
  const keys = Object.keys(bucket).sort();
  return keys.length ? keys.map((k) => bucket[k] + " " + k).join(" \\u00b7 ") : "0";
}
function fmtFailures(failures) {
  const keys = Object.keys(failures).sort();
  return keys.length ? keys.map((k) => failures[k] + " " + k).join(" \\u00b7 ")
                     : "none";
}
function fmtDurationSummary(summary) {
  return summary.count + " calls \\u00b7 sum " + summary.sum.toFixed(2) +
         "s \\u00b7 max " + summary.max.toFixed(2) + "s";
}
function stateSummary(byState) {
  return ["healthy", "cooldown", "invalid", "disabled"]
    .map((state) => (byState[state] || 0) + " " + state).join(" \\u00b7 ");
}

async function renderDashboard() {
  const s = await jget("/admin/summary");
  const h = s.health;
  const chat = s.metrics.requests["POST /v1/chat/completions"] || {};
  $("tab-dashboard").innerHTML =
    '<div class="cards">' +
    card("System health", h.ok ? "OK" : "NOT READY", h.ok ? "good" : "bad") +
    card("Version", esc(h.version)) +
    card("Backend", esc(h.backend.type) + " / " + esc(h.backend.status)) +
    card("Uptime", fmtDur(s.uptime_seconds)) +
    card("Accounts", s.accounts.total + " total<br>" +
         stateSummary(s.accounts.by_state)) +
    card("Conversations", s.conversations + " stored \\u00b7 " +
         s.active_sessions + " linked") +
    card("Chat requests", fmtClasses(chat)) +
    card("Backend attempts", s.metrics.backend_attempts) +
    card("Transport retries", s.metrics.transport_retries) +
    card("Session failovers", s.metrics.session_failovers) +
    card("Tool turns", s.metrics.tool_turns) +
    card("Backend failures", fmtFailures(s.metrics.backend_failures)) +
    '</div>';
}

async function renderAccounts() {
  const d = await jget("/admin/accounts");
  const rows = d.accounts.map((a) =>
    '<tr>' +
    '<td class="mono">' + esc(a.id) + '</td>' +
    '<td>' + esc(a.label) + '</td>' +
    '<td><span class="badge st-' + esc(a.state) + '">' + esc(a.state) +
        '</span></td>' +
    '<td>' + (a.enabled ? "yes" : "no") + '</td>' +
    '<td>' + (a.cooldown_remaining_seconds > 0
        ? a.cooldown_remaining_seconds.toFixed(1) + "s" : "\\u2014") + '</td>' +
    '<td>' + a.consecutive_failures + '</td>' +
    '<td>' + a.active_conversations + '</td>' +
    '<td>' + fmtTs(a.last_used_at) + '</td>' +
    '<td class="actions">' +
    (a.enabled
      ? '<button data-id="' + esc(a.id) + '" data-action="disable">Disable</button>'
      : '<button data-id="' + esc(a.id) + '" data-action="enable">Enable</button>') +
    '<button data-id="' + esc(a.id) + '" data-action="reset">Reset</button>' +
    '</td></tr>').join("");
  $("tab-accounts").innerHTML =
    '<div id="account-error" class="error"></div>' +
    '<table><thead><tr><th>id</th><th>label</th><th>state</th>' +
    '<th>enabled</th><th>cooldown</th><th>consec. failures</th>' +
    '<th>active conv.</th><th>last used</th><th>actions</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>' +
    '<p class="note">Disable releases the account&rsquo;s session links. ' +
    'Reset restores an invalid/cooling account to healthy (after ' +
    'credential rotation). Credentials are never displayed.</p>';
  $("tab-accounts").querySelectorAll("button[data-action]")
    .forEach((b) => b.addEventListener("click", onAccountAction));
}

async function onAccountAction(event) {
  const id = event.currentTarget.dataset.id;
  const action = event.currentTarget.dataset.action;
  const errorElement = $("account-error");
  errorElement.textContent = "";
  try {
    await jpost("/admin/accounts/" + encodeURIComponent(id) + "/" + action);
  } catch (error) {
    errorElement.textContent = error.message;
  }
  await renderAccounts();
}

async function renderSessions() {
  const d = await jget("/admin/sessions");
  const rows = d.sessions.length ? d.sessions.map((s) =>
    '<tr>' +
    '<td class="mono">' + esc(s.conversation_id) + '</td>' +
    '<td>' + esc(s.backend_account_id || "\\u2014") + '</td>' +
    '<td class="mono">' + esc(s.backend_session_id || "\\u2014") + '</td>' +
    '<td>' + (s.linked ? "linked" : "detached") + '</td>' +
    '<td>' + esc(s.status) + '</td>' +
    '<td>' + s.message_count + '</td>' +
    '<td>' + s.tool_call_count + '</td>' +
    '<td>' + fmtTs(s.updated_at) + '</td></tr>').join("") :
    '<tr><td colspan="8">no sessions yet</td></tr>';
  $("tab-sessions").innerHTML =
    '<table><thead><tr><th>conversation</th><th>account</th>' +
    '<th>backend session</th><th>link</th><th>status</th><th>msgs</th>' +
    '<th>tool calls</th><th>updated</th></tr></thead><tbody>' + rows +
    '</tbody></table>' +
    '<p class="note">Metadata only &mdash; conversation content is never ' +
    'exposed on the admin surface.</p>';
}

async function renderMetrics() {
  const m = await jget("/admin/metrics");
  const requestRows = Object.keys(m.requests).sort().map((endpoint) =>
    '<tr><td class="mono">' + esc(endpoint) + '</td><td>' +
    fmtClasses(m.requests[endpoint]) + '</td></tr>').join("");
  $("tab-metrics").innerHTML =
    '<div class="cards">' +
    card("Backend attempts", m.backend_attempts) +
    card("Transport retries", m.transport_retries) +
    card("Session failovers", m.session_failovers) +
    card("Tool turns", m.tool_turns) +
    card("Repair retries", m.tool_repair_retries) +
    card("Repair budget exhausted", m.tool_repair_budget_exhausted) +
    card("Backend failures", fmtFailures(m.backend_failures)) +
    card("Request time", fmtDurationSummary(m.request_seconds)) +
    card("Backend attempt time", fmtDurationSummary(m.backend_attempt_seconds)) +
    card("Uptime", fmtDur(m.uptime_seconds)) +
    '</div>' +
    '<h2>Requests by endpoint</h2>' +
    '<table><thead><tr><th>endpoint</th><th>status classes</th></tr></thead>' +
    '<tbody>' + (requestRows ||
    '<tr><td colspan="2">no requests yet</td></tr>') + '</tbody></table>';
}

async function renderSettings() {
  const s = await jget("/admin/settings");
  const rows = [
    ["backend_type", s.backend_type],
    ["model_id", s.model_id],
    ["host", s.host],
    ["port", String(s.port)],
    ["gateway_auth", s.gateway_auth],
    ["account mode", s.accounts.mode + " (" + s.accounts.count + ")"],
    ["diagnostics", s.diagnostics.enabled
      ? "enabled (" + s.diagnostics.dir + ")" : "disabled"],
    ["max_retries", String(s.reliability.max_retries)],
    ["retry_backoff_seconds", String(s.reliability.retry_backoff_seconds)],
    ["upstream_timeout_seconds", String(s.reliability.upstream_timeout_seconds)],
    ["account_cooldown_seconds",
      String(s.reliability.account_cooldown_seconds)],
  ].map(([k, v]) => '<tr><td class="mono">' + esc(k) + '</td><td>' +
                    esc(v) + '</td></tr>').join("");
  $("tab-settings").innerHTML =
    '<table><tbody>' + rows + '</tbody></table>' +
    '<p class="note">Read-only: settings are env-derived and applied on ' +
    'restart. Secrets are shown as presence only, never as values.</p>';
}

const renderers = {
  dashboard: renderDashboard,
  accounts: renderAccounts,
  sessions: renderSessions,
  metrics: renderMetrics,
  settings: renderSettings,
};
let activeTab = "dashboard";

function showTab(tab) {
  activeTab = tab;
  document.querySelectorAll("nav .tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("section.panel").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + tab));
  refresh();
}
async function refresh() {
  try {
    await renderers[activeTab]();
    $("refresh-info").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (error) {
    $("refresh-info").textContent = "refresh failed: " + error.message;
  }
}
document.querySelectorAll("nav .tab").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));
setInterval(refresh, 5000);
refresh();
</script>
</body>
</html>
"""
