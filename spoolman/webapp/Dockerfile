# ENR Filament Intake — Stage C container image.
#
# Build CONTEXT is the deploy folder that contains the `spoolman/` package and
# this Dockerfile (on the NAS: /volume1/docker/filament-intake/). Build with:
#   docker build -f spoolman/webapp/Dockerfile -t filament-intake .
# or via the Container Manager Project compose (build.context: ., build.dockerfile:
# spoolman/webapp/Dockerfile).
#
# Runtime config is ALL environment-driven (no secrets baked in):
#   SPOOLMAN_API_URL  -> http://spoolman:8000   (peer container, internal port)
#   BUSINESS_DB_HOST  -> postgres16             (peer container)
#   BUSINESS_DB_PORT  -> 5432                    (internal port)
#   BUSINESS_DB_NAME / BUSINESS_DB_USER / BUSINESS_DB_PASSWORD
# See connector.py for resolution + defaults.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first (layer-cached unless requirements change).
COPY spoolman/webapp/requirements-app.txt /app/requirements-app.txt
RUN pip install --no-cache-dir -r /app/requirements-app.txt

# The shared label engine (Label Service Phase S): ptouch.py imports
# `labels.engine` in-process, so the package must ship in this image too.
# NOTE for the NAS deploy folder: copy BOTH spoolman/ AND labels/ into
# /volume1/docker/filament-intake/ before the next rebuild.
COPY labels/ /app/labels/

# The whole spoolman package: connector (Stage A) + webapp + static page (Stage B).
COPY spoolman/ /app/spoolman/

# Internal container port (peers reach this app by container-name:8000 if needed;
# the host publishes it on a chosen host port — see the compose / runbook).
EXPOSE 8000

# Bind 0.0.0.0 so the published host port reaches the app.
CMD ["uvicorn", "spoolman.webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
