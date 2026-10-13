#!/bin/bash
# check_ga4_freshness.sh -- active staleness alert for the GA4 warehouse.
#
# Independent of the GA4 load cron (com.eldersnine.ga4-load) on purpose: if that
# job dies silently, THIS job still runs and surfaces the gap. Reads the
# ops.ga4_freshness view and, when days_behind >= THRESHOLD, raises a signal via
# three cheap, reversible channels:
#   1. appends a line to a prominent LOCAL alert log (~/Library/Logs/ga4-alerts.log)
#   2. posts a macOS Notification Center banner
#   3. files ONE `ASK - YYYY-MM-DD - ga4-freshness-<stale|error>.md` into the Business
#      vault Inbox (AI Teams/Business/Inbox/) so the warning reaches the cc-business
#      session even when the job runs on an unwatched host (the M6 Mac mini). A repeat
#      on the same day appends a dated line under a `## Repeat` heading; the original
#      request text is never rewritten. Filing is best-effort: any failure is logged
#      to the alert log as a WARN and the script carries on with its normal exit code.
#
# To change the channel: edit the `raise_alert` / `raise_error` functions below (e.g.
# swap the osascript notification for a `mail`/SMTP call, or a webhook curl).
#
# DRY-RUN MODE (touches nothing in production; added 2026-09-24 for the M6 move)
# ------------------------------------------------------------------------------
#   GA4_FRESH_DRY_RUN=1      skip .env (no secrets read) and the psql loop; use the
#                            simulated values below and print the banner text instead
#                            of posting a Notification Center banner.
#   GA4_FRESH_SIM_BEHIND=N   simulated days_behind (default 3, i.e. above THRESHOLD).
#   GA4_FRESH_SIM_LATEST=D   simulated latest_event_date (default: yesterday).
#   GA4_FRESH_SIM_ERROR=1    exercise the raise_error path instead (exit 1).
#   ENR_ALERT_LOG=PATH       dry-run only: where the alert log lines go
#                            (default ~/Library/Logs/ga4-alerts.log).
#   ENR_ASK_INBOX_DIR=DIR    where the ASK file is written (default: the real vault
#                            Inbox). Honoured in every mode; point it at scratch to test.
# Example:
#   GA4_FRESH_DRY_RUN=1 ENR_ALERT_LOG=/tmp/x/alerts.log ENR_ASK_INBOX_DIR=/tmp/x/inbox \
#     bash scripts/cron/check_ga4_freshness.sh
#
# CADENCE & THRESHOLD LOGIC (revised 2026-06-04, Task #65; THRESHOLD 2 -> 3 on 2026-10-13)
# --------------------------------------------------------
# Scheduled ONCE daily at 17:00 (com.eldersnine.ga4-freshness-check.plist), one
# hour after the 16:00 GA4 afternoon load. THRESHOLD is 3 since 2026-10-13
# (it was 2 from 2026-06-04 to 2026-10-13; see the dated note below).
#
# 2026-10-13 THRESHOLD 2 -> 3 (Brian, D-BUSINESS-005 option A):
#   Google's GA4 daily export (D-1) now publishes late in the evening instead of
#   around 08:00 the next morning. Observed publish times (MST):
#     events for 10-04 published 10-05 08:39
#     events for 10-05 published 10-06 10:08
#     events for 10-06 published 10-07 19:16
#     events for 10-07 published 10-08 22:39
#   So at 17:00 a HEALTHY warehouse now sits at days_behind=2 (D-1 has not been
#   exported yet), and THRESHOLD=2 filed a false "stale" ASK every day from 10-07.
#   Trade-off: with THRESHOLD=3 a real load stall surfaces about one day later
#   than before. The 2026-06-04 reasoning below still holds for the cadence; its
#   "post-16:00 a healthy warehouse is days_behind<=1" assumption no longer does.
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
# Owner: Edison (Analytics Engineer). Created 2026-06-02; revised 2026-06-04,
# 2026-09-24 (Inbox ASK filing + dry-run mode, for the M6 move).

set -u

PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
ENV_FILE="${PROJECT_DIR}/.env"
PSQL="/opt/homebrew/opt/libpq/bin/psql"
THRESHOLD=3                                        # alert when days_behind >= 3

DRY_RUN="${GA4_FRESH_DRY_RUN:-0}"
if [[ "${DRY_RUN}" == "1" ]]; then
    # Dry run only: allow the alert log to be pointed at scratch.
    ALERT_LOG="${ENR_ALERT_LOG:-${HOME}/Library/Logs/ga4-alerts.log}"
else
    ALERT_LOG="${HOME}/Library/Logs/ga4-alerts.log"   # LOCAL volume on purpose
fi

# Business vault Inbox for the automated ASK. Env override exists so tests can point
# it at a scratch dir; with nothing set this is the real Inbox.
ASK_INBOX_DIR="${ENR_ASK_INBOX_DIR:-/Users/bnieco/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault/AI Teams/Business/Inbox}"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

# Notification Center banner; in dry-run mode just echo it so nothing fires.
notify() {
    local title="$1" body="$2"
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "[dry-run] notification suppressed: title=\"${title}\" body=\"${body}\""
        return 0
    fi
    /usr/bin/osascript -e "display notification \"${body}\" with title \"${title}\" sound name \"Basso\"" 2>/dev/null || true
}

