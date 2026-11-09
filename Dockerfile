# 백엔드 API·워커 공용 이미지. 서버 배포는 compose.server.yaml, 로컬 컨테이너 확인은 compose.api.yaml을 쓴다.
# 시험을 돌린 런타임(Python 3.13)과 맞춘다. 비밀값(.env·.env.api)은 이미지에 넣지 않고 실행 시 주입한다.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache

COPY app ./app
# 앱 검색 지원 범위 판정용 대전 행정경계(app/modules/routing/service_area.py 기본 경로).
COPY data/service_area ./data/service_area

EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
