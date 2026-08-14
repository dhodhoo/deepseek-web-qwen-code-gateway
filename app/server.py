"""FastAPI application — OpenAI-compatible HTTP surface (M2/M3/M4 subset).

Endpoints (docs/API_CONTRACT.md):

* ``GET  /health``              — service health (unauthenticated by design;
  exposes no secrets)
* ``GET  /v1/models``           — gateway model alias list (auth required)
* ``POST /v1/chat/completions`` — plain chat, non-streaming (M2) and OpenAI
  SSE streaming (M3) with multi-turn conversation continuity resolved from
  the request's own message history (M4); ``tools``/``tool_choice`` answer
  400 until M6

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
import time
import uuid
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .backends.base import LLMBackend
from .backends.errors import BackendErrorCategory, BackendFailure
from .backends.events import (
    BackendError,
    BackendMessageId,
    MessageFinished,
    TextDelta,
)
from .config import GatewaySettings, build_backend
from .conversation import CanonicalMessage, Conversation, ConversationStore
from .error_mapping import backend_failure_to_response, openai_error_body
from .openai_types import (
    AssistantMessageOut,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ModelInfo,
    ModelList,
)
from .prompt_compiler import (
    UnsupportedMessageError,
    compile_canonical_to_prompt,
    messages_to_canonical,
)
from .streaming import STREAM_EMPTY, sse_stream

__all__ = ["create_app", "GatewayHttpError"]


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
    as UNSPECIFIED — is reported as ``stop``. ``tool_calls`` arrives in M6.
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
    """

    store: ConversationStore
    backend_type: str
    incoming: list[CanonicalMessage]  # the request's full canonical history
    conversation: Conversation | None
    session_id: str
    parent_message_id: str | None


class _TurnRecorder:
    """Accumulates one turn's canonical outcome from backend events.

    ``observe`` is fed every event the backend yields (both response modes).
    The recorder collects the assistant text and the last backend message id
    (the next turn's ``parent_message_id``, M0 threading convention) and
    notes whether the turn finished. Committing to the store is the caller's
    decision — only on finish (ADR-020 point 5).
    """

    __slots__ = ("text_parts", "parent_message_id", "finished", "committed")

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.parent_message_id: str | None = None
        self.finished = False
        self.committed = False

    def observe(self, event) -> None:
        if isinstance(event, TextDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, BackendMessageId):
            self.parent_message_id = event.id
        elif isinstance(event, MessageFinished):
            self.finished = True

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def assistant_message(self) -> CanonicalMessage:
        return CanonicalMessage(role="assistant", content=self.text)


def _prepare_turn(
    request: Request,
    backend_: LLMBackend,
    canonical: list[CanonicalMessage],
) -> tuple[_TurnContext, str]:
    """Resolve the conversation; choose backend session and prompt (ADR-020).

    Matched conversation with a live backend link → reuse its session and
    compile ONLY the trailing delta messages (the upstream session already
    holds prior context). Otherwise create a fresh backend session and
    compile the request's FULL history (rebuild from canonical state —
    always correct, and exactly what a restart requires). ``create_session``
    may raise ``BackendFailure``; it crosses as an HTTP error through the
    app-level handler.
    """
    store: ConversationStore = request.app.state.store
    conversation, delta = store.resolve(backend_.backend_type, canonical)

    if conversation is not None and conversation.backend_session_id is not None:
        session_id = conversation.backend_session_id
        parent_message_id = conversation.backend_parent_message_id
        prompt_messages = delta
    else:
        session = backend_.create_session()
        session_id = session.session_id
        parent_message_id = None
        prompt_messages = canonical

    prompt = compile_canonical_to_prompt(prompt_messages)
    context = _TurnContext(
        store=store,
        backend_type=backend_.backend_type,
        incoming=canonical,
        conversation=conversation,
        session_id=session_id,
        parent_message_id=parent_message_id,
    )
    return context, prompt


def _commit_turn(context: _TurnContext, recorder: _TurnRecorder) -> None:
    """Store a completed turn: history := incoming + assistant reply."""
    context.store.commit_turn(
        context.backend_type,
        context.conversation,
        context.incoming,
        recorder.assistant_message(),
        session_id=context.session_id,
        parent_message_id=recorder.parent_message_id,
    )
    recorder.committed = True


