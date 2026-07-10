"""ENR Garage Inventory — mobile web app (FastAPI).

A mobile-first web layer over `inventory.garage_items` in the `business` DB,
mirroring the filament-intake app's shape (one dark-styled page per view, thin
request-parse/response-shape handlers, env-driven config).

Views / endpoints
-----------------
  GET  /                     -> lookup + add page (landing view = lookup).
  GET  /item/{idx}           -> the item page: view / edit / print / QR.
                                THIS is the URL each bin's label QR encodes —
                                scan a bin, land on its record, edit, reprint
                                (the QR loop from Brian's 2026-07-02 decision).
  GET  /api/health           -> liveness + row count.
  GET  /api/search?q=        -> trigram/ILIKE search over label_line1 +
                                label_line2 + media (the SOP's three lookup
                                fields, via the generated search_text) plus
                                loc_code prefix match.
  GET  /api/item/{idx}       -> one row + code-drift info + the FULL ancestor
                                chain (root-first breadcrumb) + DIRECT children.
  PUT  /api/item/{idx}/parent-> associate / re-parent / detach. Body
                                {"parent_idx": n} nests under n; {"parent_idx":
                                null} detaches to top level. Kept OUT of the
                                generic edit PUT (which skips None) so "clear the
                                parent" stays expressible; blocks self-parent and
                                cycles (API pre-check + DB trigger backstop).
  POST /api/items/preview    -> confirm-before-save: echoes the parsed fields,
                                the would-be loc_code letters, and a
                                NON-CONSUMING estimate of the next idx.
  POST /api/items            -> the actual insert: mints idx from
                                inventory.garage_items_idx_seq (starts 643) and
                                loc_code = first(room)+first(location)+idx.
  PUT  /api/item/{idx}       -> edit fields / retire. NEVER rewrites loc_code;
                                returns code_drift so the UI can warn that the
                                printed label no longer matches the rule.
  POST /api/item/{idx}/print -> print the bin label via the shared Label
                                Printing Service (labels.py). Degrades to a
                                clear message while the service/template is
                                not yet available.
  GET  /api/item/{idx}/qr.svg-> the item's QR (server-rendered SVG via segno),
                                encoding GARAGE_QR_BASE + /item/{idx}.

QR URL base (GARAGE_QR_BASE)
----------------------------
The QR must open on Brian's phone WHEREVER he scans it, so it encodes an
absolute origin, not a relative link. On the NAS this is the Tailscale-HTTPS
origin the container is served behind (same posture as filament-intake), e.g.
https://garage.<tailnet-host>.ts.net — set GARAGE_QR_BASE to that origin in the
compose. Local-dev default is http://127.0.0.1:8745. Labels printed with a
dev-origin QR would go stale; print only once the real origin is configured.

Run locally
-----------
  ./venv/bin/python -m uvicorn garage.webapp.app:app --host 127.0.0.1 --port 8745
then open  http://127.0.0.1:8745/
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.errors
import segno
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from garage import db
from garage.webapp import labels

STATIC_DIR = Path(__file__).resolve().parent / "static"

GARAGE_QR_BASE = os.environ.get(
    "GARAGE_QR_BASE", "http://127.0.0.1:8745").rstrip("/")

app = FastAPI(title="ENR Garage Inventory", version="1.0.0")

ITEM_FIELDS = ("idx, loc_code, room, location, label_line1, label_line2, "
               "media, former_location, qty, item_type, status, parent_idx, "
               "created_at, updated_at")

# Compact summary shape for the parent/child sub-objects (breadcrumb + Contains).
CHILD_FIELDS = "idx, loc_code, label_line1, label_line2, qty, status"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class NewItemBody(BaseModel):
    """Add form. Room + Location are required (they mint the loc_code)."""

    room: str = Field(..., min_length=1)
    location: str = Field(..., min_length=1)
    label_line1: Optional[str] = None
    label_line2: Optional[str] = None
    media: Optional[str] = None
    qty: Optional[int] = Field(None, ge=0)
    parent_idx: Optional[int] = Field(None, ge=1)


class EditItemBody(BaseModel):
    """Edit form. loc_code is NOT editable — it is printed on the bin."""

    room: Optional[str] = None
    location: Optional[str] = None
    label_line1: Optional[str] = None
    label_line2: Optional[str] = None
    media: Optional[str] = None
    qty: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(active|reserved|retired)$")


class PrintBody(BaseModel):
    copies: int = Field(1, ge=1, le=10)


class ParentBody(BaseModel):
    """Associate / re-parent / detach. null parent_idx = detach to top level."""

    parent_idx: Optional[int] = Field(None, ge=1)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row_out(row: dict) -> dict:
    out = dict(row)
    for k in ("created_at", "updated_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    # Code drift: what the rule WOULD mint from today's room/location vs the
    # stored (printed-on-the-bin) code. Never auto-fixed; surfaced for the UI.
    would_be = None
    if out.get("room") and out.get("location"):
        would_be = db.mint_loc_code(out["room"], out["location"], out["idx"])
    out["rule_code_now"] = would_be
    out["code_drift"] = bool(would_be and would_be != out["loc_code"])
    out["qr_url"] = f"{GARAGE_QR_BASE}/item/{out['idx']}"
    return out


def _fetch_item(cur, idx: int) -> dict:
    cur.execute(
        f"SELECT {ITEM_FIELDS} FROM {db.TABLE} WHERE idx = %s", (idx,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No item with idx {idx}.")
    return row


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
# These HTML pages carry all their CSS/JS INLINE, so `Cache-Control: no-cache`
# on the page response is enough to keep the whole app fresh (no separate JS
# bundle to version). no-cache — NOT no-store — means the browser must
# revalidate before reusing a cached copy, so the ETag/Last-Modified still yield
# a cheap 304 when nothing changed, but a redeploy can never be served stale
# (the iPhone-Safari stale-HTML bug from the 2026-07 combobox redeploy).
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/item/{idx}")
def item_page(idx: int) -> FileResponse:
    """The QR-target page. Static shell; JS loads /api/item/{idx}."""
    return FileResponse(STATIC_DIR / "item.html", headers=_NO_CACHE)


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    """The 'see everything at once' spreadsheet view (laptop review flow).

    Static shell; JS loads all rows from /api/items and renders a dense,
    sortable, filterable table. Structured as a dashboard shell so a summary
    strip / CSV export can slot in later without a rewrite (v1 = table only).
    """
    return FileResponse(STATIC_DIR / "dashboard.html", headers=_NO_CACHE)


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {db.TABLE}")
                n = cur.fetchone()[0]
        finally:
            conn.close()
        return {"ok": True, "items": n}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"DB unreachable: {exc}")


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(50, le=200),
           status: Optional[str] = Query(None)) -> dict:
    """Lookup: 'where is X?' across the SOP's three fields + loc-code prefix.

    ILIKE substring match (backed by the pg_trgm GIN index), ranked by trigram
    similarity so the closest content floats up. Optional ?status= narrows to
    active / reserved / retired (same handling as /api/items); anything else is
    ignored (all statuses). The lookup page defaults to status=active so
    retired bins drop out of 'where is X?' once items are retired.
    """
    params = {"sub": f"%{q}%", "prefix": f"{q}%", "q": q, "limit": limit}
    status_pred = ""
    if status in ("active", "reserved", "retired"):
        status_pred = "AND status = %(status)s"
        params["status"] = status
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT {ITEM_FIELDS}
                FROM {db.TABLE}
                WHERE (search_text ILIKE %(sub)s
                       OR loc_code ILIKE %(prefix)s)
                  {status_pred}
                ORDER BY similarity(search_text, %(q)s) DESC NULLS LAST, idx
                LIMIT %(limit)s
                """,
                params,
            )
            rows = [_row_out(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"q": q, "count": len(rows), "results": rows}


@app.get("/api/items")
def list_items(status: Optional[str] = Query(None)) -> dict:
    """All rows for the dashboard 'spreadsheet' view.

    Small dataset (~638 rows), so no pagination — return everything and let
    the client sort/filter. Optional ?status= narrows server-side (active /
    reserved / retired); anything else is ignored (client filters the rest).
    Ordered by room, location so the default view reads like the SOP sheet.
    """
    where, params = "", {}
    if status in ("active", "reserved", "retired"):
        where = "WHERE status = %(status)s"
        params["status"] = status
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT {ITEM_FIELDS}
                FROM {db.TABLE}
                {where}
                ORDER BY room NULLS LAST, location NULLS LAST, idx
                """,
                params,
            )
            rows = [_row_out(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"count": len(rows), "results": rows}


@app.get("/api/facets")
def facets() -> dict:
    """Distinct existing Room / Location values for the Add form's datalists.

    Feeds the native combobox (`<input list=…>` + `<datalist>`) so the Add view
    suggests existing values while still accepting a brand-new free-text value.
    Cheap: two DISTINCT scans, no row payload — just the two sorted lists,
    NULL/blank excluded.
    """
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT room FROM {db.TABLE} "
                f"WHERE room IS NOT NULL AND btrim(room) <> '' "
                f"ORDER BY room")
            rooms = [r[0] for r in cur.fetchall()]
            cur.execute(
                f"SELECT DISTINCT location FROM {db.TABLE} "
                f"WHERE location IS NOT NULL AND btrim(location) <> '' "
                f"ORDER BY location")
            locations = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    return {"rooms": rooms, "locations": locations}


@app.get("/api/item/{idx}")
def get_item(idx: int) -> dict:
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            row = _fetch_item(cur, idx)
            out = _row_out(row)

            # Full ancestor chain: root first, immediate parent last. `depth`
            # starts at 0 for the immediate parent and increases going UP;
            # ORDER BY depth DESC yields root-first. LIMIT 100 is a hard
            # defensive cap (the cycle trigger already ensures no true loop).
            # The current item itself is NOT included — the frontend appends it
            # as the final, non-link crumb.
            cur.execute(
                f"""
                WITH RECURSIVE anc AS (
                    SELECT g.idx, g.loc_code, g.label_line1, g.parent_idx,
                           0 AS depth
                    FROM {db.TABLE} g
                    WHERE g.idx = (SELECT parent_idx FROM {db.TABLE}
                                   WHERE idx = %(idx)s)
                    UNION ALL
                    SELECT p.idx, p.loc_code, p.label_line1, p.parent_idx,
                           anc.depth + 1
                    FROM {db.TABLE} p
                    JOIN anc ON p.idx = anc.parent_idx
                    WHERE anc.depth < 100
                )
                SELECT idx, loc_code, label_line1 FROM anc ORDER BY depth DESC
                """,
                {"idx": idx},
            )
            out["ancestors"] = [dict(a) for a in cur.fetchall()]  # [] for root

            # Direct children only — no recursion, one level down.
            cur.execute(f"SELECT {CHILD_FIELDS} FROM {db.TABLE} "
                        f"WHERE parent_idx = %s ORDER BY idx", (idx,))
            out["children"] = [dict(c) for c in cur.fetchall()]
            out["child_count"] = len(out["children"])
    finally:
        conn.close()
    return out


@app.post("/api/items/preview")
def preview_item(body: NewItemBody) -> dict:
    """Confirm-before-save (SOP requirement): echo the row as it would land.

    The idx shown is a PEEK at the sequence (non-consuming), so it is an
    estimate — the real number is minted inside the insert transaction and
    could differ if another add lands in between. The letters are exact.
    """
    parent = None
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                f"SELECT CASE WHEN is_called THEN last_value + 1 "
                f"ELSE last_value END AS next_idx FROM {db.IDX_SEQ}")
            next_idx = cur.fetchone()["next_idx"]
            # When creating a sub-item, resolve the parent so the confirm card
            # can show "Inside: {loc_code} — {line1}". A bad parent fails here,
            # before the user commits to save.
            if body.parent_idx is not None:
                cur.execute(
                    f"SELECT idx, loc_code, label_line1 FROM {db.TABLE} "
                    f"WHERE idx = %s", (body.parent_idx,))
                prow = cur.fetchone()
                if prow is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Parent item {body.parent_idx} does not exist.")
                parent = dict(prow)
    finally:
        conn.close()
    return {
        "room": body.room.strip(),
        "location": body.location.strip(),
        "label_line1": (body.label_line1 or "").strip() or None,
        "label_line2": (body.label_line2 or "").strip() or None,
        "media": (body.media or "").strip() or None,
        "qty": body.qty,
        "parent_idx": body.parent_idx,
        "parent": parent,
        "next_idx_estimate": next_idx,
        "loc_code_preview": db.mint_loc_code(body.room, body.location, next_idx),
        "note": "idx is assigned at save; the number can shift if another "
                "add lands first.",
    }


@app.post("/api/items")
def create_item(body: NewItemBody) -> dict:
    """The confirmed insert: mint idx from the sequence, then the loc_code."""
    room, location = body.room.strip(), body.location.strip()
    if not room or not location:
        raise HTTPException(status_code=400,
                            detail="Room and Location are required.")
    conn = db.connect()
    try:
        with conn:  # one transaction: nextval + insert commit together
            with db.dict_cursor(conn) as cur:
                cur.execute(f"SELECT nextval('{db.IDX_SEQ}')")
                idx = cur.fetchone()["nextval"]
                loc_code = db.mint_loc_code(room, location, idx)
                cur.execute(
                    f"""
                    INSERT INTO {db.TABLE}
                        (idx, loc_code, room, location, label_line1,
                         label_line2, media, qty, parent_idx)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {ITEM_FIELDS}
                    """,
                    (idx, loc_code, room, location,
                     (body.label_line1 or "").strip() or None,
                     (body.label_line2 or "").strip() or None,
                     (body.media or "").strip() or None,
                     body.qty, body.parent_idx),
                )
                row = cur.fetchone()
    except psycopg2.errors.ForeignKeyViolation:
        # A parent_idx pointing at a nonexistent item.
        raise HTTPException(
            status_code=400,
            detail=f"Parent item {body.parent_idx} does not exist.")
    except psycopg2.errors.CheckViolation as exc:
        # Self-parent / cycle backstop — unreachable for a freshly minted idx,
        # but caught so the DB constraint never leaks a 500.
        raise HTTPException(
            status_code=400,
            detail=f"That parent association is not allowed: {exc}")
    finally:
        conn.close()
    return {"ok": True, "item": _row_out(row)}


@app.put("/api/item/{idx}")
def edit_item(idx: int, body: EditItemBody) -> dict:
    """Edit contents / move / retire. loc_code is deliberately untouched:
    it is printed on the physical bin. The response's code_drift flag tells
    the UI to warn + offer a reprint instead of silently rewriting."""
    sets, vals = [], []
    for col in ("room", "location", "label_line1", "label_line2",
                "media", "qty", "status"):
        val = getattr(body, col)
        if val is not None:
            if isinstance(val, str):
                val = val.strip()
                if col in ("room", "location") and not val:
                    raise HTTPException(
                        status_code=400, detail=f"{col} cannot be blank.")
                val = val or None
            sets.append(f"{col} = %s")
            vals.append(val)
    if not sets:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    sets.append("updated_at = now()")

    conn = db.connect()
    try:
        with conn:
            with db.dict_cursor(conn) as cur:
                cur.execute(
                    f"UPDATE {db.TABLE} SET {', '.join(sets)} "
                    f"WHERE idx = %s RETURNING {ITEM_FIELDS}",
                    (*vals, idx),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail=f"No item with idx {idx}.")
    finally:
        conn.close()
    return {"ok": True, "item": _row_out(row)}


@app.put("/api/item/{idx}/parent")
def set_parent(idx: int, body: ParentBody) -> dict:
    """Associate / re-parent / detach — deliberately OUTSIDE the generic edit
    PUT (which skips None values, making "clear the parent" inexpressible).
    Body {"parent_idx": 123} sets/re-parents; {"parent_idx": null} detaches.

    Self-parent and cycles are blocked here (friendly 400) AND at the DB
    (self-parent CHECK, cycle trigger) — the trigger is the authoritative
    backstop against manual SQL or a concurrent-re-parent race.
    """
    conn = db.connect()
    try:
        with conn:  # rollback on any raise, incl. HTTPException
            with db.dict_cursor(conn) as cur:
                _fetch_item(cur, idx)  # 404 if the item doesn't exist
                new_parent = body.parent_idx
                if new_parent is not None:
                    if new_parent == idx:
                        raise HTTPException(
                            status_code=400,
                            detail="An item cannot be its own parent.")
                    cur.execute(f"SELECT idx, loc_code FROM {db.TABLE} "
                                f"WHERE idx = %s", (new_parent,))
                    prow = cur.fetchone()
                    if prow is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"No item with idx {new_parent} to use as "
                                   f"parent.")
                    # Friendly cycle pre-check: is `idx` an ancestor of
                    # new_parent (or new_parent itself)? The DB trigger is the
                    # authoritative backstop.
                    cur.execute(
                        f"""
                        WITH RECURSIVE anc AS (
                            SELECT g.idx, g.parent_idx FROM {db.TABLE} g
                            WHERE g.idx = %s
                            UNION ALL
                            SELECT g.idx, g.parent_idx FROM {db.TABLE} g
                            JOIN anc ON g.idx = anc.parent_idx
                        )
                        SELECT 1 FROM anc WHERE idx = %s LIMIT 1
                        """, (new_parent, idx))
                    if cur.fetchone():
                        raise HTTPException(
                            status_code=400,
                            detail=f"Can't nest here: {prow['loc_code']} is "
                                   f"already inside this item.")
                cur.execute(
                    f"UPDATE {db.TABLE} SET parent_idx = %s, updated_at = now() "
                    f"WHERE idx = %s RETURNING {ITEM_FIELDS}",
                    (new_parent, idx))
                row = cur.fetchone()
    except psycopg2.errors.CheckViolation as exc:
        raise HTTPException(
            status_code=400,
            detail=f"That nesting would create a loop: {exc}")
    finally:
        conn.close()
    return {"ok": True, "item": _row_out(row)}


@app.post("/api/item/{idx}/print")
def print_label(idx: int, body: PrintBody) -> dict:
    """Print this bin's label through the shared Label Printing Service.

    Until the service is deployed and the garage QL template registered, this
    returns a clear 503/501 message rather than pretending to print.
    """
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            row = _fetch_item(cur, idx)
    finally:
        conn.close()
    item = _row_out(row)
    try:
        result = labels.print_item_label(item, item["qr_url"],
                                         copies=body.copies)
    except labels.FieldValueError as exc:
        # A bin field value carries a byte the ordered-fill label rejects
        # ('^' / Tab) — a user input problem, so a clean 400, not a 5xx.
        raise HTTPException(status_code=400, detail=str(exc))
    except labels.LabelServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except labels.TemplateNotRegistered as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except labels.FieldMappingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "loc_code": item["loc_code"], "service": result}


@app.get("/api/item/{idx}/qr.svg")
def item_qr(idx: int) -> Response:
    """The item's QR (SVG, server-rendered — no CDN dependency).

    Encodes the absolute item URL under GARAGE_QR_BASE. 404s for a
    nonexistent idx so labels can't carry dead links.
    """
    conn = db.connect()
    try:
        with db.dict_cursor(conn) as cur:
            _fetch_item(cur, idx)
    finally:
        conn.close()
    qr = segno.make(f"{GARAGE_QR_BASE}/item/{idx}", error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=4, dark="#f3ead9", border=2, xmldecl=False)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
