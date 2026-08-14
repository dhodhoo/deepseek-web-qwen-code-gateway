"""FastAPI application — OpenAI-compatible HTTP surface (M2/M3 subset).

Endpoints (docs/API_CONTRACT.md):

* ``GET  /health``              — service health (unauthenticated by design;
  exposes no secrets)
* ``GET  /v1/models``           — gateway model alias list (auth required)
* ``POST /v1/chat/completions`` — plain chat, non-streaming (M2) and OpenAI
  SSE streaming (M3); ``tools``/``tool_choice`` answer 400 until M6

Threading note: the DeepSeek backend is synchronous/blocking (vendored
curl-cffi). All route handlers are therefore plain ``def`` — Starlette runs
them in its threadpool so the event loop is never blocked (master prompt:
"isolate blocking upstream calls with a safe worker/thread boundary").
Streaming additionally consumes the blocking event iterator through
``starlette.concurrency.iterate_in_threadpool`` (see app/streaming.py).

Session policy (M2/M3): each request creates a fresh backend session.
Canonical conversation state and session reuse arrive in M4.
"""

from __future__ import annotations

import hmac
import time
import uuid

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .backends.base import LLMBackend
from .backends.errors import BackendErrorCategory, BackendFailure
from .backends.events import BackendError, MessageFinished, TextDelta
from .config import GatewaySettings, build_backend
from .error_mapping import backend_failure_to_response, openai_error_body
from .openai_types import (
    AssistantMessageOut,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ModelInfo,
    ModelList,
)
from .prompt_compiler import UnsupportedMessageError, compile_messages_to_prompt
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


def _start_stream_response(
    backend_: LLMBackend, cfg: GatewaySettings, prompt: str
) -> StreamingResponse:
    """Begin an SSE streaming turn (M3).

    The FIRST event is pulled synchronously (this handler runs in
    Starlette's threadpool) BEFORE any response byte is committed: failures
    raised while priming therefore still answer with a real HTTP status —
    the Qwen Code client keys its retry behavior off HTTP status
    (docs/UPSTREAM_NOTES.md). Mid-stream failures become an in-stream error
    envelope instead (app/streaming.py, ADR-019).
    """
    session = backend_.create_session()
    events = backend_.stream_turn(session.session_id, prompt)
    try:
        primed = next(events)
    except StopIteration:
        primed = STREAM_EMPTY
    except BackendFailure as failure:
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
) -> FastAPI:
    """Build the gateway application.

    ``settings`` defaults to :meth:`GatewaySettings.from_env`; ``backend``
    defaults to :func:`build_backend(settings)`. Both are injectable for
    tests (the whole M2 surface is testable offline with ``FakeBackend``).
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
            "Qwen Code. M2/M3 subset: chat completions, non-streaming and "
            "OpenAI SSE streaming."
        ),
    )
    app.state.settings = settings
    app.state.backend = backend

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
            prompt = compile_messages_to_prompt(body.messages)
        except UnsupportedMessageError as exc:
            raise GatewayHttpError(
                400,
                openai_error_body(
                    str(exc), "invalid_request_error", "UNSUPPORTED_MESSAGE"
                ),
            ) from exc

        if body.stream:
            return _start_stream_response(backend_, cfg, prompt)

        try:
            session = backend_.create_session()
            text_parts: list[str] = []
            finish_reason: str | None = None
            for event in backend_.stream_turn(session.session_id, prompt):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, MessageFinished):
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
                # UnknownDelta are intentionally ignored in M2.
        except BackendFailure as failure:
            status, error_body = backend_failure_to_response(failure)
            raise GatewayHttpError(status, error_body) from failure

        return ChatCompletionResponse(
            id=f"chatcmpl_local_{uuid.uuid4().hex}",
            created=int(time.time()),
            model=cfg.model_id,
            choices=[
                Choice(
                    message=AssistantMessageOut(content="".join(text_parts)),
                    finish_reason=_map_finish_reason(finish_reason),
                )
            ],
        )

    return app
