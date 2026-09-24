#!/bin/bash
# run_vault_production_ask.sh -- launchd wrapper for scripts/vault_production_ask.py
#
# Files ONE `ASK - *.md` per new solid-wood dice vault order into the vault Inbox
# (AI Teams/Business/Inbox/) proposing the mill/sand/finish production block. A human
# reads it, confirms the species and the Etsy ship-by, and creates the calendar events.
#
# THE JOB NEVER WRITES TO THE CALENDAR, NEVER CALLS THE ETSY API, AND NEVER CONTACTS A
# CUSTOMER. One markdown file is its entire outward surface. That boundary is Brian's
# explicit choice (2026-09-14), not an implementation detail -- do not widen it here.
#
# Handles per-run dated log files (launchd's StandardOutPath cannot expand variables, so
# the wrapper does the redirection instead). The dated run log lives in the SynologyDrive
# Inbox, which is fine at RUN time; the EX_CONFIG problem only affects launchd stdio
# opened at job-spawn time, which the plist keeps on local APFS.
#
# Invoked daily at 06:45 by ~/Library/LaunchAgents/com.eldersnine.vault-production-ask.plist
# -- 30 minutes after the 06:15 Etsy ingest + raw->core load, so core.orders already holds
# this morning's receipts.
#
# Owner: Edison (Analytics Engineer)

set -u  # unset vars are errors; do NOT set -e (capture all output even on failure)

PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
VENV_PY="${PROJECT_DIR}/venv/bin/python"
LOG_DIR="/Users/bnieco/Library/CloudStorage/SynologyDrive-HomeSync/Documents/Business/Inbox/_logs/vault_production_ask"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/vault_production_ask_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_DIR}" || { echo "FATAL: cannot cd to ${PROJECT_DIR}" >&2; exit 1; }

{
    echo "=== Vault production ASK wrapper start: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
    echo "project_dir=${PROJECT_DIR}"
    echo "python=${VENV_PY}"
    echo "args=scripts/vault_production_ask.py"
    echo "---"

    "${VENV_PY}" scripts/vault_production_ask.py
    RC=$?
    echo "--- vault_production_ask exit code: ${RC}"
    if [ "${RC}" -ne 0 ]; then
        echo "ERROR: vault_production_ask.py FAILED (rc=${RC}). No ASK may have been filed."
        echo "       The ledger is only written after a file is verified on disk, so a"
        echo "       failed run is safe to retry: the next run re-files what it missed."
    fi

    echo "=== Vault production ASK wrapper end: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
} >> "${LOG_FILE}" 2>&1
