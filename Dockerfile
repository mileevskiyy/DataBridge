FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd -m operator && mkdir data && chown operator:operator data
COPY backend/ ./backend/
COPY --from=web /web/dist ./frontend/dist/
USER operator
ENV HOST=0.0.0.0 PORT=3300 PUBLIC_DEMO=true PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 3300
CMD ["sh", "-c", "exec uvicorn backend.app:create_app --factory --host ${HOST} --port ${PORT} --workers 1"]
