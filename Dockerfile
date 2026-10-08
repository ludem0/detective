FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY case/ case/

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app app \
    && mkdir -p /app/data && chown app:app /app/data
USER app

ENV DATA_DIR=/app/data
EXPOSE 8000
# Один воркер: блокировка SQLite и лимитер входа живут в памяти процесса.
CMD ["uvicorn", "app.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
