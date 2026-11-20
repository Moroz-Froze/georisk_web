FROM debian:trixie-slim

# Python/GDAL/NumPy are installed together to keep their native ABI compatible.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-venv python3-gdal python3-numpy ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 PORT=8000 HAZARD_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY webapp/requirements.txt /app/webapp/requirements.txt
RUN pip install --no-cache-dir -r webapp/requirements.txt \
    && python -c "import fastapi,uvicorn,numpy; from osgeo import gdal; print(gdal.VersionInfo())" \
    && pip check
COPY avalanche_scenario /app/avalanche_scenario
COPY landslide_scenario /app/landslide_scenario
COPY wildfire_scenario /app/wildfire_scenario
COPY webapp/backend /app/webapp/backend
COPY webapp/__init__.py webapp/run.py /app/webapp/
COPY webapp/frontend/dist /app/webapp/frontend/dist
COPY licenses /app/licenses
RUN useradd --no-log-init --create-home --uid 10001 georisk \
    && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3); assert json.load(r)['status']=='ok'"
CMD ["python", "-B", "webapp/run.py"]
