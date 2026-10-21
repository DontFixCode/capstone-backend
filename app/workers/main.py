"""워커 실행 골격임을 알리고 실제 작업 없이 정상 종료한다."""

import logging

from app.core.config import Settings
from app.core.logging import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).warning("워커 실행 골격이며 실제 작업은 미구현")


if __name__ == "__main__":
    main()
