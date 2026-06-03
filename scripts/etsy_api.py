"""
etsy_api.py — Etsy Open API v3 → business.raw schema

Direct, read-only ingestion of THIS shop's own Etsy data, replacing the manual
CSV exports loaded by scripts/load_etsy.py. Personal access (own-shop read),
OAuth2 Authorization-Code + PKCE. Mirrors the shape of scripts/load_ga4.py:
argparse CLI, `logging` (no print in the load path; print reserved for
--dry-run), reuse of db.get_engine(), per-entity failure isolation, non-zero
exit on failure, ID-keyed delete-reload idempotency under an advisory lock, and
a raw.etsy_api_load_state watermark table.

Source of truth: Obsidian → Projects/Etsy Business/"Etsy API Integration —
Design and Registration.md" (Deliverable 2).

Endpoint -> table mapping
-------------------------
  getShopReceipts            -> raw.etsy_orders        (receipt_id -> order_id)
  receipt transactions[]     -> raw.etsy_order_items   (carry numeric listing_id)
  getListingsByShop          -> raw.etsy_listings      (populate listing_id)

OAuth model
-----------
  Authorize:  https://www.etsy.com/oauth/connect              (one-time consent)
  Token:      https://api.etsy.com/v3/public/oauth/token      (code + refresh)
  Scopes:     transactions_r listings_r                       (read-only)
  Access TTL ~1h (regenerated every run, never persisted)
  Refresh TTL ~90 days (the only long-lived secret; rotated -> rewritten to .env;
  the daily job keeps it alive indefinitely)
  PKCE S256 required. Headers: x-api-key: <keystring> + Authorization: Bearer.

Usage
-----
  # One-time interactive consent (only works once the app is ACTIVE).
  python scripts/etsy_api.py authorize

  # Dry run: validate .env keys, DB reachability, planned window. Makes NO
  # live API calls. If ETSY_REFRESH_TOKEN is absent (app not yet authorized),
  # prints a "pending activation" notice and exits 0.
  python scripts/etsy_api.py --dry-run

  # Default load: re-pull last 7 days of receipts + full listings refresh.
  python scripts/etsy_api.py

  # Specific window / tuning
  python scripts/etsy_api.py --since 2026-05-01 --until 2026-06-03
  python scripts/etsy_api.py --backfill-days 14
  python scripts/etsy_api.py --verbose

Environment (.env)
------------------
  BUSINESS_DB_*             Postgres connection (see .env.example)
  ETSY_API_KEYSTRING        app keystring (x-api-key) — Brian added
  ETSY_SHARED_SECRET        app shared secret — Brian added
  ETSY_SHOP_ID              numeric shop id (resolved once; see resolve_shop_id)
  ETSY_OAUTH_REDIRECT_URI   default http://localhost:3003/oauth/redirect
  ETSY_REFRESH_TOKEN        written by `authorize`; the only persisted secret
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

# Allow `from db import get_engine` when run as a script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

ETSY_API_KEYSTRING = os.getenv("ETSY_API_KEYSTRING")
ETSY_SHARED_SECRET = os.getenv("ETSY_SHARED_SECRET")  # noqa: F841 (kept for parity; PKCE flow doesn't use it)
ETSY_SHOP_ID = os.getenv("ETSY_SHOP_ID")
ETSY_OAUTH_REDIRECT_URI = os.getenv(
    "ETSY_OAUTH_REDIRECT_URI", "http://localhost:3003/oauth/redirect"
)
ETSY_REFRESH_TOKEN = os.getenv("ETSY_REFRESH_TOKEN")

OAUTH_SCOPES = "transactions_r listings_r shops_r"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"

# The throwaway callback server. Port must match the redirect URI's port.
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 3003

DEFAULT_BACKFILL_DAYS = 7          # receipts re-pull window (catches status changes)
PAGE_LIMIT = 100                   # Etsy max page size
INTER_REQUEST_SLEEP = 0.25         # gentle pacing between paged calls
MAX_RETRIES = 6                    # for 429 / 5xx
BACKOFF_BASE = 1.5                 # exponential backoff base (seconds)
REQUEST_TIMEOUT = 30               # per-HTTP-request timeout (seconds)

log = logging.getLogger("etsy_api")


# ===========================================================================
# .env read/write helpers (for refresh-token persistence + rotation)
# ===========================================================================

def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text().splitlines()


def upsert_env_var(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or appending. Only the
    refresh token is written here. Never logs the value."""
    lines = _read_env_lines()
    new_line = f"{key}={value}"
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    ENV_PATH.write_text("\n".join(lines) + "\n")
    log.info("Updated %s in .env", key)


