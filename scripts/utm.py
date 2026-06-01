"""utm.py — wrap a base URL with Phase 1 Reactivation Pilot UTM params.

The full scheme is documented in `docs/phase1_utm_scheme.md`. This script
exists so Brian doesn't have to assemble UTM strings by hand and so the
allowed `utm_source` values are validated at construction time.

Every generated URL is auto-logged to `raw.utm_links` (migration 004). The
log is idempotent: re-generating the same tagged URL bumps `generated_count`
and `last_generated_at` instead of inserting a duplicate row. The CLI's
stdout contract is unchanged — auto-log is a silent side effect unless
`--verbose` is passed.

Usage:
    python scripts/utm.py --url https://www.etsy.com/listing/123/foo \\
                          --source instagram \\
                          --content fearTracker_processVid_001

    python scripts/utm.py https://eldersninerelics.com/ instagram storefront_may01

Defaults:
    --medium    organic
    --campaign  phase1_reactivation
"""
from __future__ import annotations

import argparse
import sys
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

ALLOWED_SOURCES = {
    "facebook",
    "instagram",
    "pinterest",
    "reddit",
    "discord",
    "bgg",
}

DEFAULT_MEDIUM = "organic"
DEFAULT_CAMPAIGN = "phase1_reactivation"


def build_tagged_url(
    url: str,
    source: str,
    content: str,
    medium: str = DEFAULT_MEDIUM,
    campaign: str = DEFAULT_CAMPAIGN,
) -> str:
    """Return `url` with the four standard UTM params appended.

    If the input URL already has any utm_* query params, the new values
    overwrite them so the output is always consistent with the scheme.
    """
    source = source.strip().lower()
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"utm_source={source!r} is not in the allowed Phase 1 source list: "
            f"{sorted(ALLOWED_SOURCES)}. Either pick one of these or update "
            f"docs/phase1_utm_scheme.md first."
        )

    content = content.strip()
    if not content:
        raise ValueError("utm_content is required (a short post identifier).")
    if " " in content:
        raise ValueError(
            "utm_content cannot contain spaces. Use snake_case, e.g. "
            "'fearTracker_processVid_001'."
        )

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"URL looks malformed (need scheme + host): {url!r}")

    # Preserve existing non-utm params; replace utm_* if already present.
    existing = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    utm_params = [
        ("utm_source", source),
        ("utm_medium", medium),
        ("utm_campaign", campaign),
        ("utm_content", content),
    ]
    new_query = urlencode(existing + utm_params)

    return urlunparse(parsed._replace(query=new_query))


def _strip_utm(url: str) -> str:
    """Return `url` with all utm_* params removed — used for `base_url` logging."""
    parsed = urlparse(url)
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def log_to_db(
    url_full: str,
    base_url: str,
    source: str,
    medium: str,
    campaign: str,
    content: str,
    verbose: bool = False,
) -> None:
    """Insert (or upsert) the tagged URL into `raw.utm_links`.

    Failures here MUST NOT break the CLI — they only emit a stderr warning,
    and only when `--verbose`. Brian still gets his URL on stdout regardless
    of DB availability.
    """
    try:
        # Local import so `utm.py --help` doesn't require sqlalchemy or .env.
        from sqlalchemy import text
        from db import get_engine

        sql = text("""
            INSERT INTO raw.utm_links (
                url_full, base_url,
                utm_source, utm_medium, utm_campaign, utm_content,
                created_by
            )
            VALUES (
                :url_full, :base_url,
                :utm_source, :utm_medium, :utm_campaign, :utm_content,
                'utm.py'
            )
            ON CONFLICT (url_full) DO UPDATE
              SET generated_count   = raw.utm_links.generated_count + 1,
                  last_generated_at = now()
            RETURNING (xmax = 0) AS inserted, generated_count
        """)
        engine = get_engine()
        with engine.begin() as conn:
            row = conn.execute(sql, {
                "url_full": url_full,
                "base_url": base_url,
                "utm_source": source,
                "utm_medium": medium,
                "utm_campaign": campaign,
                "utm_content": content,
            }).fetchone()
        if verbose and row is not None:
            kind = "inserted" if row.inserted else f"bumped (count={row.generated_count})"
            print(f"[utm.py] raw.utm_links: {kind}", file=sys.stderr)
    except Exception as e:
        if verbose:
            print(f"[utm.py] WARN: failed to log to raw.utm_links: {e}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Wrap a URL with Phase 1 Reactivation Pilot UTM params.",
    )
    p.add_argument("url_pos", nargs="?", help="Base URL (positional form)")
    p.add_argument("source_pos", nargs="?", help="utm_source (positional form)")
    p.add_argument("content_pos", nargs="?", help="utm_content (positional form)")

    p.add_argument("--url", help="Base URL to tag")
    p.add_argument(
        "--source",
        help=f"utm_source — one of {sorted(ALLOWED_SOURCES)}",
    )
    p.add_argument("--content", help="utm_content — short post identifier")
    p.add_argument(
        "--medium",
        default=DEFAULT_MEDIUM,
        help=f"utm_medium (default: {DEFAULT_MEDIUM})",
    )
    p.add_argument(
        "--campaign",
        default=DEFAULT_CAMPAIGN,
        help=f"utm_campaign (default: {DEFAULT_CAMPAIGN})",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print DB-log status to stderr (silent by default).",
    )
    p.add_argument(
        "--no-log",
        action="store_true",
        help="Skip writing to raw.utm_links (useful for dry-run/testing).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    url = args.url or args.url_pos
    source = args.source or args.source_pos
    content = args.content or args.content_pos

    missing = [
        name for name, val in (("url", url), ("source", source), ("content", content))
        if not val
    ]
    if missing:
        print(
            f"error: missing required argument(s): {', '.join(missing)}\n"
            "Pass them as flags (--url --source --content) or positionally "
            "(URL SOURCE CONTENT).",
            file=sys.stderr,
        )
        return 2

    try:
        tagged = build_tagged_url(url, source, content, args.medium, args.campaign)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Stdout contract: just the tagged URL.
    print(tagged)

    if not args.no_log:
        log_to_db(
            url_full=tagged,
            base_url=_strip_utm(url),
            source=source.strip().lower(),
            medium=args.medium,
            campaign=args.campaign,
            content=content.strip(),
            verbose=args.verbose,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
