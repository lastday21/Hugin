from __future__ import annotations

import uvicorn

from hugin.core.settings import get_settings
from hugin.database import upgrade_database
from hugin.diagnostics import OperationJournal


def main() -> None:
    settings = get_settings()
    starting = OperationJournal(settings.data_dir).start(
        "server",
        "database.upgrade",
    )
    try:
        upgrade_database(settings)
    except Exception as error:
        starting.fail(error)
        raise
    starting.succeed()
    uvicorn.run(
        "hugin.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
