"""Phase 1 Reactivation Pilot dashboard — browser-openable progress page.

CANONICAL source of truth for Phase 1 reporting (2026-06-01 consolidation,
Edison). The CLI scripts/phase1_progress.py and the psql .sql files mirror
this file's query logic; this file is authoritative.

Stdlib http.server + SQLAlchemy + pandas, inline HTML. Panels:
    Q1  Headline scoreboard (criterion-4 500-visit floor)
    Q2  By platform (collected_traffic_source_source)
    Q3  By post (utm_source + utm_content, top 25)
    Q4  By landing page (platform + page_location, top 25)   [added 2026-06-01]
    Q5  Per-link performance — rich two-arm (etsy/own_site) attribution
    Q6  Per-link summary chips, with etsy/own_site split      [enriched 2026-06-01]
    Q7  Etsy "Social Media" YoY
    R1  Etsy reviews — avg+count headline, latest-3, per-listing [added 2026-06-04, Task #64]
    R2  Listing attention — views/favorers + day-over-day delta  [added 2026-06-04, Task #64]

Reviews/attention panels read ops.review_summary / ops.review_recent /
ops.listing_attention (migration 008) — never raw.* directly — and degrade to
an empty panel if those views are absent (query_* wrapped in _query_safe).

ATTRIBUTION (round 4, 2026-05-27): every Phase 1 filter anchors on the
canonical campaign tag collected_traffic_source_campaign = 'phase1_reactivation'
(utm.py stamps it on every link). utm_source/campaign come from GA4's
collected_traffic_source_* columns; utm_content comes from event_params
(no collected_traffic_source_content column exists in raw.ga4_events).
The Etsy attribution branch additionally carries a listing-id page-location
guard so a /listing/N link only attributes visits that landed on /listing/N.

FLAG-A FIX (2026-06-01): utm_content extraction is now a SINGLE shared
expression (_UTM_CONTENT_EXPR), anchored on the known-good nested form
ep->'value'->>'string_value' first, used identically in both attribution
arms — they can no longer drift in COALESCE precedence.

Usage:
    Set env vars (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD — the
    BUSINESS_DB_* namespace is also accepted as a fallback) and run with
    --serve. The Dockerfile / compose stack handles that on the NAS.
"""

from __future__ import annotations

import html as html_mod
import math
import os
import sys
from datetime import date
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    """Walk up from script dir to find .env. No-op in Docker (env_file handles it)."""
    search = PROJECT_ROOT
    while True:
        env_path = search / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
            return
        parent = search.parent
        if parent == search:
            return
        search = parent


_load_env()


def _db_env(key: str, default: str | None = None) -> str:
    """Read a DB setting from the DB_* namespace (container) with a
    BUSINESS_DB_* fallback (dev CLI / db.py). Lets the one canonical file
    run in both environments without touching the NAS .env."""
    val = os.environ.get(f"DB_{key}") or os.environ.get(f"BUSINESS_DB_{key}")
    if val is None:
        if default is not None:
            return default
        raise KeyError(f"DB_{key} (or BUSINESS_DB_{key}) not set")
    return val


DB_URI = (
    f"postgresql+psycopg2://{_db_env('USER')}:{_db_env('PASSWORD')}"
    f"@{_db_env('HOST')}:{_db_env('PORT')}/{_db_env('NAME')}"
)
PORT = int(os.environ.get("PORT", "8788"))

# Phase 1 window — single source of truth. The CLI (scripts/phase1_progress.py)
# and the psql .sql files duplicate these and carry a sync comment pointing here.
PHASE1_START = date(2026, 5, 24)
PHASE1_END = date(2026, 8, 22)
PHASE1_TARGET_VISITS = 500
PHASE1_DAYS = 90
# Canonical Phase 1 campaign tag — utm.py stamps this on every generated link.
PHASE1_CAMPAIGN_TAG = "phase1_reactivation"

# ---------------------------------------------------------------------------
# SQL — this file is canonical; the CLI + .sql files mirror it.
# All Phase 1 filters bind :campaign_tag (no hardcoded campaign string).
# ---------------------------------------------------------------------------

# Flag-A fix: the ONE shared utm_content extraction expression, anchored on
# the known-good nested form first. Both attribution arms reference this same
# string so their COALESCE precedence can never drift again.
_UTM_CONTENT_EXPR = """COALESCE(
            (SELECT ep -> 'value' ->> 'string_value'
             FROM jsonb_array_elements(e.event_params) ep
             WHERE ep ->> 'key' = 'content'
             LIMIT 1),
            (SELECT ep ->> 'value'
             FROM jsonb_array_elements(e.event_params) ep
             WHERE ep ->> 'key' = 'content'
             LIMIT 1)
        )"""

HEADLINE_SQL = """
WITH visits AS (
    SELECT COUNT(*) AS phase1_visits
    FROM raw.ga4_events
    WHERE event_date BETWEEN :start AND :end
      AND event_name = 'page_view'
      AND collected_traffic_source_campaign = :campaign_tag
)
SELECT
    DATE :start                                                  AS phase1_start,
    DATE :end                                                    AS phase1_end,
    CURRENT_DATE                                                 AS as_of,
    GREATEST(0, (DATE :end - CURRENT_DATE))                      AS days_remaining,
    LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1             AS days_elapsed,
    v.phase1_visits                                              AS visits_to_date,
    :target                                                      AS visits_floor,
    ROUND(100.0 * v.phase1_visits / :target, 1)                  AS pct_of_floor,
    CASE
        WHEN (LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1) > 0
        THEN ROUND(
            v.phase1_visits::numeric
            * 90.0
            / (LEAST(CURRENT_DATE, DATE :end) - DATE :start + 1)
        )
        ELSE NULL
    END                                                          AS pace_projection_90d
FROM visits v
"""

BY_PLATFORM_SQL = """
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS utm_source,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    COUNT(DISTINCT ga_session_id)                                AS sessions,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN :start AND :end
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :campaign_tag
GROUP BY 1
ORDER BY visits DESC
"""

