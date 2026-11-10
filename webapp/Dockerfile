FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY webapp/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY webapp/frontend/ ./
RUN npm run build

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-gdal python3-numpy ca-certificates libgdal32 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH" HOST=0.0.0.0 PYTHONUNBUFFERED=1 HAZARD_DATA_DIR=/data
WORKDIR /app
COPY webapp/requirements.txt /app/webapp/requirements.txt
RUN pip install --no-cache-dir -r webapp/requirements.txt
COPY avalanche_scenario /app/avalanche_scenario
COPY landslide_scenario /app/landslide_scenario
COPY wildfire_scenario /app/wildfire_scenario
COPY webapp/backend /app/webapp/backend
COPY webapp/__init__.py webapp/run.py /app/webapp/
COPY --from=frontend /build/dist /app/webapp/frontend/dist
RUN useradd -m -u 10001 georisk && mkdir -p /data && chown georisk:georisk /data
USER georisk
EXPOSE 8000
CMD ["python", "webapp/run.py"]
