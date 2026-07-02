# ENR Label Printing Service — container image (v1).
#
# Build CONTEXT is the deploy folder that contains the `labels/` package and
# this Dockerfile (on the NAS: /volume1/docker/label-service/). Build with:
#   docker build -f labels/service/Dockerfile -t label-service .
# or via the Container Manager Project compose (build.context: .,
# build.dockerfile: labels/service/Dockerfile).
#
# The registries (labels/printers.yaml, labels/templates.yaml) ship IN the
# image — a registry edit is a repo edit + image rebuild, matching the
# YAML-in-repo decision. Runtime knobs are env-driven:
#   LABELS_DRY_RUN       -> 1 = assemble+log only, never open a printer socket
#                           (set on first deploy to verify the container
#                           BEFORE it can touch the printer, then set 0)
#   LABELS_PRINTERS_YAML / LABELS_TEMPLATES_YAML -> optional path overrides
#                           if the YAMLs are ever bind-mounted instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first (layer-cached unless requirements change).
COPY labels/service/requirements-service.txt /app/requirements-service.txt
RUN pip install --no-cache-dir -r /app/requirements-service.txt

# The whole labels package: engine + registry + YAML registries + service.
COPY labels/ /app/labels/

# Internal container port (peers on postgres_default reach this app by
# container-name:8000; the host publishes it on 8744 — see the compose).
EXPOSE 8000

# Bind 0.0.0.0 so the published host port reaches the app.
CMD ["uvicorn", "labels.service.app:app", "--host", "0.0.0.0", "--port", "8000"]
