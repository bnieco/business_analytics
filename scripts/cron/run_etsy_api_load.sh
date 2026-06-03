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
    exec "${VENV_PY}" scripts/etsy_api.py --backfill-days 7
} >> "${LOG_FILE}" 2>&1
