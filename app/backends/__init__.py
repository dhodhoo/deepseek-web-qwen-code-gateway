"""Backend adapters.

Every upstream-specific detail lives inside a backend subpackage (currently
``deepseek_web``). The rest of the application only sees:

* the normalized event classes in :mod:`app.backends.events`
* the error taxonomy in :mod:`app.backends.errors`
"""