def _invalidate_turn(context: _TurnContext) -> None:
    """Drop a failed turn's backend link; the next request rebuilds."""
    if context.conversation is not None:
        context.store.invalidate_backend_link(context.conversation)


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
    except BackendFailure:
        _invalidate_turn(context)
        raise


def _start_stream_response(
    backend_: LLMBackend,
    context: _TurnContext,
    cfg: GatewaySettings,
    prompt: str,
) -> StreamingResponse:
    """Begin an SSE streaming turn (M3 priming + M4 state bookkeeping).

    The FIRST event is pulled synchronously (this handler runs in
    Starlette's threadpool) BEFORE any response byte is committed: failures
    raised while priming therefore still answer with a real HTTP status —
    the Qwen Code client keys its retry behavior off HTTP status
    (docs/UPSTREAM_NOTES.md). Mid-stream failures become an in-stream error
    envelope instead (app/streaming.py, ADR-019).
    """
    recorder = _TurnRecorder()
    events = _observed_events(
        backend_.stream_turn(
            context.session_id,
            prompt,
            parent_message_id=context.parent_message_id,
        ),
        context=context,
        recorder=recorder,
    )
    try:
        primed = next(events)
    except StopIteration:
        primed = STREAM_EMPTY
    except BackendFailure as failure:
        _invalidate_turn(context)
        status, error_body = backend_failure_to_response(failure)
        raise GatewayHttpError(status, error_body) from failure
    if isinstance(primed, BackendError):
        # Headers are not committed yet: convert to an HTTP status too.
        _invalidate_turn(context)
        failure = BackendFailure(
            category=_category_or_internal(primed.kind),
            message=primed.message,
            retryable=primed.retryable,
            status_code=primed.status_code,
        )
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
) -> FastAPI:
    """Build the gateway application.

    ``settings`` defaults to :meth:`GatewaySettings.from_env`; ``backend``
    defaults to :func:`build_backend(settings)`; ``store`` defaults to a
    fresh bounded in-memory :class:`ConversationStore` (ADR-020). All three
    are injectable for tests (the whole surface is testable offline with
    ``FakeBackend``).
    """
    if settings is None:
        settings = GatewaySettings.from_env()
    if backend is None:
        backend = build_backend(settings)

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
    app.state.store = store if store is not None else ConversationStore()

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
        backend_: LLMBackend = request.app.state.backend
        snapshot = backend_.health_check()
        return {
            "ok": snapshot.ready,
            "version": __version__,
            "backend": {
                "type": snapshot.backend_type,
                "status": "ready" if snapshot.ready else "not_ready",
            },
        }

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
        cfg: GatewaySettings = request.app.state.settings
        backend_: LLMBackend = request.app.state.backend

        if body.model != cfg.model_id:
            raise GatewayHttpError(
                404,
                openai_error_body(
                    f"The model '{body.model}' does not exist.",
                    "invalid_request_error",
                    "model_not_found",
                ),
            )
        if body.tools or body.tool_choice is not None:
            raise GatewayHttpError(
                400,
                openai_error_body(
                    "tools/tool_choice are not supported yet (milestone M6).",
                    "invalid_request_error",
                    "TOOLS_NOT_YET_SUPPORTED",
                ),
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

        context, prompt = _prepare_turn(request, backend_, canonical)

        if body.stream:
            return _start_stream_response(backend_, context, cfg, prompt)

        recorder = _TurnRecorder()
        try:
            finish_reason: str | None = None
            for event in backend_.stream_turn(
                context.session_id,
                prompt,
                parent_message_id=context.parent_message_id,
            ):
                recorder.observe(event)
                if isinstance(event, MessageFinished):
                    finish_reason = event.finish_reason
                elif isinstance(event, BackendError):
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
        except BackendFailure as failure:
            _invalidate_turn(context)
            status, error_body = backend_failure_to_response(failure)
            raise GatewayHttpError(status, error_body) from failure

        if recorder.finished and not recorder.committed:
            _commit_turn(context, recorder)

        return ChatCompletionResponse(
            id=f"chatcmpl_local_{uuid.uuid4().hex}",
            created=int(time.time()),
            model=cfg.model_id,
            choices=[
                Choice(
                    message=AssistantMessageOut(content=recorder.text),
                    finish_reason=_map_finish_reason(finish_reason),
                )
            ],
        )

    return app
