"""Worker entrypoint: ``python -m pharmasafe_ai_worker``.

Phase 0 skeleton. Temporal workflow/activity registration is added in Phase 5.
"""

import logging

from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.settings import get_settings

SERVICE_NAME = "ai-worker"


def main() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    logging.getLogger(SERVICE_NAME).info(
        "worker skeleton started; no task queues registered yet",
        extra={"temporal_address": settings.temporal_address},
    )


if __name__ == "__main__":
    main()