BY_POST_SQL = """
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS utm_source,
    COALESCE(
        (SELECT ep -> 'value' ->> 'string_value'
         FROM jsonb_array_elements(event_params) ep
         WHERE ep ->> 'key' = 'content'
         LIMIT 1),
        '(no utm_content)'
    )                                                            AS utm_content,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN :start AND :end
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :campaign_tag
GROUP BY 1, 2
ORDER BY visits DESC
LIMIT 25
"""

# Q4 — by landing page (platform + page_location, query string stripped).
# Ported from the dev CLI 2026-06-01; complements Q3 (destination URL vs post
# tag). Same campaign-tag filter as everything else.
BY_LANDING_SQL = """
SELECT
    COALESCE(collected_traffic_source_source, '(unset)')         AS platform,
    regexp_replace(page_location, '\\?.*$', '')                  AS landing_page,
    COUNT(*)                                                     AS visits,
    COUNT(DISTINCT user_pseudo_id)                               AS unique_visitors,
    MIN(event_date)                                              AS first_seen,
    MAX(event_date)                                              AS last_seen
FROM raw.ga4_events
WHERE event_date BETWEEN :start AND :end
  AND event_name = 'page_view'
  AND collected_traffic_source_campaign = :campaign_tag
GROUP BY 1, 2
ORDER BY visits DESC
LIMIT 25
"""

# Q5 — rich two-arm per-link performance (Round-4 fix preserved; Flag-A fixed).
# etsy + own_site branches share the SAME utm_content extraction (_UTM_CONTENT_EXPR)
# so they cannot drift. The etsy branch carries the listing-id page-location
# guard. destination_kind / attribution_mode columns carried across from the
# dev SQL so the renderer can show the etsy/own_site split.
LINK_PERFORMANCE_SQL = f"""
WITH
links AS (
    SELECT
        l.*,
        CASE
            WHEN l.base_url ~* '(^|//|\\.)etsy\\.com(/|\\?|$)' THEN 'etsy'
            ELSE 'own_site'
        END AS destination_kind
    FROM raw.utm_links l
    WHERE l.utm_campaign = :campaign_tag
),
etsy_attribution AS (
    SELECT
        l.id AS link_id,
        COUNT(*)                         AS visits,
        COUNT(DISTINCT e.user_pseudo_id) AS unique_visitors,
        MIN(e.event_date)                AS first_visit,
        MAX(e.event_date)                AS last_visit
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'etsy'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN :start AND :end
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND (
         l.base_url !~ '/listing/[0-9]+'
         OR e.page_location ~ ('/listing/' || substring(l.base_url FROM '/listing/([0-9]+)'))
     )
     AND {_UTM_CONTENT_EXPR} IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
own_site_attribution AS (
    SELECT
        l.id AS link_id,
        COUNT(*)                         AS visits,
        COUNT(DISTINCT e.user_pseudo_id) AS unique_visitors,
        MIN(e.event_date)                AS first_visit,
        MAX(e.event_date)                AS last_visit
    FROM links l
    JOIN raw.ga4_events e
      ON l.destination_kind = 'own_site'
     AND e.event_name = 'page_view'
     AND e.event_date BETWEEN :start AND :end
     AND e.collected_traffic_source_source   IS NOT DISTINCT FROM l.utm_source
     AND e.collected_traffic_source_campaign IS NOT DISTINCT FROM l.utm_campaign
     AND {_UTM_CONTENT_EXPR} IS NOT DISTINCT FROM l.utm_content
    GROUP BY l.id
),
attribution AS (
    SELECT * FROM etsy_attribution
    UNION ALL
    SELECT * FROM own_site_attribution
)
SELECT
    l.destination_kind,
    l.utm_source,
    l.utm_content,
    l.base_url,
    l.created_at::date              AS link_created,
    l.generated_count,
    COALESCE(a.visits, 0)           AS visits,
    COALESCE(a.unique_visitors, 0)  AS unique_visitors,
    a.first_visit,
    a.last_visit,
    'per_post'                      AS attribution_mode
FROM links l
LEFT JOIN attribution a ON a.link_id = l.id
ORDER BY visits DESC, l.created_at DESC
"""

ETSY_YOY_SQL = """
SELECT
    year,
    sub_source,
    visits,
    date_range_start,
    date_range_end
FROM raw.etsy_traffic_stats
WHERE category = 'Social Media'
ORDER BY year DESC, visits DESC NULLS LAST, sub_source NULLS FIRST
"""

# Flag B — GA4 load freshness. Prefer the ops.ga4_freshness view (created
# 2026-06-01); fall back to a direct MAX(event_date) if the view isn't present
# (e.g. running against a DB where it hasn't been deployed). days_behind > 1
# means the daily ga4-load cron likely stalled — the badge warns on the page
# Brian already views so a dead cron can't hide.
FRESHNESS_VIEW_SQL = """
SELECT latest_event_date, last_loaded_at, days_behind
FROM ops.ga4_freshness
"""

FRESHNESS_FALLBACK_SQL = """
SELECT
    MAX(event_date)                AS latest_event_date,
    MAX(loaded_at)                 AS last_loaded_at,
    CURRENT_DATE - MAX(event_date) AS days_behind
FROM raw.ga4_events
"""

# ---------------------------------------------------------------------------
# Reviews + listing-attention (Task #64). Both read ops.* views created in
# migration 008 (ops.review_summary / ops.review_recent / ops.listing_attention)
# so the dashboard never touches raw.* directly and agents share the same
# derived, reviewer-safe projection. Each query is wrapped (see query_* below)
# so a missing view degrades gracefully to an empty panel rather than a 500.
# ---------------------------------------------------------------------------

