"""Gateway entry point.

Run:

    .venv\\Scripts\\python.exe -m app.main

Configuration comes from the environment via
:meth:`app.config.GatewaySettings.from_env`, with the repository-root
``.env`` file merged underneath (real environment variables always win;
ADR-022). See .env.example for every variable.
"""

from __future__ import annotations

import uvicorn

from .config import GatewaySettings, load_env_file
from .server import create_app


def main() -> None:
    settings = GatewaySettings.from_env(load_env_file())
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
