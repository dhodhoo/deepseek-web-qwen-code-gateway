"""Gateway entry point.

Run:

    .venv\\Scripts\\python.exe -m app.main

Configuration comes from the environment via
:meth:`app.config.GatewaySettings.from_env`, with the repository-root
``.env`` file merged underneath (real environment variables always win;
ADR-022). See .env.example for every variable.
"""

from __future__ import annotations

import logging

import uvicorn

from .config import GatewaySettings, load_env_file
from .server import create_app


def main() -> None:
    # Operator visibility for gateway-internal events (e.g. the bounded
    # repair policy's INFO lines, dsqg.server); uvicorn keeps its own
    # loggers configured separately.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = GatewaySettings.from_env(load_env_file())
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
