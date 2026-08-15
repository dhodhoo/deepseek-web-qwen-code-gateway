"""Multi-account router (M10, ADR-037).

The in-memory account registry behind the M10 milestone: an ordered,
config-driven list of :class:`AccountRecord` rows (the ARCHITECTURE.md
accounts schema, minus persistence) and a thread-safe
:class:`AccountRouter` that owns

* **selection** — new conversations route to the USABLE account with the
  fewest active conversations (ties: least recently used, then config
  order);
* **account consequences** — attached ONLY to FINAL surfaced failures
  (the M9 transport-retry budget has already absorbed the transient
  ones): ``AUTH_INVALID`` (401-class) marks the account INVALID and
  releases its conversations' backend links so they rebuild elsewhere;
  ``RATE_LIMITED`` (429-class) puts it into a bounded COOLDOWN that
  blocks NEW conversations only;
* **sticky support** — an existing conversation with a live backend link
  keeps its account (never round-robin per turn, ARCHITECTURE.md); only
  invalid/disabled accounts lose stickiness, cooldown does NOT (the
  upstream session stays valid through a rate-limit window).

Design rules honored here (see docs/DECISIONS.md ADR-037):

* the record holds NO credential — the secret lives only inside the
  account's :class:`~app.backends.base.LLMBackend` instance, so every
  admin surface built from this module is masked by construction;
* state rebuilds from config on every restart (in-memory registry,
  ADR-020 storage precedent);
* cooldown expiry is LAZY (checked at selection/summary time — no
  background thread);
* ``consecutive_failures`` is recorded for every final failure but only
  the two named categories change state (network/protocol failures are
  not evidence against the account).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .backends.base import LLMBackend
from .backends.errors import BackendErrorCategory, BackendFailure
from .conversation import ConversationStore

__all__ = [
    "ACCOUNT_COOLDOWN",
    "ACCOUNT_DISABLED",
    "ACCOUNT_HEALTHY",
    "ACCOUNT_INVALID",
    "DEFAULT_ACCOUNT_COOLDOWN_SECONDS",
    "DEFAULT_ACCOUNT_ID",
    "AccountRecord",
    "AccountRouter",
]

#: health_status values (``disabled`` is DERIVED from ``enabled=False``).
ACCOUNT_HEALTHY = "healthy"
ACCOUNT_COOLDOWN = "cooldown"
ACCOUNT_INVALID = "invalid"
ACCOUNT_DISABLED = "disabled"

#: Default 429 cooldown window (ADR-037; ``DSQG_ACCOUNT_COOLDOWN_SECONDS``).
DEFAULT_ACCOUNT_COOLDOWN_SECONDS = 300.0

#: Account id of the single-account (backward-compatible) configuration.
DEFAULT_ACCOUNT_ID = "default"


@dataclass
class AccountRecord:
    """One row of the in-memory accounts table (ARCHITECTURE.md schema).

    ``created_at``/``updated_at`` are stamped by the router at
    construction/mutation time (injectable clock). ``backend`` is the
    account's OWN backend instance — for ``deepseek_web`` each account
    gets a separate ``DeepSeekWebBackend`` with its own vendored client,
    PoW solver and call gate, so accounts are concurrency-isolated
    (ADR-037 point 2).
    """

    id: str
    label: str
    backend: LLMBackend
    enabled: bool = True
    health_status: str = ACCOUNT_HEALTHY
    cooldown_until: float | None = None
    consecutive_failures: int = 0
    last_used_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class AccountRouter:
    """Thread-safe account registry + routing policy (ADR-037).

    Lock ordering: router lock FIRST, conversation-store lock second
    (selection and AUTH_INVALID/disable consequences derive or
    invalidate conversation state while holding the router lock). The
    server never nests the store lock around a router call, so the
    ordering is consistent and deadlock-free.
    """

    def __init__(
        self,
        accounts: Sequence[AccountRecord],
        *,
        cooldown_seconds: float = DEFAULT_ACCOUNT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not accounts:
            raise ValueError("at least one account is required")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._accounts = list(accounts)
        self._by_id = {account.id: account for account in self._accounts}
        if len(self._by_id) != len(self._accounts):
            raise ValueError("account ids must be unique")
        backend_types = {
            account.backend.backend_type for account in self._accounts
        }
        if len(backend_types) != 1:
            raise ValueError(
                "all accounts must share one backend_type, got "
                f"{sorted(backend_types)}"
            )
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.RLock()
        now = clock()
        for account in self._accounts:
            account.created_at = now
            account.updated_at = now

    # ----------------------------------------------------------- registry

    @classmethod
    def single(
        cls,
        backend: LLMBackend,
        *,
        cooldown_seconds: float = DEFAULT_ACCOUNT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> "AccountRouter":
        """One-account router — the backward-compatible shape used when a
        bare backend is injected (``DEEPSEEK_AUTH_TOKEN`` config)."""
        return cls(
            [
                AccountRecord(
                    id=DEFAULT_ACCOUNT_ID,
                    label="Default",
                    backend=backend,
                )
            ],
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )

    @property
    def backend_type(self) -> str:
        """The common backend type of every account (validated at init)."""
        return self._accounts[0].backend.backend_type

    @property
    def accounts(self) -> list[AccountRecord]:
        """All accounts in config order (live records — treat read-only)."""
        with self._lock:
            return list(self._accounts)

    @property
    def default_account(self) -> AccountRecord:
        """First account in config order (the single-account backend)."""
        with self._lock:
            return self._accounts[0]

    def get(self, account_id: str | None) -> AccountRecord | None:
        if account_id is None:
            return None
        with self._lock:
            return self._by_id.get(account_id)

    # ----------------------------------------------------------- selection

    def select_for_new_conversation(
        self, store: ConversationStore
    ) -> AccountRecord:
        """Pick the account for a NEW backend session (least-active rule).

        Usable = enabled AND (healthy OR cooldown already expired —
        lazy promotion, no background thread). Among usable accounts the
        minimum of ``(active_conversations, last_used_at-or-0, config
        order)`` wins; ``active_conversations`` is DERIVED from the store
        (self-healing under LRU eviction, like the tool-call index).
        Unused accounts sort first. Stamps ``last_used_at`` so concurrent
        selections spread across accounts deterministically.

        Raises :class:`BackendFailure` when nothing is usable:
        ``RATE_LIMITED`` (→ 429, client backs off) when at least one
        account is still cooling down, ``AUTH_INVALID`` (→ 502, operator
        action) otherwise. Messages never contain secrets.
        """
        now = self._clock()
        with self._lock:
            active = {account.id: 0 for account in self._accounts}
            for conversation in store.conversations():
                if conversation.backend_account_id in active:
                    active[conversation.backend_account_id] += 1
            usable: list[tuple[int, float, int, AccountRecord]] = []
            any_cooling = False
            for order, account in enumerate(self._accounts):
                if not account.enabled:
                    continue
                if account.health_status == ACCOUNT_INVALID:
                    continue
                if account.health_status == ACCOUNT_COOLDOWN:
                    if (
                        account.cooldown_until is not None
                        and account.cooldown_until > now
                    ):
                        any_cooling = True
                        continue
                    # Lazy promotion: the window passed — treat as healthy.
                    account.health_status = ACCOUNT_HEALTHY
                    account.cooldown_until = None
                    account.updated_at = now
                usable.append(
                    (
                        active[account.id],
                        account.last_used_at or 0.0,
                        order,
                        account,
                    )
                )
            if not usable:
                category = (
                    BackendErrorCategory.RATE_LIMITED
                    if any_cooling
                    else BackendErrorCategory.AUTH_INVALID
                )
                raise BackendFailure(
                    category=category,
                    message=(
                        "No usable backend account is available "
                        "(all disabled, invalid, or cooling down)."
                    ),
                )
            account = min(usable, key=lambda item: item[:3])[3]
            account.last_used_at = now
            account.updated_at = now
            return account

    def sticky_account(self, account_id: str | None) -> AccountRecord | None:
        """The bound account if it may keep serving its sticky session.

        Sticky survives COOLDOWN (the upstream session is still valid
        during a rate-limit window) but not invalidation or disabling —
        those return ``None`` so the caller drops the link and re-selects
        (ADR-037). Unknown/``None`` ids also return ``None``.
        """
        with self._lock:
            account = self._by_id.get(account_id) if account_id else None
            if account is None or not account.enabled:
                return None
            if account.health_status == ACCOUNT_INVALID:
                return None
            return account

    # --------------------------------------------------------- consequences

    def record_success(self, account_id: str) -> None:
        """A committed turn: healthy state restored, counters cleared.

        Success while cooling down clears the cooldown — upstream clearly
        accepted the request, so the rate-limit window is over.
        """
        with self._lock:
            account = self._by_id.get(account_id)
            if account is None:
                return
            now = self._clock()
            account.health_status = ACCOUNT_HEALTHY
            account.cooldown_until = None
            account.consecutive_failures = 0
            account.last_used_at = now
            account.updated_at = now

    def record_failure(
        self,
        account_id: str,
        category: BackendErrorCategory,
        store: ConversationStore | None = None,
    ) -> None:
        """A FINAL surfaced failure (post-M9-budget) for one account.

        ``AUTH_INVALID`` → invalid until operator action, and every
        conversation bound to the account loses its backend link (the
        next request rebuilds through the router on a usable account —
        the ADR-020 rebuild path). ``RATE_LIMITED`` → cooldown for the
        configured window (blocks new conversations only; sticky sessions
        keep working). Every other category only bumps the failure
        counter. Unknown ids are ignored (defensive — the server always
        passes a routed account).
        """
        with self._lock:
            account = self._by_id.get(account_id)
            if account is None:
                return
            now = self._clock()
            account.consecutive_failures += 1
            account.updated_at = now
            if category == BackendErrorCategory.AUTH_INVALID:
                account.health_status = ACCOUNT_INVALID
                account.cooldown_until = None
                if store is not None:
                    for conversation in store.conversations():
                        if conversation.backend_account_id == account_id:
                            store.invalidate_backend_link(conversation)
            elif category == BackendErrorCategory.RATE_LIMITED:
                account.health_status = ACCOUNT_COOLDOWN
                account.cooldown_until = now + self._cooldown_seconds

    # ------------------------------------------------------------ lifecycle

    def set_enabled(
        self,
        account_id: str,
        enabled: bool,
        store: ConversationStore | None = None,
    ) -> None:
        """Operator enable/disable (M12 surface; tested method in M10).

        Disabling also releases the account's conversation links — from
        the gateway's viewpoint those sessions are dead.
        """
        with self._lock:
            account = self._require(account_id)
            account.enabled = enabled
            account.updated_at = self._clock()
            if not enabled and store is not None:
                for conversation in store.conversations():
                    if conversation.backend_account_id == account_id:
                        store.invalidate_backend_link(conversation)

    def reset(self, account_id: str) -> None:
        """Operator restoration after credential rotation (M12 surface):
        re-enable and return to healthy with cleared counters."""
        with self._lock:
            account = self._require(account_id)
            account.enabled = True
            account.health_status = ACCOUNT_HEALTHY
            account.cooldown_until = None
            account.consecutive_failures = 0
            account.updated_at = self._clock()

    # ---------------------------------------------------------- observability

    def summary(self, store: ConversationStore) -> list[dict[str, Any]]:
        """Masked admin view (GET /admin/accounts payload, ADR-037).

        Structurally secret-free: ids, labels, derived state, counters
        and timestamps only — no credential ever exists on this side of
        the backend boundary.
        """
        now = self._clock()
        with self._lock:
            active = {account.id: 0 for account in self._accounts}
            for conversation in store.conversations():
                if conversation.backend_account_id in active:
                    active[conversation.backend_account_id] += 1
            rows: list[dict[str, Any]] = []
            for account in self._accounts:
                if not account.enabled:
                    state = ACCOUNT_DISABLED
                elif account.health_status == ACCOUNT_INVALID:
                    state = ACCOUNT_INVALID
                elif (
                    account.health_status == ACCOUNT_COOLDOWN
                    and account.cooldown_until is not None
                    and account.cooldown_until > now
                ):
                    state = ACCOUNT_COOLDOWN
                else:
                    state = ACCOUNT_HEALTHY
                cooldown_remaining = 0.0
                if (
                    state == ACCOUNT_COOLDOWN
                    and account.cooldown_until is not None
                ):
                    cooldown_remaining = max(0.0, account.cooldown_until - now)
                rows.append(
                    {
                        "id": account.id,
                        "label": account.label,
                        "enabled": account.enabled,
                        "state": state,
                        "cooldown_remaining_seconds": round(
                            cooldown_remaining, 3
                        ),
                        "consecutive_failures": account.consecutive_failures,
                        "active_conversations": active[account.id],
                        "last_used_at": account.last_used_at,
                    }
                )
            return rows

    # ------------------------------------------------------------- internal

    def _require(self, account_id: str) -> AccountRecord:
        try:
            return self._by_id[account_id]
        except KeyError:
            raise KeyError(f"unknown account id: {account_id}") from None