# Overall headline: totals across every review (legacy CSV + API era).
REVIEW_HEADLINE_SQL = """
SELECT
    SUM(review_count)                                               AS total_reviews,
    ROUND(SUM(review_count * avg_rating) / NULLIF(SUM(review_count), 0), 2)
                                                                    AS overall_avg
FROM ops.review_summary
"""

# Per-listing ratings, top by count. Legacy CSV-era reviews (no listing_id)
# are excluded here — they have no listing to attribute to — but they DO count
# in the overall headline above, so the totals stay honest.
REVIEW_BY_LISTING_SQL = """
SELECT
    listing_title,
    review_count,
    avg_rating,
    stars_5, stars_4, stars_3, stars_2, stars_1,
    last_review
FROM ops.review_summary
WHERE listing_id IS NOT NULL
ORDER BY review_count DESC, avg_rating DESC
LIMIT 10
"""

# Latest reviews ticker — reviewer-safe (view exposes no buyer PII).
REVIEW_RECENT_SQL = """
SELECT star_rating, message, reviewed_at, listing_title
FROM ops.review_recent
LIMIT 3
"""

# Per-listing attention for the latest snapshot day, top by views, with the
# day-over-day delta (NULL until a second snapshot exists). New listings appear
# automatically the day their first snapshot lands.
LISTING_ATTENTION_SQL = """
WITH latest AS (SELECT MAX(snapshot_date) AS d FROM ops.listing_attention)
SELECT
    a.listing_title,
    a.state,
    a.views,
    a.num_favorers,
    a.views_delta,
    a.favorers_delta,
    a.has_prior,
    a.snapshot_date
FROM ops.listing_attention a, latest
WHERE a.snapshot_date = latest.d
ORDER BY a.views DESC NULLS LAST
LIMIT 5
"""

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine
        _engine = create_engine(DB_URI)
    return _engine


def _query(sql: str, params=None) -> list:
    import pandas as pd
    from sqlalchemy import text
    with _get_engine().connect() as conn:
        df = pd.read_sql_query(text(sql), conn, params=params)
    records = df.to_dict(orient="records")
    # pd.read_sql_query maps SQL NULL -> NaN, which is truthy; normalize at
    # the boundary so html.escape() and `value or ""` behave.
    for row in records:
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                row[k] = None
    return records


def _window_params() -> dict:
    return {
        "start": PHASE1_START,
        "end": PHASE1_END,
        "target": PHASE1_TARGET_VISITS,
        "campaign_tag": PHASE1_CAMPAIGN_TAG,
    }


def query_headline() -> dict:
    rows = _query(HEADLINE_SQL, _window_params())
    return rows[0] if rows else {}


def query_by_platform() -> list:
    return _query(BY_PLATFORM_SQL, _window_params())


def query_by_post() -> list:
    return _query(BY_POST_SQL, _window_params())


def query_by_landing() -> list:
    return _query(BY_LANDING_SQL, _window_params())


def query_link_performance() -> list:
    return _query(LINK_PERFORMANCE_SQL, {
        "start": PHASE1_START,
        "end": PHASE1_END,
        "campaign_tag": PHASE1_CAMPAIGN_TAG,
    })


def query_etsy_yoy() -> list:
    return _query(ETSY_YOY_SQL)


def query_freshness() -> dict:
    """GA4 freshness. Try the ops.ga4_freshness view; fall back to a direct
    MAX(event_date) if the view doesn't exist. Never raises — a freshness
    probe must not be able to break the dashboard."""
    for sql in (FRESHNESS_VIEW_SQL, FRESHNESS_FALLBACK_SQL):
        try:
            rows = _query(sql, {})
            if rows:
                return rows[0]
        except Exception:  # noqa: BLE001 — view-missing or transient; try fallback / give up
            continue
    return {}


def _query_safe(sql: str, params=None, default=None):
    """Run a query but never raise — a missing ops view (e.g. migration 008 not
    yet deployed to this DB) degrades the panel to empty, it can't 500 the page.
    Mirrors query_freshness()'s defensive posture for the Task #64 panels."""
    try:
        return _query(sql, params)
    except Exception:  # noqa: BLE001 — view-missing or transient
        return default if default is not None else []


def query_review_headline() -> dict:
    rows = _query_safe(REVIEW_HEADLINE_SQL, {})
    return rows[0] if rows else {}


def query_reviews_by_listing() -> list:
    return _query_safe(REVIEW_BY_LISTING_SQL, {})


def query_reviews_recent() -> list:
    return _query_safe(REVIEW_RECENT_SQL, {})