# ===========================================================================
# OAuth2 Authorization-Code + PKCE
# ===========================================================================

def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code=&state= from Etsy's redirect."""

    captured: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _OAuthCallbackHandler.captured = {
            "code": params.get("code", [""])[0],
            "state": params.get("state", [""])[0],
            "error": params.get("error", [""])[0],
            "error_description": params.get("error_description", [""])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _OAuthCallbackHandler.captured["code"]:
            body = (
                "<html><body><h2>Etsy authorization received.</h2>"
                "<p>You can close this tab and return to the terminal.</p></body></html>"
            )
        else:
            body = (
                "<html><body><h2>Etsy authorization failed.</h2>"
                f"<p>{_OAuthCallbackHandler.captured.get('error_description') or 'No code returned.'}</p>"
                "</body></html>"
            )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args):  # silence the default stderr access log
        return


def cmd_authorize() -> int:
    """One-time interactive OAuth consent. Captures the auth code on a throwaway
    localhost server, exchanges it for tokens, and writes ETSY_REFRESH_TOKEN to
    .env. Only succeeds once the app is ACTIVE (keystring approved by Etsy)."""
    if not ETSY_API_KEYSTRING:
        print("ERROR: ETSY_API_KEYSTRING missing from .env. Cannot authorize.")
        return 2

    redirect = ETSY_OAUTH_REDIRECT_URI
    parsed_redirect = urllib.parse.urlparse(redirect)
    port = parsed_redirect.port or CALLBACK_PORT

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_params = {
        "response_type": "code",
        "client_id": ETSY_API_KEYSTRING,
        "redirect_uri": redirect,
        "scope": OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    consent_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    print("=" * 72)
    print("Etsy OAuth — one-time authorization")
    print("=" * 72)
    print("Scopes requested:", OAUTH_SCOPES)
    print("Redirect URI    :", redirect)
    print()
    print("Opening your browser to approve. If it does not open, paste this URL:")
    print()
    print(consent_url)
    print()

    # Start the throwaway callback server BEFORE opening the browser.
    try:
        server = HTTPServer((CALLBACK_HOST, port), _OAuthCallbackHandler)
    except OSError as e:
        print(f"ERROR: cannot bind {CALLBACK_HOST}:{port} for the OAuth callback: {e}")
        print("Is another process using the port? Free it and re-run `authorize`.")
        return 2

    try:
        webbrowser.open(consent_url)
    except Exception:
        pass  # URL is printed above; manual paste works.

    print(f"Waiting for Etsy's redirect on http://{CALLBACK_HOST}:{port} ...")
    server.handle_request()  # blocks until one request (the redirect) is served
    server.server_close()

    captured = _OAuthCallbackHandler.captured
    if captured.get("error"):
        print(f"ERROR: Etsy returned an error: {captured.get('error_description') or captured['error']}")
        return 2
    code = captured.get("code")
    returned_state = captured.get("state")
    if not code:
        print("ERROR: no authorization code received. Re-run `authorize`.")
        return 2
    if returned_state != state:
        print("ERROR: OAuth state mismatch (possible CSRF). Aborting; re-run `authorize`.")
        return 2

    # Exchange the code for tokens.
    payload = {
        "grant_type": "authorization_code",
        "client_id": ETSY_API_KEYSTRING,
        "redirect_uri": redirect,
        "code": code,
        "code_verifier": verifier,
    }
    try:
        resp = requests.post(TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"ERROR: token exchange request failed: {e}")
        return 2

    if resp.status_code != 200:
        print(f"ERROR: token exchange failed ({resp.status_code}). Body: {resp.text[:500]}")
        return 2

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("ERROR: token response did not include a refresh_token. Cannot persist.")
        return 2

    upsert_env_var("ETSY_REFRESH_TOKEN", refresh_token)
    print()
    print("SUCCESS: refresh token stored in .env (ETSY_REFRESH_TOKEN).")
    print("The daily load can now run unattended. Next step: confirm ETSY_SHOP_ID,")
    print("then run `python scripts/etsy_api.py --dry-run` and a first live load.")
    return 0


def refresh_access_token() -> str:
    """Exchange the stored refresh token for a fresh access token. Rotates and
    rewrites ETSY_REFRESH_TOKEN in .env if Etsy returns a new one. Raises
    AuthError on 400/401 (caller exits non-zero, prompting `authorize`)."""
    if not ETSY_REFRESH_TOKEN:
        raise AuthError("ETSY_REFRESH_TOKEN missing — run `authorize` once the app is active.")

    payload = {
        "grant_type": "refresh_token",
        "client_id": ETSY_API_KEYSTRING,
        "refresh_token": ETSY_REFRESH_TOKEN,
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code in (400, 401):
        raise AuthError(
            f"Refresh failed ({resp.status_code}); token likely expired/revoked. "
            f"Re-run `authorize`. Body: {resp.text[:300]}"
        )
    resp.raise_for_status()
    data = resp.json()
    access = data.get("access_token")
    if not access:
        raise AuthError("Refresh response missing access_token.")

    rotated = data.get("refresh_token")
    if rotated and rotated != ETSY_REFRESH_TOKEN:
        upsert_env_var("ETSY_REFRESH_TOKEN", rotated)
        log.info("Etsy rotated the refresh token; .env updated (90-day clock reset).")
    return access


class AuthError(Exception):
    """OAuth/token failure that should prompt a re-`authorize`."""


# ===========================================================================
# HTTP session with backoff
# ===========================================================================

class EtsyClient:
    """Thin Etsy v3 client: auth headers + paginated GET with backoff."""

    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": ETSY_API_KEYSTRING,
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with exponential backoff + jitter on 429/5xx, honoring retry-after.
        Returns parsed JSON. Raises on non-retryable errors / exhausted retries."""
        url = f"{API_BASE}{path}"
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 200:
                # Visibility into the per-app rate budget when present.
                remaining = resp.headers.get("x-remaining-this-second")
                if remaining is not None:
                    log.debug("rate x-remaining-this-second=%s path=%s", remaining, path)
                return resp.json()

            if resp.status_code in (401,):
                raise AuthError(
                    f"401 Unauthorized on {path}; access token rejected. "
                    "Re-run `authorize` if this persists."
                )

            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if not retryable or attempt > MAX_RETRIES:
                raise RuntimeError(
                    f"Etsy GET {path} failed: {resp.status_code} {resp.text[:300]}"
                )

            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = BACKOFF_BASE ** attempt
            else:
                delay = BACKOFF_BASE ** attempt
            delay += secrets.randbelow(1000) / 1000.0  # jitter 0..1s
            log.warning(
                "Etsy %s on %s (attempt %d/%d); backing off %.1fs",
                resp.status_code, path, attempt, MAX_RETRIES, delay,
            )
            time.sleep(delay)

    def paginate(self, path: str, params: dict | None = None):
        """Yield each `results` item across all pages (limit/offset/count)."""
        params = dict(params or {})
        params.setdefault("limit", PAGE_LIMIT)
        offset = 0
        seen = 0
        while True:
            params["offset"] = offset
            payload = self.get(path, params=params)
            results = payload.get("results", []) or []
            count = payload.get("count")
            for item in results:
                yield item
            seen += len(results)
            if not results or len(results) < params["limit"]:
                break
            if count is not None and seen >= count:
                break
            offset += params["limit"]
            time.sleep(INTER_REQUEST_SLEEP)


# ===========================================================================
# Shop ID resolution
# ===========================================================================

def resolve_shop_id(client: EtsyClient) -> str:
    """Return the numeric shop id. Prefer ETSY_SHOP_ID from .env; otherwise
    resolve from the authenticated user's shops on the first live run.

    TODO(brian): once resolved on the first live run, persist ETSY_SHOP_ID into
    .env so we never depend on the lookup again. We deliberately do NOT hardcode
    a shop id here.
    """
    if ETSY_SHOP_ID:
        return ETSY_SHOP_ID

    # getMe (/users/me) returns the authenticated user and includes shop_id
    # directly. Fall back to resolving via owner user_id only if absent.
    # (Lookup only runs live.)
    me = client.get("/users/me")
    shop_id = me.get("shop_id")
    if not shop_id:
        # fallback: resolve via owner user_id (getShopByOwnerUserId returns a single Shop)
        user_id = me.get("user_id")
        if user_id:
            shop = client.get(f"/users/{user_id}/shops")
            shop_id = shop.get("shop_id") if isinstance(shop, dict) else None
    if not shop_id:
        raise RuntimeError("Could not resolve shop_id from the authenticated user's shops.")
    log.info("Resolved shop_id=%s. TODO: persist ETSY_SHOP_ID=%s into .env.", shop_id, shop_id)
    return str(shop_id)


# ===========================================================================
# Window planning
# ===========================================================================

def plan_window(since: date | None, until: date | None, backfill_days: int) -> tuple[date, date]:
    today = date.today()
    if until is None:
        until = today
    if since is None:
        since = until - timedelta(days=backfill_days - 1)
    if since > until:
        raise ValueError(f"--since ({since}) is after --until ({until})")
    return since, until


def _epoch(d: date, end: bool = False) -> int:
    """UTC epoch seconds for the start (or end) of a date."""
    t = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if end:
        t = t + timedelta(days=1) - timedelta(seconds=1)
    return int(t.timestamp())


# ===========================================================================
# Transform: API records -> insert dicts
# ===========================================================================

def _money(node: dict | None) -> float | None:
    """Etsy Money is {amount, divisor, currency_code}. Return amount/divisor."""
    if not node:
        return None
    amount = node.get("amount")
    divisor = node.get("divisor") or 1
    if amount is None:
        return None
    return amount / divisor


def _ts_to_date(ts: int | None):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def receipt_to_order_row(r: dict, loaded_at: datetime) -> dict:
    """Map a getShopReceipts result -> raw.etsy_orders insert dict."""
    currency = (r.get("grandtotal") or {}).get("currency_code") or r.get("currency_code")
    name = r.get("name") or ""
    first = name.split(" ", 1)[0] if name else None
    last = name.split(" ", 1)[1] if name and " " in name else None
    return {
        "order_id": str(r.get("receipt_id")),
        "receipt_id": r.get("receipt_id"),
        "sale_date": _ts_to_date(r.get("created_timestamp")),
        "buyer_user_id": str(r.get("buyer_user_id")) if r.get("buyer_user_id") is not None else None,
        "full_name": name or None,
        "first_name": first,
        "last_name": last,
        "number_of_items": None,  # derived downstream from transactions if needed
        "date_shipped": _ts_to_date(
            (r.get("shipments") or [{}])[0].get("shipment_notification_timestamp")
        ),
        "street_1": (r.get("first_line") or None),
        "street_2": (r.get("second_line") or None),
        "ship_city": r.get("city"),
        "ship_state": r.get("state"),
        "ship_zipcode": r.get("zip"),
        "ship_country": r.get("country_iso"),
        "currency": currency,
        "order_value": _money(r.get("subtotal")),
        "discount_amount": _money(r.get("discount_amt")),
        "shipping": _money(r.get("total_shipping_cost")),
        "sales_tax": _money(r.get("total_tax_cost")),
        "order_total": _money(r.get("grandtotal")),
        "status": r.get("status"),
        "buyer": r.get("buyer_email") or None,
        "created_timestamp": r.get("created_timestamp"),
        "updated_timestamp": r.get("updated_timestamp"),
        "is_paid": r.get("is_paid"),
        "is_shipped": r.get("is_shipped"),
        "_api_loaded_at": loaded_at,
    }


def transaction_to_item_row(t: dict, receipt: dict, loaded_at: datetime) -> dict:
    """Map a receipt transaction -> raw.etsy_order_items insert dict."""
    variations = t.get("variations")
    return {
        "transaction_id": str(t.get("transaction_id")),
        "order_id": str(receipt.get("receipt_id")),
        "receipt_id": receipt.get("receipt_id"),
        "listing_id": str(t.get("listing_id")) if t.get("listing_id") is not None else None,
        "listing_id_num": t.get("listing_id"),
        "item_name": t.get("title"),
        "quantity": t.get("quantity"),
        "price": _money(t.get("price")),
        "currency": (t.get("price") or {}).get("currency_code"),
        "sku": t.get("sku"),
        "variations": json.dumps(variations) if variations else None,
        "sale_date": _ts_to_date(t.get("created_timestamp")),
        "date_paid": _ts_to_date(t.get("paid_timestamp")),
        "date_shipped": _ts_to_date(t.get("shipped_timestamp")),
        "created_timestamp": t.get("created_timestamp"),
        "_api_loaded_at": loaded_at,
    }


def listing_to_row(l: dict, loaded_at: datetime) -> dict:
    """Map a getListingsByShop result -> raw.etsy_listings insert dict."""
    tags = l.get("tags")
    materials = l.get("materials")
    return {
        "listing_id": l.get("listing_id"),
        "title": l.get("title"),
        "description": l.get("description"),
        "price": _money(l.get("price")),
        "currency_code": (l.get("price") or {}).get("currency_code"),
        "quantity": l.get("quantity"),
        "tags": ", ".join(tags) if isinstance(tags, list) else tags,
        "materials": ", ".join(materials) if isinstance(materials, list) else materials,
        "sku": ", ".join(l.get("skus")) if isinstance(l.get("skus"), list) else l.get("sku"),
        "state": l.get("state"),
        "created_timestamp": l.get("created_timestamp") or l.get("creation_timestamp"),
        "updated_timestamp": l.get("updated_timestamp") or l.get("last_modified_timestamp"),
        "url": l.get("url"),
        "_api_loaded_at": loaded_at,
    }


# ===========================================================================
# Postgres load (ID-keyed delete-reload under advisory lock)
# ===========================================================================

ADVISORY_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtext('raw.etsy_api:load:' || :lock_key))"
)

UPSERT_STATE_SQL = text(
    """
    INSERT INTO raw.etsy_api_load_state (entity, window_key, last_max_created,
                                         row_count, first_loaded_at, last_loaded_at, load_count)
    VALUES (:entity, :window_key, :last_max_created, :row_count, now(), now(), 1)
    ON CONFLICT (entity, window_key) DO UPDATE
       SET last_max_created = EXCLUDED.last_max_created,
           row_count        = EXCLUDED.row_count,
           last_loaded_at   = now(),
           load_count       = raw.etsy_api_load_state.load_count + 1
    """
)


def _insert_rows(conn, table: str, rows: list[dict]) -> None:
    """executemany-style insert; column set taken from the first row's keys."""
    if not rows:
        return
    cols = list(rows[0].keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    conn.execute(
        text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"),
        rows,
    )


def delete_reload(
    engine: Engine,
    *,
    entity: str,
    table: str,
    id_column: str,
    rows: list[dict],
    window_key: str,
    lock_key: str,
    last_max_created: int | None,
) -> int:
    """ID-keyed delete-reload for one entity/window inside a single transaction,
    serialized by an advisory lock. Returns the row count written."""
    ids = [r[id_column] for r in rows if r.get(id_column) is not None]
    with engine.begin() as conn:
        conn.execute(ADVISORY_LOCK_SQL, {"lock_key": lock_key})
        if ids:
            conn.execute(
                text(f"DELETE FROM {table} WHERE {id_column} = ANY(:ids)"),
                {"ids": ids},
            )
        _insert_rows(conn, table, rows)
        conn.execute(UPSERT_STATE_SQL, {
            "entity": entity,
            "window_key": window_key,
            "last_max_created": last_max_created,
            "row_count": len(rows),
        })
    return len(rows)


# ===========================================================================
# Entity loaders
# ===========================================================================

@dataclass
class EntityResult:
    entity: str
    status: str  # "loaded" | "failed"
    row_count: int = 0
    error: str | None = None


def load_receipts_and_items(
    engine: Engine, client: EtsyClient, shop_id: str,
    start: date, end: date, loaded_at: datetime,
) -> tuple[EntityResult, EntityResult]:
    """Pull getShopReceipts for the window (+ their transactions) and
    delete-reload raw.etsy_orders and raw.etsy_order_items."""
    window_key = f"{start.isoformat()}..{end.isoformat()}"
    order_rows: list[dict] = []
    item_rows: list[dict] = []
    max_created = None

    params = {
        "min_created": _epoch(start),
        "max_created": _epoch(end, end=True),
    }
    for receipt in client.paginate(f"/shops/{shop_id}/receipts", params=params):
        order_rows.append(receipt_to_order_row(receipt, loaded_at))
        ct = receipt.get("created_timestamp")
        if ct and (max_created is None or ct > max_created):
            max_created = ct
        # Transactions may be embedded; otherwise fetch them per receipt.
        transactions = receipt.get("transactions")
        if transactions is None:
            rid = receipt.get("receipt_id")
            payload = client.get(f"/shops/{shop_id}/receipts/{rid}/transactions")
            transactions = payload.get("results", []) or []
        for t in transactions:
            item_rows.append(transaction_to_item_row(t, receipt, loaded_at))

    orders_res = _safe_delete_reload(
        engine, entity="orders", table="raw.etsy_orders", id_column="order_id",
        rows=order_rows, window_key=window_key,
        lock_key=f"orders:{window_key}", last_max_created=max_created,
    )
    items_res = _safe_delete_reload(
        engine, entity="order_items", table="raw.etsy_order_items", id_column="transaction_id",
        rows=item_rows, window_key=window_key,
        lock_key=f"order_items:{window_key}", last_max_created=max_created,
    )
    return orders_res, items_res


def load_listings(
    engine: Engine, client: EtsyClient, shop_id: str, loaded_at: datetime,
) -> EntityResult:
    """Full refresh of raw.etsy_listings via getListingsByShop, populating
    listing_id. Pulls active + inactive/expired/draft/sold_out."""
    rows: list[dict] = []
    max_created = None
    # getListingsByShop supports a `state` filter; pull each state for completeness.
    for state in ("active", "inactive", "expired", "draft", "sold_out"):
        try:
            for listing in client.paginate(
                f"/shops/{shop_id}/listings", params={"state": state}
            ):
                rows.append(listing_to_row(listing, loaded_at))
                ct = listing.get("created_timestamp") or listing.get("creation_timestamp")
                if ct and (max_created is None or ct > max_created):
                    max_created = ct
        except RuntimeError as e:
            # Some states (e.g. draft) can 4xx on shops without them; log + continue.
            log.warning("Listings state=%s skipped: %s", state, e)

    return _safe_delete_reload(
        engine, entity="listings", table="raw.etsy_listings", id_column="listing_id",
        rows=rows, window_key="full",
        lock_key="listings:full", last_max_created=max_created,
    )


def _safe_delete_reload(engine, **kwargs) -> EntityResult:
    entity = kwargs["entity"]
    try:
        n = delete_reload(engine, **kwargs)
        log.info("Loaded %d rows into raw.etsy_%s (window=%s)",
                 n, entity if entity != "order_items" else "order_items", kwargs["window_key"])
        return EntityResult(entity=entity, status="loaded", row_count=n)
    except Exception as e:  # noqa: BLE001 — isolate per-entity failures
        log.exception("Load failed for entity=%s: %s", entity, e)
        return EntityResult(entity=entity, status="failed", error=str(e))


# ===========================================================================
# Dry run (NO live API calls)
# ===========================================================================

def check_postgres() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar()
        print(f"  Postgres         : {version.split(',')[0]}")
        for tbl in ("etsy_orders", "etsy_order_items", "etsy_listings", "etsy_api_load_state"):
            exists = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='raw' AND table_name=:t)"
            ), {"t": tbl}).scalar()
            print(f"  raw.{tbl:<19} : {'present' if exists else 'MISSING (run migration 006)'}")
        has_lid = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='raw' AND table_name='etsy_listings' AND column_name='listing_id')"
        )).scalar()
        print(f"  listings.listing_id: {'present' if has_lid else 'MISSING (run migration 006)'}")


