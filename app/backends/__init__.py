"""Backend adapters.

Every upstream-specific detail lives inside a backend subpackage (currently
``deepseek_web``). The rest of the application only sees:

* the stable backend interface in :mod:`app.backends.base`
  (:class:`LLMBackend`, :class:`BackendSession`, :class:`BackendHealth`)
* the normalized event classes in :mod:`app.backends.events`
* the error taxonomy in :mod:`app.backends.errors`
* :class:`app.backends.fake.FakeBackend` for deterministic tests/dev

Importing this package does NOT pull in the vendored DeepSeek client; only
:mod:`app.backends.deepseek_web` does (and only it may import ``dsk``).
"""

from .base import BackendHealth, BackendSession, LLMBackend
from .fake import FakeBackend

__all__ = [
    "LLMBackend",
    "BackendSession",
    "BackendHealth",
    "FakeBackend",
]
