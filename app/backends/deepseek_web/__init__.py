"""DeepSeek Web backend adapter (private-API boundary).

Public surface for the rest of the application:

* :class:`DeepSeekWebBackend` — the M0 spike backend
* :mod:`app.backends.deepseek_web.normalize` — event normalization + error
  classification
* :mod:`app.backends.deepseek_web.sanitize` — fixture sanitization helpers

Nothing outside ``app.backends.deepseek_web`` may import ``dsk`` directly.
"""

from .backend import DeepSeekWebBackend

__all__ = ["DeepSeekWebBackend"]
