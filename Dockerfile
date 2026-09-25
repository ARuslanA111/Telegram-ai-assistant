FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_MODE=demo ASSISTANT_DB=/data/assistant.sqlite3
WORKDIR /app
COPY assistant ./assistant
COPY README.md .env.example pyproject.toml ./
RUN useradd --system --uid 10001 assistant && mkdir -p /data && chown -R assistant:assistant /app /data
USER assistant
CMD ["python", "-m", "assistant", "run"]
