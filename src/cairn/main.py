"""Entry point — launches the uvicorn server (console script `cairn`)."""

from __future__ import annotations

import uvicorn

from cairn.config import get_settings
from cairn.logging import configure


def run() -> None:
    settings = get_settings()
    configure(settings.log_level)
    uvicorn.run(
        "cairn.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
