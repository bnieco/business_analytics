#!/bin/bash
# run_etsy_api_load.sh -- launchd wrapper for scripts/etsy_api.py
#
# Handles per-run dated log files (launchd's StandardOutPath cannot expand
# variables, so this wrapper does the redirection instead). The dated run log
# lives in the SynologyDrive Inbox (written at run time = OK on the mount;
# the EX_CONFIG issue only affects launchd stdio opened at job-spawn time, which
# the plist keeps on local APFS).
#
# Invoked daily at 06:15 by ~/Library/LaunchAgents/com.eldersnine.etsy-api-load.plist
# (after the GA4 load at 06:00).
#
# Owner: Edison (Analytics Engineer)

set -u  # unset vars are errors; do NOT set -e (capture all output even on failure)

PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
VENV_PY="${PROJECT_DIR}/venv/bin/python"
LOG_DIR="/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business/Inbox/_logs/etsy_api_load"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/etsy_api_load_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_DIR}" || { echo "FATAL: cannot cd to ${PROJECT_DIR}" >&2; exit 1; }

{
    echo "=== Etsy API load wrapper start: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
    echo "project_dir=${PROJECT_DIR}"
    echo "python=${VENV_PY}"
    echo "args=scripts/etsy_api.py --backfill-days 7"
    echo "---"

    # Step 1: Etsy API ingest -> raw.etsy_* (no exec: we must run step 2 after).
    "${VENV_PY}" scripts/etsy_api.py --backfill-days 7
    INGEST_RC=$?
    echo "--- etsy_api ingest exit code: ${INGEST_RC}"

    # Step 2: raw -> core load. Only if the ingest succeeded, so we never
    # promote a partial/failed pull. A load_core failure is logged loudly,
    # not swallowed.
    if [ "${INGEST_RC}" -eq 0 ]; then
        echo "--- raw->core load start: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
        "${VENV_PY}" scripts/load_core.py
        LOADCORE_RC=$?
        echo "--- load_core exit code: ${LOADCORE_RC}"
        if [ "${LOADCORE_RC}" -ne 0 ]; then
            echo "ERROR: load_core.py FAILED (rc=${LOADCORE_RC}); raw.etsy_* is current but core.* was NOT updated."
        fi
    else
        echo "ERROR: etsy_api ingest FAILED (rc=${INGEST_RC}); SKIPPING raw->core load to avoid promoting a partial pull."
    fi

    echo "=== Etsy API load wrapper end: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
} >> "${LOG_FILE}" 2>&1
