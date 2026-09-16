# syntax=docker/dockerfile:1.7
#
# TG Chat Guard — образ бота-модератора.
#
# Две стадии: в builder ставятся зависимости в изолированный venv, в runtime
# копируется только этот venv и код. pip, кэши и заголовки сборки в итоговый
# образ не попадают.

ARG PYTHON_VERSION=3.12

# ------------------------------------------------------------------ builder ---
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Сначала только requirements: слой с зависимостями кэшируется и не
# пересобирается при каждой правке кода.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt

# ------------------------------------------------------------------ runtime ---
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="tg-chat-guard" \
      org.opencontainers.image.description="Telegram anti-spam moderator bot (aiogram 3)" \
      org.opencontainers.image.source="https://github.com/vindibee/tg-chat-guard"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:${PATH}"

# Системный пользователь без shell и пароля. Фиксированный UID удобен для
# политик Kubernetes (runAsNonRoot) и прав на тома.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser \
       --home-dir /app --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Код принадлежит root и только читается: скомпрометированный процесс
# не сможет подменить собственные исходники.
COPY alembic.ini main.py ./
COPY migrations ./migrations
COPY bot ./bot

USER appuser

# Бот работает через long polling и не слушает порты — EXPOSE не нужен.
# Миграции накатываются при старте (RUN_MIGRATIONS_ON_STARTUP=true).
CMD ["python", "main.py"]
