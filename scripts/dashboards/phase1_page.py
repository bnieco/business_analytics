"""Phase 1 Reactivation Pilot dashboard — browser-openable progress page.

Mirrors apply-dashboard's pattern: stdlib http.server + SQLAlchemy + pandas,
inline HTML. Five panels render the same data as
scripts/phase1_progress.py (the CLI) — query strings are copied verbatim
from there so the dashboard and CLI stay in lockstep.

Usage:
    Set env vars (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD) and
    run with --serve. The Dockerfile / compose stack handles that.
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

DB_URI = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
PORT = int(os.environ.get("PORT", "8788"))

# Phase 1 window — keep in sync with scripts/queries/phase1_progress.sql
# and scripts/phase1_progress.py.
PHASE1_START = date(2026, 5, 24)
PHASE1_END = date(2026, 8, 22)
PHASE1_TARGET_VISITS = 500
PHASE1_DAYS = 90

# ---------------------------------------------------------------------------
# SQL — copied verbatim from scripts/phase1_progress.py so any change in
# the CLI's queries is a single-file diff to mirror here.
# ---------------------------------------------------------------------------

HEADLINE_SQL = """
WITH visits AS (
    SELECT COUNT(*) AS phase1_visits
    FROM raw.ga4_events
    WHERE event_date BETWEEN :start AND :end
      AND event_name = 'page_view'
      AND collected_traffic_source_campaign = 'phase1_reactivation'
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
  AND collected_traffic_source_campaign = 'phase1_reactivation'
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
  AND collected_traffic_source_campaign = 'phase1_reactivation'
GROUP BY 1, 2
ORDER BY visits DESC
LIMIT 25
"""

LINK_PERFORMANCE_SQL = """
WITH ga4_attribution AS (
    SELECT
        collected_traffic_source_source   AS utm_source,
        collected_traffic_source_campaign AS utm_campaign,
        (SELECT ep -> 'value' ->> 'string_value'
         FROM jsonb_array_elements(event_params) ep
         WHERE ep ->> 'key' = 'content'
         LIMIT 1)                          AS utm_content,
        COUNT(*)                           AS visits,
        COUNT(DISTINCT user_pseudo_id)     AS unique_visitors,
        MIN(event_date)                    AS first_visit,
        MAX(event_date)                    AS last_visit
    FROM raw.ga4_events
    WHERE event_date BETWEEN :start AND :end
      AND event_name = 'page_view'
      AND collected_traffic_source_campaign = 'phase1_reactivation'
    GROUP BY 1, 2, 3
)
SELECT
    l.utm_source,
    l.utm_content,
    l.base_url,
    l.created_at::date              AS link_created,
    l.generated_count,
    COALESCE(g.visits, 0)           AS visits,
    COALESCE(g.unique_visitors, 0)  AS unique_visitors,
    g.first_visit,
    g.last_visit
FROM raw.utm_links l
LEFT JOIN ga4_attribution g
       ON g.utm_source   IS NOT DISTINCT FROM l.utm_source
      AND g.utm_campaign IS NOT DISTINCT FROM l.utm_campaign
      AND g.utm_content  IS NOT DISTINCT FROM l.utm_content
WHERE l.utm_campaign = 'phase1_reactivation'
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
    }


def query_headline() -> dict:
    rows = _query(HEADLINE_SQL, _window_params())
    return rows[0] if rows else {}


def query_by_platform() -> list:
    return _query(BY_PLATFORM_SQL, _window_params())


def query_by_post() -> list:
    return _query(BY_POST_SQL, _window_params())


def query_link_performance() -> list:
    return _query(LINK_PERFORMANCE_SQL, {
        "start": PHASE1_START,
        "end": PHASE1_END,
    })


def query_etsy_yoy() -> list:
    return _query(ETSY_YOY_SQL)


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


def _render_link_performance(rows: list) -> str:
    if not rows:
        body = "<tr class='empty'><td colspan='7'>No utm.py-generated links registered yet.</td></tr>"
        summary = ""
    else:
        total = len(rows)
        with_visits = sum(1 for r in rows if int(r.get("visits") or 0) > 0)
        zero = total - with_visits
        total_visits = sum(int(r.get("visits") or 0) for r in rows)
        summary = (
            f"<div class='link-summary'>"
            f"<span><b>{total}</b> links generated</span>"
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


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def build_html(
    headline: dict,
    by_platform: list,
    by_post: list,
    links: list,
    etsy: list,
) -> str:
    headline_html = _render_headline(headline)
    platform_html = _render_by_platform(by_platform)
    post_html = _render_by_post(by_post)
    link_html = _render_link_performance(links)
    etsy_html = _render_etsy_yoy(etsy)

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
    <button id="refreshBtn" class="refresh-btn" onclick="refreshData()">Refresh</button>
  </div>
</div>
<div class="content">
  {headline_html}
  {platform_html}
  {post_html}
  {link_html}
  {etsy_html}
  <div class="footer">
    Data: GA4 events (raw.ga4_events) + UTM registry (raw.utm_links) + Etsy stats (raw.etsy_traffic_stats) ·
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
        links=query_link_performance(),
        etsy=query_etsy_yoy(),
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
            body = b"ok"
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