def cmd_dry_run(start: date, end: date, backfill_days: int) -> int:
    print("=" * 72)
    print("etsy_api.py — DRY RUN (no live API calls)")
    print("=" * 72)
    print(f"Receipts window  : {start} -> {end}  ({(end - start).days + 1} day(s))")
    print(f"Backfill window  : {backfill_days} day(s)")
    print(f"Listings         : full refresh (all states)")
    print()

    # 1. .env key presence (names only; never print secret values).
    print("Checking .env credentials ...")
    print(f"  ETSY_API_KEYSTRING     : {'present' if ETSY_API_KEYSTRING else 'MISSING'}")
    print(f"  ETSY_SHARED_SECRET     : {'present' if os.getenv('ETSY_SHARED_SECRET') else 'MISSING'}")
    print(f"  ETSY_OAUTH_REDIRECT_URI: {ETSY_OAUTH_REDIRECT_URI}")
    print(f"  ETSY_SHOP_ID           : {ETSY_SHOP_ID if ETSY_SHOP_ID else 'unset (will resolve on first live run)'}")
    print(f"  ETSY_REFRESH_TOKEN     : {'present' if ETSY_REFRESH_TOKEN else 'ABSENT'}")
    print()

    # 2. DB reachability.
    print("Checking Postgres ...")
    try:
        check_postgres()
    except Exception as e:  # noqa: BLE001
        print(f"  Postgres check FAILED: {e}")
        return 1
    print()

    # 3. Authorization gate — the whole point of the pending-activation path.
    if not ETSY_API_KEYSTRING or not os.getenv("ETSY_SHARED_SECRET"):
        print("App credentials (keystring/secret) are not both present in .env.")
        print("Add them once Etsy issues them, then re-run.")
        return 0

    if not ETSY_REFRESH_TOKEN:
        print("-" * 72)
        print("APP PENDING ACTIVATION / NOT YET AUTHORIZED.")
        print("-" * 72)
        print("ETSY_API_KEYSTRING + ETSY_SHARED_SECRET are present, but no")
        print("ETSY_REFRESH_TOKEN is stored yet — so no live data can be pulled.")
        print()
        print("Once Etsy marks the app ACTIVE, run the one-time consent:")
        print("    python scripts/etsy_api.py authorize")
        print("That stores ETSY_REFRESH_TOKEN in .env; after that the daily load")
        print("runs unattended. No live API calls were made by this dry run.")
        return 0

    print("Credentials + refresh token present. A live load would now:")
    print(f"  - refresh the access token at {TOKEN_URL}")
    print(f"  - resolve shop_id ({'from .env' if ETSY_SHOP_ID else 'via /users/me lookup'})")
    print(f"  - GET /shops/<shop_id>/receipts  (min/max_created over the window) -> raw.etsy_orders")
    print(f"  - GET receipt transactions                                        -> raw.etsy_order_items")
    print(f"  - GET /shops/<shop_id>/listings  (all states)                     -> raw.etsy_listings")
    print("No live API calls were made by this dry run.")
    return 0


