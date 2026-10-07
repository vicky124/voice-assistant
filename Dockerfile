# Stage 1: build the React client
FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: Python server that also serves the built client
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/server
COPY server/pyproject.toml ./
COPY server/voice ./voice
RUN pip install --no-cache-dir ".[aws]"
COPY --from=web /web/dist /app/web/dist
RUN useradd --uid 10001 --create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "voice.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
