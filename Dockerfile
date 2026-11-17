FROM node:22-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SMART_CS_PROJECT_ROOT=/app HOST=0.0.0.0
COPY requirements.lock.txt requirements.txt pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt
COPY src ./src
COPY start.py alembic.ini ./
COPY migrations ./migrations
COPY docs/business-scope.md ./docs/business-scope.md
COPY scripts/manage_accounts.py ./scripts/manage_accounts.py
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN python -m pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home appuser && mkdir -p data .local vector_store && chown -R appuser:appuser /app
USER appuser
ENV SMART_CS_APP_CONTAINER=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=3)"
CMD ["python", "-m", "smart_cs.delivery", "serve"]
