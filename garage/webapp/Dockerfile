# ENR Garage Inventory — container image (deploy is a LATER operator step;
# built/tested locally only for now).
#
# Build CONTEXT is the deploy folder that contains the `garage/` package and
# this Dockerfile (on the NAS: /volume1/docker/garage-inventory/). Build with:
#   docker build -f garage/webapp/Dockerfile -t garage-inventory .
# or via the Container Manager Project compose (build.context: .,
# build.dockerfile: garage/webapp/Dockerfile).
#
# Runtime config is ALL environment-driven (no secrets baked in):
#   BUSINESS_DB_HOST  -> postgres16              (peer container)
#   BUSINESS_DB_PORT  -> 5432                     (internal port)
#   BUSINESS_DB_NAME / BUSINESS_DB_USER / BUSINESS_DB_PASSWORD
#   LABEL_SERVICE_URL -> http://label-service:8000 (peer container, when live)
#   GARAGE_TEMPLATE   -> registered garage-bin template name
#   GARAGE_QR_BASE    -> the Tailscale-HTTPS origin the QR codes must encode
# See garage/db.py and garage/webapp/labels.py for resolution + defaults.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first (layer-cached unless requirements change).
COPY garage/webapp/requirements-app.txt /app/requirements-app.txt
RUN pip install --no-cache-dir -r /app/requirements-app.txt

# The whole garage package: db helper + webapp + static pages.
COPY garage/ /app/garage/

# Internal container port (the host publishes it on 8745 — see the compose).
EXPOSE 8000

# Bind 0.0.0.0 so the published host port reaches the app.
CMD ["uvicorn", "garage.webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
