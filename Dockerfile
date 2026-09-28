FROM node:22-alpine AS frontend-build

WORKDIR /source/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    WORDLEARNER_BS_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.production.txt ./
RUN pip install --no-cache-dir -r requirements.production.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin wordlearner \
    && mkdir -p /app/data \
    && chown -R wordlearner:wordlearner /app/data

COPY backend/ ./backend/
COPY frontend/public/ ./frontend/public/
COPY --from=frontend-build /source/frontend/dist/ ./frontend/dist/

USER wordlearner
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