# ===========================================================================
# CLI
# ===========================================================================

def parse_date_arg(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_args():
    p = argparse.ArgumentParser(description="Load Etsy Open API v3 data into business.raw")
    p.add_argument(
        "command", nargs="?", default="load", choices=["load", "authorize"],
        help="'authorize' for one-time OAuth consent; default 'load' for the data run.",
    )
    p.add_argument("--since", type=parse_date_arg, help="Receipts window start (YYYY-MM-DD, inclusive)")
    p.add_argument("--until", type=parse_date_arg, help="Receipts window end (YYYY-MM-DD, inclusive)")
    p.add_argument(
        "--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS,
        help=f"Receipts re-pull window when --since omitted (default: {DEFAULT_BACKFILL_DAYS})",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Validate .env + DB + plan; make NO live API calls")
    p.add_argument("--verbose", action="store_true", help="Set log level to DEBUG")
    return p.parse_args()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    if args.command == "authorize":
        return cmd_authorize()

    start, end = plan_window(args.since, args.until, args.backfill_days)

    if args.dry_run:
        return cmd_dry_run(start, end, args.backfill_days)

    # ---- Real load path -----------------------------------------------------
    log.info("Starting Etsy API load: receipts window=%s -> %s (%d day(s)), listings=full",
             start, end, (end - start).days + 1)

    try:
        access_token = refresh_access_token()
    except AuthError as e:
        log.error("Auth failed: %s", e)
        return 2

    client = EtsyClient(access_token)

    try:
        shop_id = resolve_shop_id(client)
    except (AuthError, RuntimeError) as e:
        log.error("Shop ID resolution failed: %s", e)
        return 2

    loaded_at = datetime.now(tz=timezone.utc)
    results: list[EntityResult] = []

    try:
        orders_res, items_res = load_receipts_and_items(
            get_engine(), client, shop_id, start, end, loaded_at
        )
        results.extend([orders_res, items_res])
    except AuthError as e:
        log.error("Auth failed mid-run: %s", e)
        return 2

    try:
        results.append(load_listings(get_engine(), client, shop_id, loaded_at))
    except AuthError as e:
        log.error("Auth failed during listings: %s", e)
        return 2

    loaded = [r for r in results if r.status == "loaded"]
    failed = [r for r in results if r.status == "failed"]
    total_rows = sum(r.row_count for r in loaded)
    log.info("LOAD SUMMARY: %d entities, %d loaded, %d failed, %d total rows",
             len(results), len(loaded), len(failed), total_rows)
    for r in failed:
        log.error("Failed entity=%s err=%s", r.entity, r.error)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
