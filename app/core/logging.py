"""표준 logging으로 API와 워커의 기본 로그 형식을 설정한다."""

import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
