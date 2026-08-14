"""Gateway entry point.

Run:

    .venv\\Scripts\\python.exe -m app.main

Configuration comes from the environment via
:meth:`app.config.GatewaySettings.from_env` (see .env.example).
"""

from __future__ import annotations

import uvicorn

from .config import GatewaySettings
from .server import create_app


def main() -> None:
    settings = GatewaySettings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
