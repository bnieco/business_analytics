"""ENR Filament Inventory — Stage B intake web app (FastAPI).

A thin, mobile-first web layer over the Stage-A connector
(`spoolman/connector.py`). It serves one page for iPhone Safari that scans (or
accepts a typed) manufacturer box barcode and logs physical spools into
Spoolman through the connector's Option 2 / Option 3 decision flow.

Endpoints
---------
  GET  /                  -> the intake page (static HTML/JS, html5-qrcode).
  GET  /api/lookup        -> ?barcode=...  : connector.lookup_barcode().
                            {found: bool, filament: {brand,material,color,...}}
  POST /api/intake        -> {barcode, roll_count, location, + (on miss) brand,
                            material, color, color_hex, diameter, spool_weight,
                            density, price}. Calls connector.intake(); returns
                            the path taken and the spool ids created.
  GET  /api/health        -> liveness + Spoolman version (sanity ping).

The web app holds NO intake logic of its own: it builds a `FilamentRecord`,
delegates to `connector.intake()`, and serializes the `IntakeResult`. Both
Option 2 (known barcode -> roll_count + location only) and Option 3 (unknown
barcode -> full attributes + auto-bind for next time) are handled by the
connector; this layer is request parsing + response shaping.

CAMERA / SECURE CONTEXT
-----------------------
`getUserMedia` (the camera) only runs in a *secure context*: HTTPS, or
http://localhost / http://127.0.0.1. So the camera scanner WORKS for local dev
on localhost with no certificate. Serving this to the iPhone over the LAN needs
a genuinely-trusted HTTPS cert — that is Stage C (gated on Brian's domain/DNS
decision), NOT part of this app. The typed-barcode fallback works everywhere.

Run locally
-----------
  ./venv/bin/python -m uvicorn spoolman.webapp.app:app --host 127.0.0.1 --port 8742
then open  http://127.0.0.1:8742/
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from spoolman import connector as C
from spoolman.webapp import ptouch

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ENR Filament Intake", version="1.0.0")

# A single shared client (thin requests.Session wrapper) for the process.
_client = C.SpoolmanClient()

# Label printer config — inline env, matching the docker-compose.nas.yml literals
# (CM does not interpolate ${VAR}). Defaults are the hardware-validated values.
PRINTER_IP = os.environ.get("PRINTER_IP", "10.0.0.200")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
PTOUCH_TEMPLATE_KEY = int(os.environ.get("PTOUCH_TEMPLATE_KEY", "99"))


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class IntakeBody(BaseModel):
    """Intake form submission.

    Option 2 (known barcode): only `barcode`, `roll_count`, `location` are used;
    the filament attributes are ignored because the mapping resolves them.
    Option 3 (unknown barcode): `brand` is required (the Spoolman vendor); the
    rest describe the new filament and are written once, then auto-bound.
    """

    barcode: str = Field(..., min_length=1)
    roll_count: int = Field(1, ge=1)
    location: Optional[str] = None

    # Option-3 attributes (ignored on an Option-2 hit)
    brand: Optional[str] = None
    material: Optional[str] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    diameter: float = 1.75
    spool_weight: Optional[float] = 1000.0
    density: Optional[float] = None
    price: Optional[float] = None

    # optional per-spool extras
    lot_nr: Optional[str] = None


class PrintLabelBody(BaseModel):
    """Print (or reprint) the QL-720NW label for one Spoolman spool id."""

    spool_id: int = Field(..., ge=1)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
# Mirror garage's mechanism: no-cache (NOT no-store) so the browser must
# revalidate before reusing a cached copy — the ETag/Last-Modified still yield a
# cheap 304 when nothing changed, but a redeploy can never be served stale (the
# iPhone-Safari stale-HTML bug). Matters more now the page is a PWA install
# (stale-shell risk); the inline CSS/JS means the one page response covers it.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    try:
        info = _client.info()
        return {"ok": True, "spoolman_version": info.get("version")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Spoolman unreachable: {exc}")


@app.get("/api/lookup")
def lookup(barcode: str = Query(..., min_length=1)) -> dict:
    """Resolve a scanned barcode against the ENR mapping table (the SSOT).

    On a hit, returns the denormalized brand/material/color so the page can show
    the operator what they're adding to before they pick a roll count.
    """
    try:
        barcode = C.validate_barcode(barcode)
    except C.InvalidBarcodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        # Orphan-aware resolve: a hit whose mapped filament was deleted in
        # Spoolman is cleaned up and returned as a miss, so the UI prompts for
        # entry instead of binding to a dead filament_id (Fix 4).
        row = C.resolve_mapping(barcode, _client)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"lookup failed: {exc}")
    if not row:
        return {"found": False, "barcode": barcode, "filament": None}
    return {
        "found": True,
        "barcode": barcode,
        "filament": {
            "spoolman_filament_id": row["spoolman_filament_id"],
            "brand": row.get("brand"),
            "material": row.get("material"),
            "color": row.get("color"),
            "source": row.get("source"),
        },
    }


@app.post("/api/intake")
def intake(body: IntakeBody) -> dict:
    """Log `roll_count` spools for a scanned box, via the connector's flow.

    The connector decides Option 2 vs Option 3 by looking the barcode up; this
    handler just forwards a `FilamentRecord` plus roll_count + location. On an
    Option-3 miss the barcode is auto-bound so the next scan is an Option-2 hit.
    """
    try:
        barcode = C.validate_barcode(body.barcode)
    except C.InvalidBarcodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record = C.FilamentRecord(
        barcode=barcode,
        brand=(body.brand or None),
        material=(body.material or None),
        color=(body.color or None),
        color_hex=(body.color_hex or None),
        diameter=body.diameter,
        spool_weight=body.spool_weight,
        density=body.density,
        price=body.price,
    )
    try:
        result = C.intake(
            record,
            roll_count=body.roll_count,
            location=(body.location or "").strip() or None,
            client=_client,
            lot_nr=(body.lot_nr or None),
            price=body.price,
        )
    except ValueError as exc:
        # missing brand on an Option-3 miss, bad roll_count, etc. -> 400
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"intake failed: {exc}")

    return {
        "ok": True,
        "path": result.path,  # "option2" | "option3"
        "barcode": result.barcode,
        "vendor_id": result.vendor_id,
        "filament_id": result.filament_id,
        "spool_ids": result.spool_ids,
        "spools_created": len(result.spool_ids),
        "mapping_created": result.mapping_created,
        "vendor_created": result.vendor_created,
        "filament_created": result.filament_created,
    }


@app.post("/api/print_label")
def print_label(body: PrintLabelBody) -> dict:
    """Print the filament label for `spool_id` on the QL-720NW.

    Fetches the spool from Spoolman, assembles the hardware-validated P-touch
    Template byte stream (via `ptouch.print_spool_label`), and sends it over raw
    TCP to the printer. Returns `{ok, message}` on success. If the printer is
    unreachable, returns HTTP 502 with the reachability message; an unknown
    spool id returns HTTP 404.

    NOTE (Phase 3 open item): the proven test prints were sent from Brian's Mac.
    The container -> 10.0.0.200:9100 path is exercised for the first time here;
    if this 502s with a reachability message from inside the container, that is a
    Docker-network-to-LAN routing issue to diagnose, not a code bug.
    """
    try:
        ptouch.print_spool_label(
            body.spool_id,
            printer_ip=PRINTER_IP,
            port=PRINTER_PORT,
            template_key=PTOUCH_TEMPLATE_KEY,
            client=_client,
        )
    except ptouch.PrinterUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 502
        if code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Spool {body.spool_id} not found in Spoolman.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Spoolman error fetching spool {body.spool_id}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"print failed: {exc}")

    return {
        "ok": True,
        "message": f"Label sent to printer for spool {body.spool_id}.",
    }


# Serve any additional static assets (none required today; the page uses a CDN
# for html5-qrcode, but this keeps room for vendoring the bundle in Stage D).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
