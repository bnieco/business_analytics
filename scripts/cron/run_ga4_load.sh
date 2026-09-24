#!/bin/bash
# run_ga4_load.sh -- launchd wrapper for scripts/load_ga4.py
#
# Handles per-run dated log files (launchd's StandardOutPath cannot
# expand variables, so this wrapper does the redirection instead).
# Invoked daily at 06:00 by ~/Library/LaunchAgents/com.eldersnine.ga4-load.plist.
#
# Owner: Edison (Analytics Engineer)

set -u  # treat unset vars as errors; do NOT set -e (we want to capture all output even on failure)

PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
VENV_PY="${PROJECT_DIR}/venv/bin/python"
# ENR_LOG_ROOT: optional override for the log root. Unset (MacBook) = the SynologyDrive
# Inbox/_logs path used today. The M6 (Mac mini) plists set it to /Users/bnieco/Library/Logs/enr
# so the job does not depend on the Synology Drive client being up.
LOG_DIR="${ENR_LOG_ROOT:-/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business/Inbox/_logs}/ga4_load"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/load_ga4_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_DIR}" || { echo "FATAL: cannot cd to ${PROJECT_DIR}" >&2; exit 1; }

{
    echo "=== GA4 load wrapper start: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
    echo "project_dir=${PROJECT_DIR}"
    echo "python=${VENV_PY}"
    echo "args=scripts/load_ga4.py --backfill-days 3"
    echo "---"
    exec "${VENV_PY}" scripts/load_ga4.py --backfill-days 3
} >> "${LOG_FILE}" 2>&1
