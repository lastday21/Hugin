from __future__ import annotations

import uvicorn

from hugin.core.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "hugin.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
