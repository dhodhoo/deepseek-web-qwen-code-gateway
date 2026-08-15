"""FastAPI application — OpenAI-compatible HTTP surface (M2/M3/M4 subset).

Endpoints (docs/API_CONTRACT.md):

* ``GET  /health``              — service health (unauthenticated by design;
  exposes no secrets)
* ``GET  /v1/models``           — gateway model alias list (auth required)
* ``POST /v1/chat/completions`` — plain chat, non-streaming (M2) and OpenAI
  SSE streaming (M3) with multi-turn conversation continuity resolved from
  the request's own message history (M4); prompt-emulated tool calling
  (M6, ADR-023): incoming ``tools[]`` are normalized and compiled into
  deterministic prompt instructions, at most one STRICTLY parsed control
  envelope in the model output becomes a standard OpenAI ``tool_calls``
  response (both response modes), and tool-shaped HISTORY (assistant
  ``tool_calls`` + ``role=tool``) compiles into the prompt — see
  docs/TOOL_CALLING_PROTOCOL.md. ``tool_choice: 'none'`` disables tools;
  ``'required'`` demands an envelope answer. M7 (ADR-028) hardens the
  loop: tool-enabled turns are fully buffered before any response byte,
  ONE bounded repair retry runs when an envelope is missing or
  malformed, and incoming tool history is validated leniently (findings
  logged, never rejected).

M5 diagnostic capture: when ``GATEWAY_DIAGNOSTICS_DIR`` is configured,
every authenticated chat-completions request is appended (sanitized —
never the Authorization value) to ``<dir>/requests.jsonl`` so the exact
wire format of a real Qwen Code install can be fixtured (ADR-021,
app/diagnostics.py).

Threading note: the DeepSeek backend is synchronous/blocking (vendored
curl-cffi). All route handlers are therefore plain ``def`` — Starlette runs
them in its threadpool so the event loop is never blocked (master prompt:
"isolate blocking upstream calls with a safe worker/thread boundary").
Streaming additionally consumes the blocking event iterator through
``starlette.concurrency.iterate_in_threadpool`` (see app/streaming.py).

Session policy (M4, ADR-020): every request is resolved against the local
canonical state (:mod:`app.conversation`) — the source of truth. A matching
conversation with a live backend link reuses its backend session, sends only
the new (delta) messages, and threads the stored ``parent_message_id``.
New conversations — or conversations whose backend link was invalidated
after a failure — create a fresh backend session and rebuild the prompt
from the request's FULL history. Canonical history advances only when a
turn finishes (``MessageFinished``); partial turns never touch it.
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Iterator, Sequence

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .accounts import ACCOUNT_INVALID, AccountRouter
from .backends.base import LLMBackend
from .backends.errors import BackendErrorCategory, BackendFailure
from .backends.events import (
    BackendError,
    BackendMessageId,
    MessageFinished,
    MessageStarted,
    TextDelta,
)
from .config import GatewaySettings, build_router
from .conversation import (
    CanonicalMessage,
    CanonicalToolCall,
    Conversation,
    ConversationStore,
    tool_call_index,
    validate_tool_history,
)
from .diagnostics import RequestRecorder
from .error_mapping import backend_failure_to_response, openai_error_body
from .metrics import MetricsCollector, MetricsMiddleware
from .openai_types import (
    AssistantMessageOut,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    FunctionCallOut,
    ModelInfo,
    ModelList,
    ToolCallOut,
)
from .prompt_compiler import (
    UnsupportedMessageError,
    compile_canonical_to_prompt,
    messages_to_canonical,
)
from .reliability import RetryPolicy, with_transport_retry
from .streaming import sse_stream
from .tool_envelope import (
    SIMULATION_MARKERS,
    EmittedToolCall,
    EnvelopeParser,
    ToolCallEmitted,
)
from .tools import (
    TOOL_CALL_END_SENTINEL,
    TOOL_CALL_START_SENTINEL,
    CanonicalTool,
    build_tool_instructions,
    normalize_tools,
)

__all__ = ["create_app", "GatewayHttpError"]

_log = logging.getLogger("dsqg.server")

#: M7 (ADR-028 point 2): at most ONE repair retry per tool-enabled turn —
#: i.e. at most two backend calls per turn. The protocol demands the bound
#: ("Avoid infinite repair loops", docs/TOOL_CALLING_PROTOCOL.md).
MAX_TOOL_REPAIR_ATTEMPTS = 1


# ---------------------------------------------------------------------------
# M9 (ADR-036): strict terminal enforcement + transport-retry glue
# ---------------------------------------------------------------------------


def _truncation_failure() -> BackendFailure:
    """Strict-terminal failure: a turn ended WITHOUT ``MessageFinished``.

    A clean-but-markerless stream is a truncated upstream answer; handing
    it to the client as a completed ``stop`` turn would silently deliver
    a partial answer. Classified UPSTREAM_PROTOCOL but marked RETRYABLE:
    transient truncation deserves the bounded transport retry, and a
    persistent one surfaces as 502 after the budget — deterministically.
    """
    return BackendFailure(
        category=BackendErrorCategory.UPSTREAM_PROTOCOL,
        message="Upstream turn ended without a terminal marker (truncated)",
        retryable=True,
    )


def _strict_terminal(events):
    """Pass events through; raise truncation if no ``MessageFinished`` came.

    Wraps the backend pipeline closest to the source so the failure
    propagates through ``_observed_events`` (which invalidates the
    backend link on ``BackendFailure``) and — on the streaming path —
    surfaces inside ``sse_stream``'s consumption, where it becomes the
    in-stream error envelope WITHOUT ``[DONE]`` (the mid-stream failure
    contract, ADR-019).
    """
    finished = False
    for event in events:
        if isinstance(event, MessageFinished):
            finished = True
        yield event
    if not finished:
        raise _truncation_failure()


def _make_on_retry(policy: RetryPolicy):
    """Transport-retry log hook (the gateway's only retry log line)."""

    def on_retry(retry_number: int, delay: float, failure: BackendFailure) -> None:
        _log.info(
            "transport retry %d/%d after %.2fs: category=%s",
            retry_number,
            policy.max_retries,
            delay,
            failure.category.value,
        )

    return on_retry


def _tool_repair_hint(
    tools: Sequence[CanonicalTool], *, required: bool, simulated: bool
) -> str:
    """Static, deterministic repair hint for the bounded retry (M7).

    Built ONLY from client-supplied tool names and server-known trigger
    reasons — model output is never echoed back into a prompt (injection
    boundary; ADR-028 point 2). Carries one anti-simulation sentence
    (ADR-029) because the dominant live failure mode is prose that
    NARRATES a tool loop instead of emitting an envelope, plus the
    anti-imitation sentence naming the history-only context formats
    (ADR-031, ADR-034): the M8 failures wrote simulated loops as
    ``[assistant tool call]`` / ``[tool result]`` blocks and fake
    ``[User]`` / ``[assistant]`` conversation transcripts. When the
    trigger was a SIMULATION MARKER, the closing states the server-known
    fact that a tool was attempted in prose and demands the envelope —
    plus the note that earlier tool results are not repeated (that retry
    runs on the STRIPPED base, ADR-033) and a still-needed result must
    be re-requested through the envelope.
    """
    names = ", ".join(tool.name for tool in tools)
    if simulated:
        closing = (
            "You attempted a tool call by writing it out as text, which "
            "cannot be executed. You MUST request that tool call with the "
            "envelope instead — output ONLY the three envelope lines. "
            "Earlier tool results are not repeated in this message; if "
            "you still need one, request the tool call again with the "
            "envelope."
        )
    elif required:
        closing = "You MUST request exactly one tool call now."
    else:
        closing = (
            "If no tool is actually needed, answer normally in plain text "
            "without any envelope."
        )
    return (
        "Your previous response did not use the required tool-call control "
        "format, so it could not be executed. Respond again with EXACTLY "
        "one control envelope and no other text:\n\n"
        f"{TOOL_CALL_START_SENTINEL}\n"
        f'{{"name":"<one of: {names}>","arguments":{{...}}}}\n'
        f"{TOOL_CALL_END_SENTINEL}\n\n"
        "The envelope must contain exactly one JSON object; 'name' must be "
        "one of the tools listed in the available-tools block; 'arguments' "
        "must be a JSON object matching that tool's parameters schema; no "
        "markdown fences and no text before or after the envelope. "
        "Never simulate or narrate tool execution in prose — you cannot "
        "execute tools yourself, so if you need one, request it with the "
        "envelope. '[tool result]' blocks, and fake conversation turns "
        "like '[user]' or '[assistant]', in the conversation are CONTEXT "
        "for things that already happened; never output them or anything "
        "resembling them. "
        f"{closing}"
    )


class GatewayHttpError(Exception):
    """Carries a pre-built OpenAI-style error response (status + body)."""

    def __init__(self, status: int, body: dict) -> None:
        super().__init__(body.get("error", {}).get("message", "error"))
        self.status = status
        self.body = body


def _map_finish_reason(reason: str | None) -> str:
    """Backend finish reason → OpenAI finish_reason (M2 subset).

    ``length`` passes through (the Qwen Code client maps it to MAX_TOKENS);
    everything else — including missing reasons, which the client tolerates
    as UNSPECIFIED — is reported as ``stop``. Turns that emit a tool call
    are reported as ``tool_calls`` by the CALLER (M6), which overrides
    this mapping.
    """
    if reason == "length":
        return "length"
    return "stop"


def _category_or_internal(kind: str) -> BackendErrorCategory:
    try:
        return BackendErrorCategory(kind)
    except ValueError:
        return BackendErrorCategory.INTERNAL


# ---------------------------------------------------------------------------
# M4: per-request conversation resolution + canonical-state bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _TurnContext:
    """One chat-completions request's resolved conversation state (ADR-020).

    Created per request, never shared across requests. ``conversation`` is
    ``None`` when the request starts a brand-new conversation (no stored
    history matched); the conversation row is only born when the turn
    commits (commit-on-finish), so failed first turns leave no debris.

    M10 (ADR-037): ``account_id`` / ``backend`` / ``router`` carry the
    ROUTED account for this turn — sticky reuse for a live conversation
    link, least-active selection for a new session. ``backend`` is the
    selected account's own backend instance; the commit and fail helpers
    use ``router`` to record the account-level outcome of the turn.
    """

    store: ConversationStore
    backend_type: str
    incoming: list[CanonicalMessage]  # the request's full canonical history
    conversation: Conversation | None
    session_id: str
    parent_message_id: str | None
    account_id: str | None = None
    backend: LLMBackend | None = None
    router: AccountRouter | None = None


class _TurnRecorder:
    """Accumulates one turn's canonical outcome from backend events.

    ``observe`` is fed every event the backend yields — AFTER envelope
    parsing when tools are enabled, so the recorder sees renderable
    ``TextDelta`` text and :class:`ToolCallEmitted` items, never raw
    envelope fragments. The recorder collects the assistant text, emitted
    tool calls and the last backend message id (the next turn's
    ``parent_message_id``, M0 threading convention) and notes whether the
    turn finished. Committing to the store is the caller's decision —
    only on finish (ADR-020 point 5).
    """

    __slots__ = (
        "text_parts",
        "tool_calls",
        "parent_message_id",
        "finished",
        "committed",
        "finish_reason",
    )

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tool_calls: list[EmittedToolCall] = []
        self.parent_message_id: str | None = None
        self.finished = False
        self.committed = False
        self.finish_reason: str | None = None

    def observe(self, event) -> None:
        if isinstance(event, TextDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, ToolCallEmitted):
            self.tool_calls.append(event.call)
        elif isinstance(event, BackendMessageId):
            self.parent_message_id = event.id
        elif isinstance(event, MessageFinished):
            self.finished = True
            self.finish_reason = event.finish_reason

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def assistant_message(self) -> CanonicalMessage:
        """Canonical assistant message for commit (M6 tool-aware).

        Mirrors the wire shape the client will re-send: a tool-calls-only
        turn stores ``content=None`` + ``tool_calls``; a text turn keeps
        its text (possibly ``""``). Arguments are already the canonical
        compact JSON (ADR-023), so the client's re-sent history matches
        structurally and the conversation resolves.
        """
        tool_calls = (
            tuple(
                CanonicalToolCall(
                    id=call.id,
                    function_name=call.name,
                    arguments_json=call.arguments_json,
                )
                for call in self.tool_calls
            )
            if self.tool_calls
            else None
        )
        content = self.text
        if not content and tool_calls is not None:
            content = None
        return CanonicalMessage(
            role="assistant", content=content, tool_calls=tool_calls
        )


def _strip_tool_history(
    messages: Sequence[CanonicalMessage],
) -> list[CanonicalMessage]:
    """Tool-shaped messages omitted; assistant text kept (ADR-033).

    Builds the message list for the simulation-triggered repair-retry
    base: the compiled retry context must NOT show any tool-shaped
    block — no envelope, no ``[tool result]`` block — because the
    model copies whatever format its context shows (ADR-032's annotated
    headers were copied verbatim — live evidence 2026-08-15). Assistant
    messages carrying BOTH text and tool_calls keep their text;
    tool-call-only assistant messages and ``role=tool`` results are
    omitted. The caller falls back to the original prompt when nothing
    remains. Canonical history is untouched — this shapes one discarded
    retry-branch prompt (re-branch + link invalidation, ADR-028).
    """
    stripped: list[CanonicalMessage] = []
    for message in messages:
        if message.role == "tool":
            continue
        if message.role == "assistant" and message.tool_calls:
            if message.content:
                stripped.append(
                    CanonicalMessage(
                        role="assistant", content=message.content
                    )
                )
            continue
        stripped.append(message)
    return stripped


def _prepare_turn(
    request: Request,
    canonical: list[CanonicalMessage],
    tool_instructions: str | None = None,
) -> tuple[_TurnContext, str, str | None]:
    """Resolve the conversation; choose ACCOUNT, session and prompt.

    M4/ADR-020 resolution is unchanged; M10 (ADR-037) layers account
    routing ON TOP of it. STICKY: a matched conversation with a live
    backend link keeps the account that created the session (never
    round-robin per turn — ARCHITECTURE.md), and a cooling-down account
    keeps its sticky sessions too (the upstream session survives a
    rate-limit window). The router selects ONLY when a new backend
    session is needed, and only among usable accounts (healthy or
    cooldown-expired). A FINAL ``create_session`` failure records the
    selected account's consequence before crossing as an HTTP error
    through the app-level handler.

    Matched conversation with a live backend link → reuse its session and
    compile ONLY the trailing delta messages (the upstream session already
    holds prior context). Otherwise create a fresh backend session and
    compile the request's FULL history (rebuild from canonical state —
    always correct, and exactly what a restart requires). The router
    selects ONLY when a new backend session is needed, and only among
    usable accounts (healthy or cooldown-expired) — with one refinement:
    a matched conversation whose link died REBUILDS on its bound account
    when that account is still usable for sticky (cooldown allowed), so
    the M9/ADR-020 rebuild contract holds even while an account cools
    down (single-account deployments stay byte-for-byte unchanged).

    ``tool_instructions`` (M6, ADR-023) — the deterministic tool-control
    block from :func:`app.tools.build_tool_instructions` — is appended
    AFTER the compiled message blocks so it appears exactly once per
    request whether the turn compiled a full history or only a delta.

    Returns ``(context, prompt, retry_base)``. ``retry_base`` (ADR-033)
    is a STRIPPED recompilation of the same prompt messages — every
    tool-shaped message omitted — and exists ONLY for tool-enabled
    turns (``None`` otherwise). The buffered turn uses it as the
    repair-retry base prompt when the trigger includes
    ``simulation_marker``: the retry context must not show any internal
    block format, because the model copies whatever format its context
    shows (ADR-032's annotated headers were copied verbatim, live
    evidence 2026-08-15). Every other retry and the entire main path
    keep the exact pinned M2–M7 rendering.
    """
    store: ConversationStore = request.app.state.store
    router: AccountRouter = request.app.state.router
    conversation, delta = store.resolve(router.backend_type, canonical)

    # M10 (ADR-037): sticky account. A live link on a usable account
    # keeps its own backend; an invalid/disabled/unknown account releases
    # the link so the conversation rebuilds on a usable account instead.
    account = None
    if conversation is not None and conversation.backend_session_id is not None:
        bound_id = conversation.backend_account_id
        if bound_id is None:
            # Pre-M10 binding (legacy/tests): the single default account.
            bound_id = router.default_account.id
        account = router.sticky_account(bound_id)
        if account is None:
            store.invalidate_backend_link(conversation)

    if account is not None:
        session_id = conversation.backend_session_id
        parent_message_id = conversation.backend_parent_message_id
        prompt_messages = delta
    else:
        # M10 (ADR-037): a matched conversation whose link died REBUILDS
        # on its bound account when that account is still usable for
        # sticky (enabled, not invalid — cooldown allowed): the ADR-020
        # rebuild contract must hold even while the account cools down
        # (a final 429 must not make the sole account unreachable to the
        # very conversation it just failed). Only a DEAD bound account
        # (invalid/disabled/unknown) — or a genuinely new conversation —
        # goes through least-active selection among the usable accounts.
        if (
            conversation is not None
            and conversation.backend_account_id is not None
        ):
            account = router.sticky_account(conversation.backend_account_id)
        if account is None:
            account = router.select_for_new_conversation(store)
        # M9 (ADR-036): session creation is a pre-byte backend call too,
        # so it gets the same bounded transport retry (a transient network
        # hiccup here used to fail the whole request immediately).
        try:
            session = with_transport_retry(
                account.backend.create_session,
                policy=request.app.state.retry_policy,
                on_retry=_make_on_retry(request.app.state.retry_policy),
                metrics=request.app.state.metrics,
            )
        except BackendFailure as failure:
            # M10 (ADR-037): a FINAL session-creation failure punishes
            # the selected account (401-class → invalid + link release;
            # 429-class → cooldown) before surfacing as the mapped error.
            router.record_failure(account.id, failure.category, store)
            raise
        session_id = session.session_id
        parent_message_id = None
        prompt_messages = canonical

    # M6/M7: when compiling a DELTA, the assistant tool call a tool
    # result belongs to may stay in stored state — seed the name map from
    # the request's FULL canonical history so results never degrade to
    # "unknown" on the delta path. M7: through the persistent tool-call
    # ID index (ADR-028 point 4), which also backs history validation.
    known_tool_names = {
        call_id: call.function_name
        for call_id, call in tool_call_index(canonical).items()
    }
    prompt = compile_canonical_to_prompt(prompt_messages, known_tool_names)
    if tool_instructions is not None:
        prompt = f"{prompt}\n\n{tool_instructions}"
    # ADR-033: the simulation-triggered repair retry is rebuilt on a
    # STRIPPED compilation — tool-shaped messages omitted entirely — so
    # the retry context presents NO imitable block template (the model
    # copies whatever format its context shows; ADR-032's annotated
    # headers were copied verbatim, live evidence 2026-08-15). The
    # stripped shape is exactly the empirically reliable pre-loop turn:
    # text blocks + tool instructions. If stripping leaves nothing, the
    # retry falls back to the original prompt. Tool-disabled turns have
    # no retry base at all.
    retry_base = None
    if tool_instructions is not None:
        retry_messages = _strip_tool_history(prompt_messages)
        if retry_messages:
            retry_base = compile_canonical_to_prompt(retry_messages)
            retry_base = f"{retry_base}\n\n{tool_instructions}"
    context = _TurnContext(
        store=store,
        backend_type=router.backend_type,
        incoming=canonical,
        conversation=conversation,
        session_id=session_id,
        parent_message_id=parent_message_id,
        account_id=account.id,
        backend=account.backend,
        router=router,
    )
    return context, prompt, retry_base


def _commit_turn(
    context: _TurnContext, recorder: _TurnRecorder
) -> Conversation:
    """Store a completed turn: history := incoming + assistant reply.

    Returns the (possibly newly created) conversation so callers can
    post-process the backend link (M7 repair invalidation, ADR-028).

    M10 (ADR-037): the commit persists the conversation→account binding
    (sticky routing) and records account-level SUCCESS — healthy state
    restored, cooldown cleared, failure counters reset.
    """
    conversation = context.store.commit_turn(
        context.backend_type,
        context.conversation,
        context.incoming,
        recorder.assistant_message(),
        session_id=context.session_id,
        parent_message_id=recorder.parent_message_id,
        account_id=context.account_id,
    )
    recorder.committed = True
    if context.router is not None and context.account_id is not None:
        context.router.record_success(context.account_id)
    return conversation


def _invalidate_turn(context: _TurnContext) -> None:
    """Drop a failed turn's backend link; the next request rebuilds."""
    if context.conversation is not None:
        context.store.invalidate_backend_link(context.conversation)


def _fail_turn(context: _TurnContext, failure: BackendFailure) -> None:
    """Drop the backend link AND record the M10 account consequence.

    Called ONLY where a FINAL ``BackendFailure`` surfaces (the M9
    transport-retry budget has already absorbed every transient retry),
    so whatever arrives here is final: 401-class invalidates the account
    and releases its conversations' links; 429-class starts its cooldown
    (ADR-037). Mid-stream failures take the same path — the stream is
    already committed, but the account-level truth still holds.
    """
    _invalidate_turn(context)
    if context.router is not None and context.account_id is not None:
        context.router.record_failure(
            context.account_id, failure.category, context.store
        )


# ---------------------------------------------------------------------------
# M7: buffered tool turns + bounded repair policy (ADR-028)
# ---------------------------------------------------------------------------


def _drain_tool_attempt(
    backend_: LLMBackend,
    context: _TurnContext,
    prompt: str,
    parser: EnvelopeParser,
) -> _TurnRecorder:
    """Run ONE tool-enabled attempt to completion (M7 buffered path).

    The whole turn is consumed through the envelope parser BEFORE any
    response byte exists (ADR-028 point 1) — nothing unclassified can
    reach the wire, and a repair decision can still be taken. Backend
    failures propagate as ``BackendFailure``; the caller answers them
    with an HTTP status because everything here is pre-response.
    """
    recorder = _TurnRecorder()
    for event in _tool_aware_events(
        backend_.stream_turn(
            context.session_id,
            prompt,
            parent_message_id=context.parent_message_id,
        ),
        parser,
    ):
        recorder.observe(event)
        if isinstance(event, BackendError):
            # Defensive: current backends raise BackendFailure
            # (ADR-011/014); handle the event surface too.
            raise BackendFailure(
                category=_category_or_internal(event.kind),
                message=event.message,
                retryable=event.retryable,
                status_code=event.status_code,
            )
    if not recorder.finished:
        # M9 (ADR-036) strict terminal: a truncated turn must never enter
        # the repair policy (or reach the client) as if it had completed.
        # Retryable — the transport retry re-runs this drain.
        raise _truncation_failure()
    return recorder


def _run_buffered_tool_turn(
    backend_: LLMBackend,
    context: _TurnContext,
    prompt: str,
    tools: Sequence[CanonicalTool],
    *,
    required: bool,
    pre_loop: bool,
    retry_base: str | None = None,
    policy: RetryPolicy,
    metrics: MetricsCollector | None = None,
) -> tuple[_TurnRecorder, int]:
    """One tool-enabled turn under the bounded repair policy (M7).

    Returns ``(recorder, attempts_used)``. Since ADR-035 EVERY attempt
    that produced no valid tool call gets the repair retry while budget
    remains — there is no termination-guard exemption anymore. The old
    guard presumed marker-less mid-loop text was the legitimate final
    answer; the third live M8 acceptance (2026-08-15) falsified that
    premise twice — mid-loop turns keep emitting marker-less intent
    prose ("I'll read the test file...") instead of an envelope, and
    flushing it killed the loop. The named triggers (``required``,
    ``invalid_envelope_seen``, the simulation markers of ADR-031, and
    ``pre_loop`` of ADR-029) survive as LOG LABELS — an attempt that
    matches none of them is retried under the label ``no_envelope``.
    Termination stays guaranteed by construction: the budget is exactly
    :data:`MAX_TOOL_REPAIR_ATTEMPTS` retry, and the non-simulation
    repair hint explicitly permits a plain answer ("If no tool is
    actually needed, answer normally..."), so a genuine final answer
    simply costs one extra inference — its second-attempt text is
    ALWAYS flushed. Marker detection still runs on the attempt's
    ASSEMBLED text (chunk-split-proof) and only ever on the current
    inference output — history and tool results are input, never
    inspected (injection boundary) — because a simulation marker still
    switches the retry to the STRIPPED base (ADR-033). The retry reuses
    the same backend session but the SAME ORIGINAL
    ``parent_message_id`` — re-branching keeps the failed attempt out
    of the threaded upstream context (ADR-028 points 2–3). One fresh
    parser per attempt keeps the injection boundary per-inference and
    the flag scoped to its attempt.

    Retry base prompt (ADR-033): a simulation-marker retry is built on
    ``retry_base`` — a STRIPPED compilation with every tool-shaped
    message omitted — so the retry context presents no imitable block
    template (the model copies whatever format its context shows;
    ADR-032's annotated headers were copied verbatim). Every other
    trigger (and simulation retries where no base was prepared, e.g.
    direct callers of this function) keeps the exact original
    ``prompt``.
    """
    attempts_used = 0
    current_prompt = prompt
    while True:
        attempts_used += 1

        # M9 (ADR-036): each semantic attempt is wrapped in the bounded
        # TRANSPORT retry — transient failures (rate limit, network,
        # truncation) re-run the drain on the same prompt/parent before
        # the repair policy ever sees them. One fresh envelope parser per
        # attempt (semantic AND transport), keeping the injection
        # boundary per-inference; ``parser`` afterwards is the SUCCESSFUL
        # attempt's parser, which the trigger inspection below needs.
        parser = EnvelopeParser(tools)

        def _attempt(_prompt: str = current_prompt) -> _TurnRecorder:
            nonlocal parser
            parser = EnvelopeParser(tools)
            return _drain_tool_attempt(
                backend_, context, _prompt, parser
            )

        recorder = with_transport_retry(
            _attempt,
            policy=policy,
            on_retry=_make_on_retry(policy),
            metrics=metrics,
        )
        if recorder.tool_calls:
            return recorder, attempts_used
        simulated = any(
            marker in recorder.text for marker in SIMULATION_MARKERS
        )
        reasons = [
            name
            for name, active in (
                ("required", required),
                ("invalid_envelope_seen", parser.invalid_envelope_seen),
                ("simulation_marker", simulated),
                ("pre_loop", pre_loop),
            )
            if active
        ]
        # ADR-035: no termination-guard exemption — an envelope-less
        # tool-enabled turn ALWAYS gets the bounded retry while budget
        # remains. Marker-less mid-loop prose (live records 90/91) is
        # retried under the "no_envelope" label; a genuine final answer
        # answers plainly again and its text flushes (the non-simulation
        # hint explicitly permits it), so termination is preserved by
        # the budget, not by skipping the retry.
        if not reasons:
            reasons = ["no_envelope"]
        if attempts_used > MAX_TOOL_REPAIR_ATTEMPTS:
            _log.info(
                "tool-enabled turn ends after %d attempt(s); repair "
                "budget exhausted (triggers: %s)",
                attempts_used,
                ", ".join(reasons),
            )
            if metrics is not None:
                metrics.record_tool_repair_budget_exhausted()
            return recorder, attempts_used
        _log.info(
            "tool repair retry %d/%d (triggers: %s)",
            attempts_used,
            MAX_TOOL_REPAIR_ATTEMPTS,
            ", ".join(reasons),
        )
        if metrics is not None:
            metrics.record_tool_repair_retry()
        # ADR-033: simulation-marker retries rebuild on the STRIPPED
        # base (no tool-shaped history) so the retry context presents
        # no imitable block template; every other retry keeps the exact
        # original prompt.
        base = retry_base if (simulated and retry_base is not None) else prompt
        current_prompt = (
            f"{base}\n\n"
            f"{_tool_repair_hint(tools, required=required, simulated=simulated)}"
        )


def _synthesized_events(recorder: _TurnRecorder) -> list:
    """Rebuild normalized events from a buffered turn's outcome (M7).

    Feeding these through the UNCHANGED M3/M6 ``sse_stream`` reproduces
    the public chunk shapes of the old live path: role chunk, content
    increments, tool-call opener + arguments chunks, and the terminal
    chunk (finish_reason overridden to ``tool_calls`` by the renderer
    when a tool call is present). Since M9 (ADR-036) ONLY turns that
    carried a real terminal marker reach this function — truncated
    drains raise before the repair policy or the renderer can see them —
    so the synthesized stream always ends with ``MessageFinished`` and
    never fabricates a completion.
    """
    events: list = [MessageStarted()]
    events.extend(TextDelta(text) for text in recorder.text_parts)
    events.extend(ToolCallEmitted(call=call) for call in recorder.tool_calls)
    events.append(MessageFinished(recorder.finish_reason))
    return events


def _finish_tool_turn(
    context: _TurnContext, recorder: _TurnRecorder, attempts_used: int
) -> None:
    """Commit a finished buffered tool turn; drop the link after repairs.

    After a multi-attempt turn the upstream session holds an orphaned
    attempt branch the canonical history does not mirror, so the backend
    link is invalidated AFTER the commit — the next request rebuilds
    from canonical state and canonical stays the truth (ADR-028 point 3,
    ADR-020 self-healing). Single-attempt turns keep the M6 behavior:
    link intact, delta reuse on the next request.
    """
    if not recorder.finished or recorder.committed:
        return
    conversation = _commit_turn(context, recorder)
    if attempts_used > 1:
        context.store.invalidate_backend_link(conversation)


def _start_buffered_tool_stream(
    backend_: LLMBackend,
    context: _TurnContext,
    cfg: GatewaySettings,
    prompt: str,
    tools: Sequence[CanonicalTool],
    *,
    required: bool,
    pre_loop: bool,
    retry_base: str | None = None,
    policy: RetryPolicy,
    metrics: MetricsCollector | None = None,
) -> StreamingResponse:
    """SSE response for a tool-enabled turn (M7 buffered path, ADR-028).

    The turn — including any bounded repair retry — runs to completion
    BEFORE the response starts, so every failure is pre-response and
    answers with a real HTTP status (the Qwen Code client keys retries
    off status; docs/UPSTREAM_NOTES.md), and no envelope fragment can
    leak partially. The buffered outcome is re-emitted through the
    unchanged M3/M6 SSE renderer.
    """
    try:
        recorder, attempts_used = _run_buffered_tool_turn(
            backend_,
            context,
            prompt,
            tools,
            required=required,
            pre_loop=pre_loop,
            retry_base=retry_base,
            policy=policy,
            metrics=metrics,
        )
    except BackendFailure as failure:
        _fail_turn(context, failure)
        status, error_body = backend_failure_to_response(failure)
        raise GatewayHttpError(status, error_body) from failure
    _finish_tool_turn(context, recorder, attempts_used)
    # M9 (ADR-036): the synthesized stream always carries a terminal
    # marker (strict terminal), so it is never empty and never degenerate.
    events = _synthesized_events(recorder)
    primed: object = events[0]
    rest: Iterator = iter(events[1:])
    return StreamingResponse(
        sse_stream(
            primed,
            rest,
            chunk_id=f"chatcmpl_local_{uuid.uuid4().hex}",
            created=int(time.time()),
            model=cfg.model_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parsed_output_to_event(output):
    """Envelope-parser output → stream item (renderable text stays a
    ``TextDelta``; a validated envelope becomes ``ToolCallEmitted``)."""
    if isinstance(output, ToolCallEmitted):
        return output
    return TextDelta(output)


def _tool_aware_events(events, parser: EnvelopeParser | None):
    """Run one turn's backend text through the control-envelope parser (M6).

    ``parser=None`` → pass through unchanged (the exact M3 path — no
    tools, zero behavior change). Otherwise every ``TextDelta`` is fed to
    the :class:`EnvelopeParser`; its outputs become ``TextDelta`` items
    (renderable text) and at most one ``ToolCallEmitted``. The parser is
    finalized BEFORE the turn's ``MessageFinished`` is yielded onward, so
    both the SSE renderer and the canonical-state recorder downstream
    observe the fully parsed turn. Injection boundary: only THIS turn's
    own output is parsed (docs/TOOL_CALLING_PROTOCOL.md).
    """
    if parser is None:
        yield from events
        return
    finalized = False
    for event in events:
        if isinstance(event, TextDelta) and not finalized:
            for output in parser.feed(event.text):
                yield _parsed_output_to_event(output)
            continue
        if isinstance(event, MessageFinished) and not finalized:
            for output in parser.finalize():
                yield _parsed_output_to_event(output)
            finalized = True
        yield event
    if not finalized:
        # Iterator ended without MessageFinished: flush held-back text so
        # nothing the model produced is silently dropped.
        for output in parser.finalize():
            yield _parsed_output_to_event(output)


def _observed_events(events, *, context: _TurnContext, recorder: _TurnRecorder):
    """Tap backend events for canonical-state bookkeeping (streaming, M4).

    Yields every event unchanged — the SSE translator downstream sees
    exactly the same stream (M3 no-leak rules untouched). A
    ``MessageFinished`` commits the turn BEFORE the event is yielded, so
    canonical state is consistent even if the client disconnects right
    after; a ``BackendFailure`` invalidates the backend link first.
    """
    try:
        for event in events:
            recorder.observe(event)
            if isinstance(event, MessageFinished) and not recorder.committed:
                _commit_turn(context, recorder)
            yield event
    except BackendFailure as failure:
        _fail_turn(context, failure)
        raise


def _start_stream_response(
    backend_: LLMBackend,
    context: _TurnContext,
    cfg: GatewaySettings,
    prompt: str,
    parser: EnvelopeParser | None = None,
    *,
    policy: RetryPolicy,
    metrics: MetricsCollector | None = None,
) -> StreamingResponse:
    """Begin an SSE streaming turn (M3 priming + M4 state bookkeeping).

    Pipeline order: backend events → strict-terminal guard (M9) →
    control-envelope parser (M6; a no-op pass-through when tools are
    disabled) → canonical-state tap (``_observed_events``) → SSE
    renderer. The transform runs BEFORE the tap so the recorder and the
    commit see the parsed turn (renderable text + emitted tool call),
    never raw envelope fragments.

    The FIRST event is pulled synchronously (this handler runs in
    Starlette's threadpool) BEFORE any response byte is committed:
    failures raised while priming therefore still answer with a real
    HTTP status — the Qwen Code client keys its retry behavior off HTTP
    status (docs/UPSTREAM_NOTES.md). M9 (ADR-036): priming is wrapped in
    the bounded transport retry — each attempt builds a FRESH pipeline
    (fresh recorder, fresh backend generator), so a retryable pre-byte
    failure (rate limit, network, zero-event/first-event truncation)
    re-issues the turn before any status goes out. Mid-stream failures
    (after HTTP 200 is committed) are never retried; they become an
    in-stream error envelope instead (app/streaming.py, ADR-019) — same
    contract when the strict-terminal guard fires on a truncated stream.

    Note: the route only ever passes ``parser=None`` (tool-enabled turns
    use the buffered path, M7); retried priming therefore never reuses a
    partially fed parser.
    """

    def _prime_attempt():
        pipeline = _observed_events(
            _strict_terminal(
                _tool_aware_events(
                    backend_.stream_turn(
                        context.session_id,
                        prompt,
                        parent_message_id=context.parent_message_id,
                    ),
                    parser,
                )
            ),
            context=context,
            recorder=_TurnRecorder(),
        )
        # A zero-event turn raises the (retryable) truncation failure
        # from _strict_terminal here — StopIteration can no longer escape
        # priming, so STREAM_EMPTY is unreachable via the routes (ADR-036).
        primed = next(pipeline)
        return pipeline, primed

    try:
        events, primed = with_transport_retry(
            _prime_attempt,
            policy=policy,
            on_retry=_make_on_retry(policy),
            metrics=metrics,
        )
    except BackendFailure as failure:
        _fail_turn(context, failure)
        status, error_body = backend_failure_to_response(failure)
        raise GatewayHttpError(status, error_body) from failure
    if isinstance(primed, BackendError):
        # Headers are not committed yet: convert to an HTTP status too.
        failure = BackendFailure(
            category=_category_or_internal(primed.kind),
            message=primed.message,
            retryable=primed.retryable,
            status_code=primed.status_code,
        )
        _fail_turn(context, failure)
        status, error_body = backend_failure_to_response(failure)
        raise GatewayHttpError(status, error_body) from failure
    return StreamingResponse(
        sse_stream(
            primed,
            events,
            chunk_id=f"chatcmpl_local_{uuid.uuid4().hex}",
            created=int(time.time()),
            model=cfg.model_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app(
    settings: GatewaySettings | None = None,
    backend: LLMBackend | None = None,
    store: ConversationStore | None = None,
    metrics: MetricsCollector | None = None,
    router: AccountRouter | None = None,
) -> FastAPI:
    """Build the gateway application.

    ``settings`` defaults to :meth:`GatewaySettings.from_env`; ``store``
    defaults to a fresh bounded in-memory :class:`ConversationStore`
    (ADR-020); ``metrics`` defaults to a fresh :class:`MetricsCollector`
    (M9, ADR-036). All are injectable for tests (the whole surface is
    testable offline with ``FakeBackend``).

    M10 (ADR-037): ``router`` is the account registry every chat request
    routes through. It defaults to :func:`build_router(settings)` — or,
    when a bare ``backend`` is injected (the pre-M10 test pattern), a
    one-account router wrapping it (id ``default``), so every existing
    call site keeps its exact behavior. Passing both ``backend`` and
    ``router`` is a programming error. ``app.state.backend`` stays the
    FIRST account's backend for compatibility.
    """
    if settings is None:
        settings = GatewaySettings.from_env()
    if router is None:
        if backend is None:
            router = build_router(settings)
        else:
            router = AccountRouter.single(
                backend,
                cooldown_seconds=settings.account_cooldown_seconds,
            )
    elif backend is not None:
        raise ValueError("pass either backend or router, not both")
    backend = router.default_account.backend
    if metrics is None:
        metrics = MetricsCollector()

    app = FastAPI(
        title="DeepSeek Qwen Gateway",
        version=__version__,
        description=(
            "Local-first OpenAI-compatible gateway exposing DeepSeek Web to "
            "Qwen Code. M2/M3/M4 subset: chat completions, non-streaming "
            "and OpenAI SSE streaming, canonical conversation state."
        ),
    )
    app.state.settings = settings
    app.state.backend = backend
    # M10 (ADR-037): the account registry behind every routing decision.
    app.state.router = router
    app.state.store = store if store is not None else ConversationStore()
    # M9 (ADR-036): reliability knobs shared by every request path — the
    # bounded transport-retry policy (from settings) and the collector
    # behind GET /admin/metrics.
    app.state.metrics = metrics
    app.state.retry_policy = RetryPolicy(
        max_retries=settings.max_retries,
        backoff_seconds=settings.retry_backoff_seconds,
    )
    # Pure ASGI instrumentation (streaming-safe): records every request's
    # endpoint, final status class and duration.
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    # M5 (ADR-021): opt-in diagnostic request capture; None when disabled.
    app.state.recorder = (
        RequestRecorder(settings.diagnostics_dir)
        if settings.diagnostics_dir is not None
        else None
    )

    # ------------------------------------------------------------- errors

    @app.exception_handler(GatewayHttpError)
    async def _gateway_http_error_handler(
        request: Request, exc: GatewayHttpError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body)

    @app.exception_handler(BackendFailure)
    async def _backend_failure_handler(
        request: Request, exc: BackendFailure
    ) -> JSONResponse:
        status, body = backend_failure_to_response(exc)
        return JSONResponse(status_code=status, content=body)

    # --------------------------------------------------------------- auth

    def require_gateway_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        """Secure-by-default gateway key check for /v1/* (ADR-017).

        * key configured → ``Authorization: Bearer <key>`` required (401);
        * no key + ``allow_no_auth`` → open (development opt-in);
        * no key + not allowed → 503 (misconfiguration, refuse to serve).
        """
        cfg: GatewaySettings = request.app.state.settings
        expected = cfg.gateway_api_key
        if expected is None:
            if cfg.allow_no_auth:
                return
            raise GatewayHttpError(
                503,
                openai_error_body(
                    "Gateway API key is not configured. Set "
                    "DEEPSEEK_GATEWAY_API_KEY (or GATEWAY_ALLOW_NO_AUTH=1 "
                    "for local development).",
                    "server_error",
                    "GATEWAY_API_KEY_NOT_CONFIGURED",
                ),
            )
        if not authorization or not authorization[:7].lower() == "bearer ":
            raise GatewayHttpError(
                401,
                openai_error_body(
                    "Missing bearer API key in the Authorization header.",
                    "authentication_error",
                    "invalid_api_key",
                ),
            )
        token = authorization[7:].strip()
        if not hmac.compare_digest(token, expected.get_secret_value()):
            raise GatewayHttpError(
                401,
                openai_error_body(
                    "Invalid gateway API key.",
                    "authentication_error",
                    "invalid_api_key",
                ),
            )

    # -------------------------------------------------------------- routes

    @app.get("/health")
    def health(request: Request) -> dict:
        """Process/service health (never exposes secrets)."""
        router_: AccountRouter = request.app.state.router
        snapshot = router_.default_account.backend.health_check()
        # M10 (ADR-037): ready when the local backend reports ready AND
        # at least one enabled account is not invalid. Single-account
        # deployments keep the exact pre-M10 semantics until a final 401
        # retires the account.
        accounts_ok = any(
            account.enabled and account.health_status != ACCOUNT_INVALID
            for account in router_.accounts
        )
        ok = snapshot.ready and accounts_ok
        return {
            "ok": ok,
            "version": __version__,
            "backend": {
                "type": snapshot.backend_type,
                "status": "ready" if ok else "not_ready",
            },
        }

    @app.get("/admin/metrics")
    def admin_metrics(request: Request) -> dict:
        """Operational counters (M9, ADR-036).

        Unauthenticated like ``/health``: the gateway binds locally and
        the payload carries counters and durations only — no prompts, no
        ids, never secrets.
        """
        return request.app.state.metrics.snapshot()

    @app.get("/admin/accounts")
    def admin_accounts(request: Request) -> dict:
        """Masked account registry view (M10, ADR-037).

        Unauthenticated like ``/admin/metrics``: the gateway binds
        locally and the payload is STRUCTURALLY secret-free — ids,
        labels, derived state (disabled > invalid > cooldown > healthy),
        cooldown counters and timestamps. Credentials live only inside
        the account backends and are never serialized by this module.
        Read-only in M10: account lifecycle management arrives with the
        M12 admin UI.
        """
        router_: AccountRouter = request.app.state.router
        store_: ConversationStore = request.app.state.store
        return {"accounts": router_.summary(store_)}

    @app.get(
        "/v1/models",
        dependencies=[Depends(require_gateway_auth)],
    )
    def list_models(request: Request) -> ModelList:
        cfg: GatewaySettings = request.app.state.settings
        return ModelList(data=[ModelInfo(id=cfg.model_id)])

    @app.post(
        "/v1/chat/completions",
        dependencies=[Depends(require_gateway_auth)],
    )
    def chat_completions(
        body: ChatCompletionRequest, request: Request
    ) -> ChatCompletionResponse:
        # NOTE: the tool-call path returns a JSONResponse instance (for
        # exclude_none serialization), which FastAPI passes through as-is
        # regardless of this annotation.
        cfg: GatewaySettings = request.app.state.settings
        # M9 (ADR-036): the bounded transport-retry policy and the
        # metrics collector live on app.state (built in create_app).
        policy: RetryPolicy = request.app.state.retry_policy
        metrics: MetricsCollector | None = request.app.state.metrics

        # M5 (ADR-021): capture the request BEFORE any validation so the
        # diagnostic layer also records shapes the gateway rejects — that
        # is exactly what the wire-compatibility fixtures need.
        recorder: RequestRecorder | None = request.app.state.recorder
        if recorder is not None:
            recorder.record(
                "POST",
                "/v1/chat/completions",
                headers=request.headers,
                # exclude_none keeps the record close to the raw wire shape
                # (fields the client omitted stay omitted).
                body=body.model_dump(mode="json", exclude_none=True),
            )

        if body.model != cfg.model_id:
            raise GatewayHttpError(
                404,
                openai_error_body(
                    f"The model '{body.model}' does not exist.",
                    "invalid_request_error",
                    "model_not_found",
                ),
            )
        # M6 (ADR-023; supersedes the M5 "accept and ignore" behavior of
        # ADR-021): incoming tools[] are normalized and compiled into
        # deterministic prompt instructions; at most one strictly parsed
        # control envelope in the model output becomes a standard OpenAI
        # tool_calls response. ``tool_choice: 'none'`` disables tools
        # entirely; ``'required'`` demands an envelope answer (Qwen Code
        # sends only these two values — docs/UPSTREAM_NOTES.md).
        tools = normalize_tools(body.tools)
        tools_enabled = bool(tools) and body.tool_choice != "none"
        required = body.tool_choice == "required"
        tool_instructions = (
            build_tool_instructions(tools, required=required)
            if tools_enabled
            else None
        )

        try:
            canonical = messages_to_canonical(body.messages)
        except UnsupportedMessageError as exc:
            raise GatewayHttpError(
                400,
                openai_error_body(
                    str(exc), "invalid_request_error", "UNSUPPORTED_MESSAGE"
                ),
            ) from exc

        # M7 (ADR-028 point 5): lenient history validation — orphan tool
        # results / missing tool_call_ids are logged for operators and
        # compiled as-is, never rejected (ADR-023 lenient-in).
        findings = validate_tool_history(canonical)
        if not findings.clean:
            _log.warning(
                "tool history anomalies (compiling as-is): %d orphan tool "
                "result(s) %s, %d missing tool_call_id(s)",
                len(findings.orphan_tool_results),
                list(findings.orphan_tool_results[:3]),
                findings.missing_tool_call_ids,
            )

        # ADR-029: PRE-LOOP plain-text repair — the canonical history
        # holds no assistant tool call yet, so an envelope-less text
        # answer on this tool-enabled turn gets the bounded repair retry
        # (dominant live failure: prose-simulated tool use). Once a loop
        # exists, text answers are presumed final and never repaired —
        # EXCEPT text carrying a simulation marker (ADR-031), which the
        # buffered turn detects on its own assembled output.
        pre_loop = not tool_call_index(canonical)

        context, prompt, retry_base = _prepare_turn(
            request, canonical, tool_instructions
        )
        # M10 (ADR-037): the ROUTED account's backend serves this turn —
        # sticky for existing sessions, least-active selection for new
        # ones; account consequences attach on commit/final failure.
        backend_: LLMBackend = context.backend

        if tools_enabled and metrics is not None:
            metrics.record_tool_turn()

        if body.stream:
            if tools_enabled:
                # M7 (ADR-028): buffered tool turn + bounded repair — the
                # whole turn completes before any SSE byte is committed.
                return _start_buffered_tool_stream(
                    backend_,
                    context,
                    cfg,
                    prompt,
                    tools,
                    required=required,
                    pre_loop=pre_loop,
                    retry_base=retry_base,
                    policy=policy,
                    metrics=metrics,
                )
            # Tool-disabled streaming stays on the M3 path, plus the M9
            # bounded transport retry around priming + strict terminal.
            return _start_stream_response(
                backend_, context, cfg, prompt, policy=policy, metrics=metrics
            )

        if tools_enabled:
            # M7 (ADR-028): the non-streaming tool path shares the
            # buffered attempt loop — same repair policy, same commit and
            # link-invalidation rules as the streaming tool path.
            try:
                recorder, attempts_used = _run_buffered_tool_turn(
                    backend_,
                    context,
                    prompt,
                    tools,
                    required=required,
                    pre_loop=pre_loop,
                    retry_base=retry_base,
                    policy=policy,
                    metrics=metrics,
                )
            except BackendFailure as failure:
                _fail_turn(context, failure)
                status, error_body = backend_failure_to_response(failure)
                raise GatewayHttpError(status, error_body) from failure
            _finish_tool_turn(context, recorder, attempts_used)
            finish_reason: str | None = recorder.finish_reason
        else:
            # M9 (ADR-036): the whole non-streaming drain is pre-byte, so
            # it sits inside the bounded transport retry; strict terminal
            # turns a markerless stream into the retryable truncation
            # failure instead of a fabricated ``stop`` answer.
            def _plain_attempt() -> _TurnRecorder:
                local = _TurnRecorder()
                for event in backend_.stream_turn(
                    context.session_id,
                    prompt,
                    parent_message_id=context.parent_message_id,
                ):
                    local.observe(event)
                    if isinstance(event, BackendError):
                        # Defensive: current backends raise BackendFailure
                        # (ADR-011/014); handle the event surface too.
                        raise BackendFailure(
                            category=_category_or_internal(event.kind),
                            message=event.message,
                            retryable=event.retryable,
                            status_code=event.status_code,
                        )
                    # ReasoningDelta / MessageStarted / BackendMessageId /
                    # UnknownDelta need no response rendering; the recorder
                    # keeps whatever canonical state needs (M4).
                if not local.finished:
                    raise _truncation_failure()
                return local

            try:
                recorder = with_transport_retry(
                    _plain_attempt,
                    policy=policy,
                    on_retry=_make_on_retry(policy),
                    metrics=metrics,
                )
            except BackendFailure as failure:
                _fail_turn(context, failure)
                status, error_body = backend_failure_to_response(failure)
                raise GatewayHttpError(status, error_body) from failure
            finish_reason = recorder.finish_reason
            if not recorder.committed:
                _commit_turn(context, recorder)

        tool_calls_out = [
            ToolCallOut(
                id=call.id,
                function=FunctionCallOut(
                    name=call.name, arguments=call.arguments_json
                ),
            )
            for call in recorder.tool_calls
        ] or None
        content = recorder.text
        if not content and tool_calls_out is not None:
            # Wire shape of a tool-calls-only turn: content omitted/null.
            content = None
        response = ChatCompletionResponse(
            id=f"chatcmpl_local_{uuid.uuid4().hex}",
            created=int(time.time()),
            model=cfg.model_id,
            choices=[
                Choice(
                    message=AssistantMessageOut(
                        content=content, tool_calls=tool_calls_out
                    ),
                    finish_reason=(
                        "tool_calls"
                        if tool_calls_out is not None
                        else _map_finish_reason(finish_reason)
                    ),
                )
            ],
        )
        # exclude_none keeps plain responses on the exact M2 shape (no
        # ``tool_calls: null``) and renders tool turns the way Qwen Code
        # itself sends them (tool_calls present, content omitted).
        return JSONResponse(
            content=response.model_dump(mode="json", exclude_none=True)
        )

    return app