# file_ask <slug> <heading> <summary> <detail-lines...>
# Best-effort: writes ONE ASK per day per slug into ASK_INBOX_DIR (temp file + mv, so
# the Inbox never sees a half-written file). If today's file already exists, appends a
# dated line under "## Repeat" instead of touching the original text. Never fails the
# caller: on any error it logs a WARN to ALERT_LOG and returns 0.
file_ask() {
    local slug="$1" heading="$2" summary="$3"
    shift 3
    local today now host target tmp err
    today="$(date +%Y-%m-%d)"
    now="$(ts)"
    host="$(hostname 2>/dev/null || echo unknown-host)"
    target="${ASK_INBOX_DIR}/ASK - ${today} - ${slug}.md"

    if ! err="$(mkdir -p "${ASK_INBOX_DIR}" 2>&1)"; then
        echo "[${now}] WARN: could not file ASK: mkdir ${ASK_INBOX_DIR} failed: ${err}" >> "${ALERT_LOG}"
        return 0
    fi

    if [[ -e "${target}" ]]; then
        # Same-day repeat: append only. Add the heading once, then one dated line per run.
        {
            if ! grep -q '^## Repeat$' "${target}" 2>/dev/null; then
                printf '\n## Repeat\n'
            fi
            printf -- '- %s on %s: %s\n' "${now}" "${host}" "${summary}"
        } >> "${target}" 2>/dev/null \
            || echo "[${now}] WARN: could not file ASK: append to ${target} failed" >> "${ALERT_LOG}"
        return 0
    fi

    tmp="${ASK_INBOX_DIR}/.ASK-${slug}-$$.tmp"
    {
        printf -- '---\n'
        printf 'type: ask\n'
        printf 'date: %s\n' "${today}"
        printf 'from: Edison (GA4 freshness check, automated)\n'
        printf 'to: Carson (Business)\n'
        printf 'status: open\n'
        printf 'priority: high\n'
        printf 'pickup: now\n'
        printf -- '---\n'
        printf '# %s\n\n' "${heading}"
        printf 'Filed automatically by scripts/cron/check_ga4_freshness.sh (com.eldersnine.ga4-freshness-check). No human wrote this.\n\n'
        printf '%s\n\n' "${summary}"
        printf '## Details\n\n'
        local line
        for line in "$@"; do printf -- '- %s\n' "${line}"; done
        printf -- '- host: %s\n' "${host}"
        printf -- '- timestamp: %s\n' "${now}"
        printf -- '- alert log: %s\n' "${ALERT_LOG}"
        printf '\n## Check to run (on the host named above)\n\n'
        printf '```bash\n'
        printf 'launchctl print gui/$(id -u)/com.eldersnine.ga4-load\n'
        printf 'tail -n 40 ~/Library/Logs/ga4-alerts.log\n'
        printf '```\n\n'
        printf 'If the load agent is healthy and the warehouse has since caught up, mark this ask done. Otherwise raise it to Brian.\n'
    } > "${tmp}" 2>/dev/null && mv -f "${tmp}" "${target}" 2>/dev/null \
        || { rm -f "${tmp}" 2>/dev/null; echo "[${now}] WARN: could not file ASK: write/mv to ${target} failed" >> "${ALERT_LOG}"; }
    return 0
}

raise_alert() {
    local behind="$1" latest="$2"
    echo "[$(ts)] ALERT: GA4 warehouse stale — days_behind=${behind}, latest_event_date=${latest} (threshold=${THRESHOLD})" >> "${ALERT_LOG}"
    notify "GA4 warehouse STALE" "days_behind=${behind}, latest=${latest}. Check com.eldersnine.ga4-load."
    file_ask "ga4-freshness-stale" "GA4 warehouse stale" \
        "The GA4 warehouse is ${behind} days behind (threshold ${THRESHOLD}); latest_event_date is ${latest}. The com.eldersnine.ga4-load agent has probably stopped advancing." \
        "days_behind: ${behind}" \
        "latest_event_date: ${latest}" \
        "threshold: ${THRESHOLD}"
}

raise_error() {
    local msg="$1"
    echo "[$(ts)] ERROR: freshness check could not run — ${msg}" >> "${ALERT_LOG}"
    notify "GA4 freshness check FAILED" "${msg}"
    file_ask "ga4-freshness-error" "GA4 freshness check failed" \
        "The freshness check itself could not run, so the warehouse state is unknown: ${msg}" \
        "days_behind: unknown (check did not complete)" \
        "latest_event_date: unknown (check did not complete)" \
        "threshold: ${THRESHOLD}"
}

if [[ "${DRY_RUN}" == "1" ]]; then
    # Simulated path: no .env, no psql, no production side effects beyond the
    # (redirectable) alert log and ASK inbox.
    echo "[dry-run] GA4_FRESH_DRY_RUN=1: skipping .env and psql; alert_log=${ALERT_LOG} ask_inbox=${ASK_INBOX_DIR}"
    if [[ "${GA4_FRESH_SIM_ERROR:-0}" == "1" ]]; then
        raise_error "simulated failure (GA4_FRESH_SIM_ERROR=1, dry run)"
        exit 1
    fi
    BEHIND="${GA4_FRESH_SIM_BEHIND:-3}"
    LATEST="${GA4_FRESH_SIM_LATEST:-$(date -v-1d +%Y-%m-%d)}"
    if [[ "${BEHIND}" =~ ^[0-9]+$ ]] && (( BEHIND >= THRESHOLD )); then
        raise_alert "${BEHIND}" "${LATEST}"
        exit 0
    fi
    echo "[$(ts)] OK: days_behind=${BEHIND}, latest_event_date=${LATEST} (dry run)" >> "${ALERT_LOG}"
    exit 0
fi

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
