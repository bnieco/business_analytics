#!/bin/bash
# check_ga4_freshness.sh -- active staleness alert for the GA4 warehouse.
#
# Independent of the GA4 load cron (com.eldersnine.ga4-load) on purpose: if that
# job dies silently, THIS job still runs and surfaces the gap. Reads the
# ops.ga4_freshness view and, when days_behind >= THRESHOLD, raises a signal via
# two cheap, reversible channels:
#   1. appends a line to a prominent LOCAL alert log (~/Library/Logs/ga4-alerts.log)
#   2. posts a macOS Notification Center banner
#
# To change the channel: edit the `raise_alert` function below. (e.g. swap the
# osascript notification for a `mail`/SMTP call, or a webhook curl.)
#
# CADENCE & THRESHOLD LOGIC (revised 2026-06-04, Task #65)
# --------------------------------------------------------
# Scheduled ONCE daily at 17:00 (com.eldersnine.ga4-freshness-check.plist), one
# hour after the 16:00 GA4 afternoon load. THRESHOLD stays at 2.
#
# Why 17:00 and one check, not 07:00:
#   * GA4 BigQuery exports yesterday (D-1) during the day, so a 06:00 load can
#     only ever reach D-2 — at 07:00 the warehouse is at its D-2 morning steady
#     state and a days_behind>=2 check would fire EVERY morning (cry-wolf).
#   * The 16:00 load lands D-1, so post-16:00 a HEALTHY warehouse is
#     days_behind<=1. That makes days_behind>=2 a meaningful "real stall" signal
#     again: it can only happen if the load cron actually failed to advance.
#   * A single 17:00 check meets the bar (catches a true stall within ~a day,
#     zero false alarms on a healthy day). A second morning check would only
#     surface a stall a day later than 17:00 already does, while re-introducing a
#     cry-wolf surface — so it was deliberately dropped. The live dashboard
#     freshness badge already covers intra-day "is it current right now?".
#
# RETRY ROBUSTNESS (revised 2026-06-04, Task #65)
# -----------------------------------------------
# launchd can wake the machine to run this job and hit the LAN NAS (10.0.0.16)
# before Wi-Fi re-associates -> transient "No route to host" even though the host
# is up (observed 06/02 & 06/04 at ~07:00). The old 5 tries at 5/10/15/20s burned
# out in ~50s — too tight. Now 6 attempts spaced 90s (~7.5 min total) ride out a
# multi-minute network blip, and each attempt is logged with a timestamp so
# future flakiness is diagnosable from ~/Library/Logs/ga4-alerts.log.
#
# Owner: Edison (Analytics Engineer). Created 2026-06-02; revised 2026-06-04.

set -u

PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
ENV_FILE="${PROJECT_DIR}/.env"
PSQL="/opt/homebrew/opt/libpq/bin/psql"
ALERT_LOG="${HOME}/Library/Logs/ga4-alerts.log"   # LOCAL volume on purpose
THRESHOLD=2                                        # alert when days_behind >= 2

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

raise_alert() {
    local behind="$1" latest="$2"
    echo "[$(ts)] ALERT: GA4 warehouse stale — days_behind=${behind}, latest_event_date=${latest} (threshold=${THRESHOLD})" >> "${ALERT_LOG}"
    /usr/bin/osascript -e "display notification \"days_behind=${behind}, latest=${latest}. Check com.eldersnine.ga4-load.\" with title \"GA4 warehouse STALE\" sound name \"Basso\"" 2>/dev/null || true
}

raise_error() {
    local msg="$1"
    echo "[$(ts)] ERROR: freshness check could not run — ${msg}" >> "${ALERT_LOG}"
    /usr/bin/osascript -e "display notification \"${msg}\" with title \"GA4 freshness check FAILED\" sound name \"Basso\"" 2>/dev/null || true
}

# Load DB creds
if [[ ! -f "${ENV_FILE}" ]]; then raise_error "missing .env at ${ENV_FILE}"; exit 1; fi
set -a; # shellcheck disable=SC1090
source "${ENV_FILE}"; set +a

export PGPASSWORD="${BUSINESS_DB_PASSWORD:-}"

# Retry with backoff. launchd background jobs can transiently get
# "No route to host" to the LAN NAS (10.0.0.16) right at spawn time even when
# the host is up — observed 2026-06-02 & 2026-06-04. We only escalate to a real
# ERROR alert after the DB stays unreachable across ALL attempts, to avoid false
# pages. 6 attempts spaced 90s (~7.5 min total) to ride out a multi-minute blip.
MAX_ATTEMPTS=6
RETRY_SLEEP=90   # seconds between attempts
ROW=""
RC=1
PG_ERR=""
for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
    ROW="$("${PSQL}" -h "${BUSINESS_DB_HOST}" -p "${BUSINESS_DB_PORT}" -d "${BUSINESS_DB_NAME}" \
            -U "${BUSINESS_DB_USER}" -w -At -F '|' -c \
            "SELECT days_behind, latest_event_date FROM ops.ga4_freshness;" 2>/tmp/ga4_fresh_psql.err)"
    RC=$?
    if [[ ${RC} -eq 0 && -n "${ROW}" ]]; then
        if (( attempt > 1 )); then
            echo "[$(ts)] INFO: ops.ga4_freshness reachable on attempt ${attempt}/${MAX_ATTEMPTS} (earlier attempts hit a transient blip)" >> "${ALERT_LOG}"
        fi
        break
    fi
    PG_ERR="$(tr '\n' ' ' < /tmp/ga4_fresh_psql.err)"
    echo "[$(ts)] RETRY: attempt ${attempt}/${MAX_ATTEMPTS} failed (rc=${RC}): ${PG_ERR}" >> "${ALERT_LOG}"
    # Don't sleep after the final attempt.
    if (( attempt < MAX_ATTEMPTS )); then sleep "${RETRY_SLEEP}"; fi
done
rm -f /tmp/ga4_fresh_psql.err
unset PGPASSWORD

if [[ ${RC} -ne 0 || -z "${ROW}" ]]; then
    raise_error "ops.ga4_freshness unreachable after ${MAX_ATTEMPTS} attempts (rc=${RC}): ${PG_ERR}"
    exit 1
fi

BEHIND="${ROW%%|*}"
LATEST="${ROW##*|}"

if [[ "${BEHIND}" =~ ^[0-9]+$ ]] && (( BEHIND >= THRESHOLD )); then
    raise_alert "${BEHIND}" "${LATEST}"
    exit 0
fi

# Healthy — leave a quiet heartbeat so "is the checker itself alive?" is answerable.
echo "[$(ts)] OK: days_behind=${BEHIND}, latest_event_date=${LATEST}" >> "${ALERT_LOG}"
exit 0