def query_listing_attention() -> list:
    return _query_safe(LISTING_ATTENTION_SQL, {})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_int(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(v) -> str:
    if v is None:
        return "—"
    return str(v)[:10]


def _esc(v) -> str:
    return html_mod.escape("" if v is None else str(v))


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------

def _render_headline(h: dict) -> str:
    """Big-number scoreboard for criterion 4."""
    if not h:
        return "<div class='panel'><h2>Headline</h2><p class='empty'>No data yet.</p></div>"

    visits = int(h.get("visits_to_date") or 0)
    floor = int(h.get("visits_floor") or PHASE1_TARGET_VISITS)
    pct = float(h.get("pct_of_floor") or 0.0)
    days_elapsed = int(h.get("days_elapsed") or 0)
    days_remaining = int(h.get("days_remaining") or 0)
    pace = h.get("pace_projection_90d")
    pace_str = _fmt_int(pace) if pace is not None else "—"

    # Day N might be 0 or negative pre-launch — clamp visually.
    day_label = max(0, days_elapsed)
    pct_clamped = max(0.0, min(pct, 999.9))

    # Floor-progress bar caps at 100% width; >100% shows as full + over badge.
    bar_pct = min(pct_clamped, 100.0)
    over_floor = pct_clamped >= 100.0

    pace_class = "stat-pace-on" if pace and int(pace) >= floor else "stat-pace-off"

    return f"""
<div class="panel headline">
  <h2>Phase 1 progress — criterion 4 (500-visit organic floor)</h2>
  <div class="stat-grid">
    <div class="stat">
      <div class="stat-num">{day_label}</div>
      <div class="stat-label">Day of {PHASE1_DAYS}</div>
    </div>
    <div class="stat">
      <div class="stat-num">{days_remaining}</div>
      <div class="stat-label">Days remaining</div>
    </div>
    <div class="stat">
      <div class="stat-num">{_fmt_int(visits)}</div>
      <div class="stat-label">Visits to date</div>
    </div>
    <div class="stat">
      <div class="stat-num">{pct_clamped:.1f}<span class="stat-unit">%</span></div>
      <div class="stat-label">of {floor}-visit floor</div>
    </div>
    <div class="stat">
      <div class="stat-num {pace_class}">{pace_str}</div>
      <div class="stat-label">90-day pace projection</div>
    </div>
  </div>
  <div class="progress-wrap">
    <div class="progress-bar-bg">
      <div class="progress-bar" style="width:{bar_pct:.1f}%"></div>
    </div>
    <div class="progress-meta">
      <span>{_fmt_int(visits)} / {floor}</span>
      {"<span class='over-floor'>over floor</span>" if over_floor else ""}
    </div>
  </div>
  <div class="window-meta">
    Window {PHASE1_START} → {PHASE1_END} · attribution: utm_campaign = phase1_reactivation
  </div>
</div>"""


def _render_by_platform(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='6'>No platform visits yet.</td></tr>"
    else:
        max_visits = max((int(r.get("visits") or 0) for r in rows), default=1) or 1
        body_parts = []
        for r in rows:
            visits = int(r.get("visits") or 0)
            pct = (visits / max_visits) * 100
            body_parts.append(
                "<tr>"
                f"<td class='platform'>{_esc(r.get('utm_source'))}</td>"
                f"<td class='num'>{_fmt_int(visits)}"
                f"<div class='inline-bar'><div class='inline-bar-fill' style='width:{pct:.0f}%'></div></div>"
                "</td>"
                f"<td class='num'>{_fmt_int(r.get('unique_visitors'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('sessions'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('first_seen'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('last_seen'))}</td>"
                "</tr>"
            )
        body = "\n".join(body_parts)
    return f"""
<div class="panel">
  <h2>By platform (utm_source)</h2>
  <table>
    <thead>
      <tr>
        <th>Platform</th>
        <th class='num'>Visits</th>
        <th class='num'>Unique</th>
        <th class='num'>Sessions</th>
        <th>First seen</th>
        <th>Last seen</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _render_by_post(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='6'>No posts attributed yet.</td></tr>"
    else:
        body_parts = []
        for r in rows:
            body_parts.append(
                "<tr>"
                f"<td class='platform'>{_esc(r.get('utm_source'))}</td>"
                f"<td class='code'>{_esc(r.get('utm_content'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('visits'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('unique_visitors'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('first_seen'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('last_seen'))}</td>"
                "</tr>"
            )
        body = "\n".join(body_parts)
    return f"""
<div class="panel">
  <h2>By post (utm_source + utm_content, top 25)</h2>
  <table>
    <thead>
      <tr>
        <th>Platform</th>
        <th>utm_content</th>
        <th class='num'>Visits</th>
        <th class='num'>Unique</th>
        <th>First seen</th>
        <th>Last seen</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _render_by_landing(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='6'>No landing pages attributed yet.</td></tr>"
    else:
        body_parts = []
        for r in rows:
            landing = r.get("landing_page") or ""
            landing_short = landing if len(landing) <= 64 else landing[:61] + "…"
            body_parts.append(
                "<tr>"
                f"<td class='platform'>{_esc(r.get('platform'))}</td>"
                f"<td class='url'><a href='{_esc(landing)}' target='_blank' rel='noopener' title='{_esc(landing)}'>{_esc(landing_short)}</a></td>"
                f"<td class='num'>{_fmt_int(r.get('visits'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('unique_visitors'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('first_seen'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('last_seen'))}</td>"
                "</tr>"
            )
        body = "\n".join(body_parts)
    return f"""
<div class="panel">
  <h2>By landing page (platform + destination URL, top 25)</h2>
  <p class='panel-note'>Where Phase 1 clicks actually landed — query string stripped so listing variations collapse. Complements the by-post view.</p>
  <table>
    <thead>
      <tr>
        <th>Platform</th>
        <th>Landing page</th>
        <th class='num'>Visits</th>
        <th class='num'>Unique</th>
        <th>First seen</th>
        <th>Last seen</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _render_link_performance(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='7'>No utm.py-generated links registered yet.</td></tr>"
        summary = ""
    else:
        total = len(rows)
        with_visits = sum(1 for r in rows if int(r.get("visits") or 0) > 0)
        zero = total - with_visits
        total_visits = sum(int(r.get("visits") or 0) for r in rows)
        # etsy/own_site split — computed from destination_kind (Q6 enrichment
        # 2026-06-01; no extra query, the rich Q5 already carries the column).
        etsy_links = sum(1 for r in rows if r.get("destination_kind") == "etsy")
        own_site_links = sum(1 for r in rows if r.get("destination_kind") == "own_site")
        summary = (
            f"<div class='link-summary'>"
            f"<span><b>{total}</b> links generated</span>"
            f"<span><b>{etsy_links}</b> Etsy</span>"
            f"<span><b>{own_site_links}</b> own-site</span>"
            f"<span><b>{with_visits}</b> with visits</span>"
            f"<span class='zero-stat'><b>{zero}</b> zero-visit</span>"
            f"<span><b>{_fmt_int(total_visits)}</b> total attributed visits</span>"
            f"</div>"
        )
        body_parts = []
        for r in rows:
            visits = int(r.get("visits") or 0)
            row_cls = "zero-visit" if visits == 0 else ""
            visits_cell = (
                "<span class='zero-marker' title='no clicks attributed yet'>—</span>"
                if visits == 0 else _fmt_int(visits)
            )
            base_url = r.get("base_url") or ""
            url_short = base_url
            if len(url_short) > 60:
                url_short = url_short[:57] + "…"
            body_parts.append(
                f"<tr class='{row_cls}'>"
                f"<td class='platform'>{_esc(r.get('utm_source'))}</td>"
                f"<td class='code'>{_esc(r.get('utm_content'))}</td>"
                f"<td class='url'><a href='{_esc(base_url)}' target='_blank' rel='noopener' title='{_esc(base_url)}'>{_esc(url_short)}</a></td>"
                f"<td class='date'>{_fmt_date(r.get('link_created'))}</td>"
                f"<td class='num'>{visits_cell}</td>"
                f"<td class='num'>{_fmt_int(r.get('unique_visitors'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('last_visit'))}</td>"
                "</tr>"
            )
        body = "\n".join(body_parts)
    return f"""
<div class="panel">
  <h2>Per-link performance — every URL Brian generated</h2>
  <p class='panel-note'>Zero-visit links are kept on purpose — they're the engagement-pattern signal for criterion 1.</p>
  {summary}
  <table>
    <thead>
      <tr>
        <th>Platform</th>
        <th>utm_content</th>
        <th>Base URL</th>
        <th>Created</th>
        <th class='num'>Visits</th>
        <th class='num'>Unique</th>
        <th>Last visit</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _render_etsy_yoy(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='4'>No Etsy traffic stats loaded.</td></tr>"
    else:
        body_parts = []
        for r in rows:
            body_parts.append(
                "<tr>"
                f"<td class='num'>{_esc(r.get('year'))}</td>"
                f"<td>{_esc(r.get('sub_source') or '(all social)')}</td>"
                f"<td class='num'>{_fmt_int(r.get('visits'))}</td>"
                f"<td class='date'>{_fmt_date(r.get('date_range_start'))} → {_fmt_date(r.get('date_range_end'))}</td>"
                "</tr>"
            )
        body = "\n".join(body_parts)
    return f"""
<div class="panel panel-narrow">
  <h2>Etsy "Social Media" YoY (annual totals from Etsy PDFs)</h2>
  <p class='panel-note'>Coarse YoY context — 2025 was paid-driven, 2026 YTD is the pre-Phase-1 baseline.</p>
  <table>
    <thead>
      <tr>
        <th class='num'>Year</th>
        <th>Sub-source</th>
        <th class='num'>Visits</th>
        <th>Window</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _fmt_delta(v) -> str:
    """Signed day-over-day delta cell. None (no prior snapshot) -> a muted dash
    so the single-snapshot day reads as 'no trend yet', not '0'."""
    if v is None:
        return "<span class='delta-none' title='no prior snapshot yet'>—</span>"
    try:
        n = int(v)
    except (TypeError, ValueError):
        return _esc(v)
    if n > 0:
        return f"<span class='delta-up'>+{n:,}</span>"
    if n < 0:
        return f"<span class='delta-down'>{n:,}</span>"
    return "<span class='delta-flat'>0</span>"


def _render_stars(rating) -> str:
    """Compact star glyphs for an avg/whole rating (rounded to nearest half)."""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return ""
    full = int(r)
    half = (r - full) >= 0.5
    stars = "★" * full + ("⯨" if half else "") + "☆" * (5 - full - (1 if half else 0))
    return f"<span class='stars' title='{r:.2f}'>{stars}</span>"


def _render_reviews(headline: dict, by_listing: list, recent: list) -> str:
    """Etsy reviews panel: headline (avg + count), latest-3 ticker, per-listing
    ratings table (top by count). Reviewer-safe — message text + rating + date
    only, as Etsy shows publicly."""
    total = headline.get("total_reviews")
    avg = headline.get("overall_avg")
    if not total:
        head = "<p class='empty'>No reviews loaded yet.</p>"
    else:
        head = (
            "<div class='review-head'>"
            f"<div class='review-avg'>{float(avg):.2f}{_render_stars(avg)}</div>"
            f"<div class='review-count'>{_fmt_int(total)} reviews</div>"
            "</div>"
        )

    # Latest-3 ticker.
    if recent:
        items = []
        for r in recent:
            msg = (r.get("message") or "").strip()
            # Etsy double-encodes some entities in the export (&#39; etc.); render
            # the raw text but trim + escape for safety. Public text only.
            if len(msg) > 140:
                msg = msg[:137] + "…"
            msg_html = _esc(msg) if msg else "<i>(rating only, no text)</i>"
            items.append(
                "<div class='review-item'>"
                f"<div class='review-item-top'>{_render_stars(r.get('star_rating'))}"
                f"<span class='review-date'>{_fmt_date(r.get('reviewed_at'))}</span>"
                f"<span class='review-listing'>{_esc((r.get('listing_title') or '')[:48])}</span></div>"
                f"<div class='review-msg'>{msg_html}</div>"
                "</div>"
            )
        ticker = "<div class='review-ticker'>" + "".join(items) + "</div>"
    else:
        ticker = "<p class='panel-note'>No recent reviews to show.</p>"

    # Per-listing ratings table.
    if by_listing:
        rows_html = []
        for r in by_listing:
            title = r.get("listing_title") or ""
            title_short = title if len(title) <= 64 else title[:61] + "…"
            rows_html.append(
                "<tr>"
                f"<td class='platform' title='{_esc(title)}'>{_esc(title_short)}</td>"
                f"<td class='num'>{_fmt_int(r.get('review_count'))}</td>"
                f"<td class='num'>{float(r.get('avg_rating')):.2f} {_render_stars(r.get('avg_rating'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('stars_5'))}</td>"
                f"<td class='num'>{_fmt_int((r.get('stars_4') or 0) + (r.get('stars_3') or 0) + (r.get('stars_2') or 0) + (r.get('stars_1') or 0))}</td>"
                f"<td class='date'>{_fmt_date(r.get('last_review'))}</td>"
                "</tr>"
            )
        table_body = "\n".join(rows_html)
    else:
        table_body = "<tr class='empty'><td colspan='6'>No per-listing reviews with a listing id yet.</td></tr>"

    return f"""
<div class="panel">
  <h2>Etsy reviews</h2>
  <p class='panel-note'>Public review data via the Etsy API (refreshed daily). Per-listing rows below exclude legacy CSV-era reviews that predate listing-id capture; those still count in the headline total.</p>
  {head}
  {ticker}
  <table>
    <thead>
      <tr>
        <th>Listing</th>
        <th class='num'>Reviews</th>
        <th class='num'>Avg</th>
        <th class='num'>5★</th>
        <th class='num'>&lt;5★</th>
        <th>Last review</th>
      </tr>
    </thead>
    <tbody>{table_body}</tbody>
  </table>
</div>"""


def _render_listing_attention(rows: list) -> str:
    """Listing-attention panel: per-listing views + favorers for the latest
    snapshot day, with day-over-day deltas (NULL until a 2nd snapshot lands).
    Top 5 by views; new listings appear automatically on their first snapshot."""
    if not rows:
        body = "<tr class='empty'><td colspan='6'>No listing snapshots yet.</td></tr>"
        snap_note = ""
        trend_building = False
    else:
        trend_building = not any(r.get("has_prior") for r in rows)
        snap_date = _fmt_date(rows[0].get("snapshot_date"))
        snap_note = f"Snapshot {snap_date}."
        max_views = max((int(r.get("views") or 0) for r in rows), default=1) or 1
        parts = []
        for r in rows:
            views = int(r.get("views") or 0)
            pct = (views / max_views) * 100
            title = r.get("listing_title") or ""
            title_short = title if len(title) <= 64 else title[:61] + "…"
            parts.append(
                "<tr>"
                f"<td class='platform' title='{_esc(title)}'>{_esc(title_short)}</td>"
                f"<td class='date'>{_esc(r.get('state'))}</td>"
                f"<td class='num'>{_fmt_int(views)}"
                f"<div class='inline-bar'><div class='inline-bar-fill' style='width:{pct:.0f}%'></div></div>"
                "</td>"
                f"<td class='num'>{_fmt_delta(r.get('views_delta'))}</td>"
                f"<td class='num'>{_fmt_int(r.get('num_favorers'))}</td>"
                f"<td class='num'>{_fmt_delta(r.get('favorers_delta'))}</td>"
                "</tr>"
            )
        body = "\n".join(parts)

    trend_banner = (
        "<p class='panel-note'>Day-over-day deltas build daily — one snapshot so far, so deltas show “—”.</p>"
        if trend_building else ""
    )
    return f"""
<div class="panel">
  <h2>Listing attention (views &amp; favorers)</h2>
  <p class='panel-note'>Top listings by views from the daily Etsy listing-stats snapshot. {snap_note} Multi-species (lists 6/9) and custom-engraved (6/16) appear here automatically once live.</p>
  {trend_banner}
  <table>
    <thead>
      <tr>
        <th>Listing</th>
        <th>State</th>
        <th class='num'>Views</th>
        <th class='num'>Δ views</th>
        <th class='num'>Favorers</th>
        <th class='num'>Δ fav</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
</div>"""


def _render_freshness_badge(fresh: dict) -> str:
    """Small topbar badge surfacing GA4 load freshness (Flag B). Warns when
    days_behind > 1 so a stalled ga4-load cron is visible on the page."""
    if not fresh:
        return (
            "<span class='freshness freshness-unknown' "
            "title='Could not read GA4 freshness'>GA4: unknown</span>"
        )
    db = fresh.get("days_behind")
    latest = _fmt_date(fresh.get("latest_event_date"))
    try:
        db_int = int(db) if db is not None else None
    except (TypeError, ValueError):
        db_int = None
    if db_int is None:
        cls, label = "freshness-unknown", "GA4: unknown"
    elif db_int > 1:
        cls = "freshness-stale"
        label = f"GA4 stale: {db_int} days behind (latest {latest})"
    else:
        cls = "freshness-ok"
        label = f"GA4 current · latest {latest}"
    return f"<span class='freshness {cls}' title='days_behind={db}'>{_esc(label)}</span>"


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def build_html(
    headline: dict,
    by_platform: list,
    by_post: list,
    by_landing: list,
    links: list,
    etsy: list,
    freshness: dict | None = None,
    review_headline: dict | None = None,
    reviews_by_listing: list | None = None,
    reviews_recent: list | None = None,
    listing_attention: list | None = None,
) -> str:
    headline_html = _render_headline(headline)
    freshness_html = _render_freshness_badge(freshness or {})
    platform_html = _render_by_platform(by_platform)
    post_html = _render_by_post(by_post)
    landing_html = _render_by_landing(by_landing)
    link_html = _render_link_performance(links)
    etsy_html = _render_etsy_yoy(etsy)
    reviews_html = _render_reviews(
        review_headline or {}, reviews_by_listing or [], reviews_recent or []
    )
    attention_html = _render_listing_attention(listing_attention or [])

    # Banner sentence — matches the CLI's _print_headline_summary so Brian
    # gets the same plain-English read at the top of the page.
    if headline:
        visits = int(headline.get("visits_to_date") or 0)
        floor = int(headline.get("visits_floor") or PHASE1_TARGET_VISITS)
        pct = float(headline.get("pct_of_floor") or 0.0)
        days_elapsed = max(0, int(headline.get("days_elapsed") or 0))
        days_remaining = int(headline.get("days_remaining") or 0)
        banner = (
            f"Day {days_elapsed} of {PHASE1_DAYS} · {days_remaining} remaining · "
            f"{visits} of {floor} visits ({pct:.1f}% of criterion-4 floor)"
        )
    else:
        banner = f"Phase 1 starts {PHASE1_START}."

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Elders Nine Relics — Phase 1 Pilot</title>
<style>
  :root {{
    --bg: #181c2f;             /* Deep Void */
    --bg-deeper: #0f1220;
    --surface: #21253f;
    --surface-alt: #2a2e4c;
    --border: #3a3559;
    --plum: #30264b;           /* Mystic Plum */
    --plum-bright: #8a76c4;
    --ivory: #ece2dc;          /* Runestone Ivory */
    --sand: #cac0af;           /* Relic Sand */
    --slate: #6d6966;          /* Ancient Slate */
    --good: #8bbf8b;
    --warn: #d9b56b;
    --bad: #c47878;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 14px;
    color: var(--ivory);
    background: var(--bg);
    min-height: 100vh;
  }}
  a {{ color: var(--plum-bright); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .topbar {{
    background: linear-gradient(135deg, var(--plum) 0%, #4a3b73 100%);
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }}
  .topbar-left {{ display: flex; align-items: center; gap: 14px; }}
  .logo-mark {{ flex-shrink: 0; }}
  .brand-group {{ display: flex; flex-direction: column; gap: 1px; }}
  .brand {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--sand);
    line-height: 1;
  }}
  .topbar h1 {{
    font-size: 22px;
    font-weight: 700;
    color: var(--ivory);
    letter-spacing: -0.01em;
    line-height: 1.2;
  }}
  .topbar-right {{
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }}
  .banner {{
    color: var(--sand);
    font-size: 13px;
    font-weight: 500;
  }}
  .refresh-btn {{
    padding: 7px 18px;
    font-size: 12px;
    font-weight: 600;
    color: var(--ivory);
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.25);
    border-radius: 6px;
    cursor: pointer;
    letter-spacing: 0.02em;
  }}
  .refresh-btn:hover {{ background: rgba(255,255,255,0.2); }}
  .refresh-btn.loading {{ opacity: 0.5; pointer-events: none; }}
  .freshness {{
    font-size: 11px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 5px;
    letter-spacing: 0.02em;
    border: 1px solid transparent;
    white-space: nowrap;
  }}
  .freshness-ok {{ color: var(--good); background: rgba(139,191,139,0.12); border-color: rgba(139,191,139,0.35); }}
  .freshness-stale {{ color: var(--bad); background: rgba(196,120,120,0.15); border-color: rgba(196,120,120,0.45); }}
  .freshness-unknown {{ color: var(--sand); background: rgba(202,192,175,0.10); border-color: rgba(202,192,175,0.30); }}
  .content {{ padding: 24px 32px 48px; max-width: 1400px; margin: 0 auto; }}
  .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 22px 26px;
    margin-bottom: 24px;
  }}
  .panel h2 {{
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--sand);
    margin-bottom: 14px;
  }}
  .panel-note {{
    color: var(--slate);
    font-size: 12px;
    margin-bottom: 12px;
    font-style: italic;
  }}
  .panel-narrow {{ max-width: 720px; }}
  /* --- Headline scoreboard --- */
  .headline {{ background: linear-gradient(135deg, var(--surface) 0%, var(--bg-deeper) 100%); }}
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 16px;
    margin-bottom: 18px;
  }}
  @media (max-width: 900px) {{
    .stat-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  .stat {{
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }}
  .stat-num {{
    font-size: 32px;
    font-weight: 700;
    color: var(--ivory);
    line-height: 1;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }}
  .stat-unit {{ font-size: 18px; color: var(--sand); margin-left: 2px; }}
  .stat-label {{
    margin-top: 8px;
    font-size: 11px;
    color: var(--sand);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }}
  .stat-pace-on {{ color: var(--good); }}
  .stat-pace-off {{ color: var(--warn); }}
  .progress-wrap {{ margin-top: 6px; }}
  .progress-bar-bg {{
    height: 12px;
    background: var(--bg-deeper);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }}
  .progress-bar {{
    height: 100%;
    background: linear-gradient(90deg, var(--plum) 0%, var(--plum-bright) 100%);
    transition: width 0.3s;
  }}
  .progress-meta {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-top: 6px;
    font-size: 12px;
    color: var(--sand);
    font-variant-numeric: tabular-nums;
  }}
  .over-floor {{
    color: var(--good);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 11px;
  }}
  .window-meta {{
    margin-top: 12px;
    font-size: 11px;
    color: var(--slate);
    letter-spacing: 0.02em;
  }}
  /* --- Tables --- */
  table {{
    width: 100%;
    border-collapse: collapse;
    background: transparent;
  }}
  th {{
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--sand);
    background: var(--surface-alt);
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }}
  th.num {{ text-align: right; }}
  td {{
    padding: 9px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
    font-size: 13px;
    color: var(--ivory);
  }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.date {{ color: var(--sand); font-size: 12px; font-variant-numeric: tabular-nums; }}
  td.platform {{ font-weight: 600; }}
  td.code {{
    font-family: "SF Mono", SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: var(--sand);
    word-break: break-all;
  }}
  td.url a {{ font-size: 12px; }}
  tr:hover td {{ background: rgba(255,255,255,0.025); }}
  tr.empty td {{
    color: var(--slate);
    font-style: italic;
    text-align: center;
    padding: 18px;
  }}
  tr.zero-visit td {{ opacity: 0.62; }}
  tr.zero-visit:hover td {{ opacity: 0.85; }}
  .zero-marker {{
    color: var(--slate);
    font-weight: 700;
  }}
  /* Inline bar inside the platform-visits cell */
  .inline-bar {{
    margin-top: 4px;
    height: 4px;
    background: var(--bg-deeper);
    border-radius: 2px;
    overflow: hidden;
    width: 100%;
  }}
  .inline-bar-fill {{
    height: 100%;
    background: var(--plum-bright);
    border-radius: 2px;
  }}
  /* Link-performance summary chips */
  .link-summary {{
    display: flex;
    gap: 18px;
    flex-wrap: wrap;
    margin-bottom: 12px;
    font-size: 12px;
    color: var(--sand);
  }}
  .link-summary b {{ color: var(--ivory); font-weight: 700; }}
  .link-summary .zero-stat b {{ color: var(--warn); }}
  /* --- Reviews (Task #64) --- */
  .empty {{ color: var(--slate); font-style: italic; font-size: 13px; padding: 6px 0; }}
  .review-head {{
    display: flex; align-items: baseline; gap: 14px; margin-bottom: 14px;
  }}
  .review-avg {{
    font-size: 30px; font-weight: 700; color: var(--ivory);
    font-variant-numeric: tabular-nums; display: flex; align-items: baseline; gap: 8px;
  }}
  .review-count {{ font-size: 13px; color: var(--sand); font-weight: 600; }}
  .stars {{ color: var(--warn); font-size: 15px; letter-spacing: 1px; }}
  .review-avg .stars {{ font-size: 20px; }}
  .review-ticker {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }}
  .review-item {{
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px;
  }}
  .review-item-top {{
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 4px;
  }}
  .review-date {{ font-size: 11px; color: var(--sand); font-variant-numeric: tabular-nums; }}
  .review-listing {{ font-size: 11px; color: var(--slate); font-style: italic; }}
  .review-msg {{ font-size: 13px; color: var(--ivory); line-height: 1.4; }}
  /* --- Day-over-day deltas --- */
  .delta-up {{ color: var(--good); font-weight: 600; }}
  .delta-down {{ color: var(--bad); font-weight: 600; }}
  .delta-flat {{ color: var(--sand); }}
  .delta-none {{ color: var(--slate); font-weight: 700; }}
  /* Footer */
  .footer {{
    color: var(--slate);
    font-size: 11px;
    text-align: center;
    padding: 16px 0 8px;
  }}
</style>
<script>
function refreshData() {{
  var btn = document.getElementById('refreshBtn');
  btn.classList.add('loading');
  btn.textContent = 'Refreshing...';
  fetch('/refresh').then(function(r) {{ return r.text(); }}).then(function(html) {{
    document.open();
    document.write(html);
    document.close();
  }}).catch(function() {{
    btn.classList.remove('loading');
    btn.textContent = 'Refresh';
    alert('Refresh failed — check server.');
  }});
}}
</script>
</head>
<body>
<div class="topbar">
  <div class="topbar-left">
    <svg class="logo-mark" width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="18" cy="18" r="15" stroke="rgba(236,226,220,0.35)" stroke-width="1.5" fill="none"/>
      <path d="M18 6 L22 14 L30 14 L24 19 L26 27 L18 22 L10 27 L12 19 L6 14 L14 14 Z" fill="rgba(236,226,220,0.85)"/>
    </svg>
    <div class="brand-group">
      <span class="brand">Elders Nine Relics</span>
      <h1>Phase 1 Reactivation Pilot</h1>
    </div>
  </div>
  <div class="topbar-right">
    <span class="banner">{_esc(banner)}</span>
    {freshness_html}
    <button id="refreshBtn" class="refresh-btn" onclick="refreshData()">Refresh</button>
  </div>
</div>
<div class="content">
  {headline_html}
  {reviews_html}
  {attention_html}
  {platform_html}
  {post_html}
  {landing_html}
  {link_html}
  {etsy_html}
  <div class="footer">
    Data: GA4 events (raw.ga4_events) + UTM registry (raw.utm_links) + Etsy stats (raw.etsy_traffic_stats) +
    Etsy reviews / listing attention (ops.review_summary · ops.review_recent · ops.listing_attention) ·
    Attribution: utm_campaign = phase1_reactivation
  </div>
</div>
</body>
</html>"""


def generate_page() -> str:
    return build_html(
        headline=query_headline(),
        by_platform=query_by_platform(),
        by_post=query_by_post(),
        by_landing=query_by_landing(),
        links=query_link_performance(),
        etsy=query_etsy_yoy(),
        freshness=query_freshness(),
        review_headline=query_review_headline(),
        reviews_by_listing=query_reviews_by_listing(),
        reviews_recent=query_reviews_recent(),
        listing_attention=query_listing_attention(),
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Phase1Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, get_page=None, **kwargs):
        self._get_page = get_page
        super().__init__(*args, **kwargs)

    def do_GET(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/refresh"):
            try:
                page = self._get_page()
            except Exception as exc:  # noqa: BLE001 — show errors in the browser, not as 500-blank
                page = _render_error_page(exc)
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/healthz":
            # ok + GA4 freshness so a stalled cron is visible to monitors too.
            # Never let the freshness probe turn /healthz into a 500.
            try:
                fresh = query_freshness()
                db = fresh.get("days_behind")
                latest = fresh.get("latest_event_date")
                suffix = f" ga4_days_behind={db} latest_event_date={latest}" if fresh else " ga4=unknown"
            except Exception:  # noqa: BLE001
                suffix = " ga4=unknown"
            body = ("ok" + suffix).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def _render_error_page(exc: Exception) -> str:
    msg = html_mod.escape(str(exc))
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Phase 1 dashboard — error</title>
<style>body{{font-family:-apple-system,sans-serif;background:#181c2f;color:#ece2dc;padding:32px;}}
pre{{background:#0f1220;border:1px solid #3a3559;padding:16px;border-radius:6px;overflow:auto;}}</style>
</head><body><h1>Phase 1 dashboard — query failed</h1>
<p>The page couldn't render. Most likely the DB is unreachable or the schema changed.</p>
<pre>{msg}</pre>
<p><a href="/" style="color:#8a76c4">Retry</a></p>
</body></html>"""


def serve() -> None:
    get_page = generate_page
    # Warm up — also validates DB.
    get_page()
    handler = partial(Phase1Handler, get_page=get_page)
    server = HTTPServer(("0.0.0.0", PORT), handler)
    print(f"Serving Phase 1 dashboard on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


def main() -> int:
    if "--serve" in sys.argv:
        serve()
        return 0
    # CLI parity: render once and print to stdout for ad-hoc checking.
    print(generate_page())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
