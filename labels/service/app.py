"""Label Printing Service v1 — FastAPI microservice over the shared engine.

The SECOND front door to the same `labels.engine` that filament-intake calls
in-process (the locked hybrid architecture): this service exists for the OTHER
consumers — the garage-inventory app first, then kitchen, then any caller that
can POST JSON. One container owns the printer + template registries so they
cannot drift across apps.

Endpoints
---------
  POST /print       {template, printer?, fields, copies=1} -> send a label.
  GET  /templates   -> the templates registry (name, fields, key, media, ...);
                       everything a UI needs to render a template selector.
  GET  /printers    -> the printers registry.
  GET  /api/health  -> liveness + registry counts (mirrors the filament app).

Validation contract (scope §2 + Argus fixes, decisions locked 2026-07-02)
-------------------------------------------------------------------------
  * unknown `template` name                       -> 400
  * unknown `printer` name (when supplied)        -> 400
  * `printer` != the template's registered printer -> 400 (mismatch)
  * UNKNOWN field names                           -> 400 ALWAYS (typo trap —
    an unknown ^ON name silently no-ops on the printer) [Argus M4]
  * MISSING fields -> defaulted to the template's per-template `placeholder`
    (default "None") and reported back in `defaulted_fields`; each app owns
    its own empties [Argus M4]
  * `copies` outside 1..20                        -> 400
  * printer dialect not implemented in v1         -> 400 (only
    `ptouch-template`; `raster` is the deferred PT-P750W phase, Argus B1)
  * printer unreachable                           -> 502

Copies semantics (Argus m2)
---------------------------
`copies: N` sends the COMPLETE command stream (ESC i a 03 ... ^FF) N times,
sequentially, each send its own TCP connection. Every ^FF feeds + prints one
label, so N sends = N identical labels. There is no printer-side copies
command in the validated grammar. All N sends happen under the printer's
lock, so another caller cannot interleave between copies.

Concurrency (Argus M3)
----------------------
One in-process `threading.Lock` PER PRINTER serializes jobs: Brother network
printers accept one 9100 connection at a time, and two simultaneous callers
would otherwise collide (refused connection or interleaved garbage). The
endpoint is deliberately `def` (not `async def`), so FastAPI runs it in the
threadpool and a held lock never blocks the event loop.

Observability (Argus m3, M1)
----------------------------
EVERY print attempt logs one structured line: template name + template_key
(so key drift after a re-transfer is diagnosable from the log — see the
drift warning in templates.yaml), printer name/address, byte count, copies,
and ok/error. Remember send-success is NOT print-success on raw 9100: a
stale/empty template key is a silent no-op at the printer.

LABELS_DRY_RUN=1 makes /print assemble + validate + log WITHOUT opening a
socket (response carries `"dry_run": true`) — for local testing and for
verifying a fresh container before letting it touch the printer.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from labels import engine
from labels.registry import IMPLEMENTED_DIALECTS, Registry

log = logging.getLogger("label-service")
if not log.handlers:  # uvicorn configures root logging; this is the fallback
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

SERVICE_VERSION = "1.0.0"
COPIES_MIN, COPIES_MAX = 1, 20

DRY_RUN = os.environ.get("LABELS_DRY_RUN", "0").lower() in ("1", "true", "yes")

app = FastAPI(title="ENR Label Printing Service", version=SERVICE_VERSION)

# Registries load once at startup; a config change means a container restart
# (YAML is repo-versioned config, not runtime state — scope Q3 decision).
REGISTRY: Registry = Registry.load()

# Argus M3: one lock per printer name, created up front from the registry.
_PRINTER_LOCKS: dict[str, threading.Lock] = {
    name: threading.Lock() for name in REGISTRY.printers
}


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #
class PrintBody(BaseModel):
    """POST /print — print `copies` labels of `template` with `fields`."""

    template: str = Field(..., min_length=1)
    printer: Optional[str] = None  # optional; must match the template's printer
    fields: dict[str, str] = Field(default_factory=dict)
    copies: int = 1


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "label-service",
        "version": SERVICE_VERSION,
        "dialects": sorted(IMPLEMENTED_DIALECTS),
        "printers": len(REGISTRY.printers),
        "templates": len(REGISTRY.templates),
        "dry_run": DRY_RUN,
    }


@app.get("/templates")
def templates() -> list[dict]:
    """The template selector's data source: name + expected field names, etc."""
    return [t.as_dict() for t in REGISTRY.templates.values()]


@app.get("/printers")
def printers() -> list[dict]:
    return [p.as_dict() for p in REGISTRY.printers.values()]


# --------------------------------------------------------------------------- #
# Print
# --------------------------------------------------------------------------- #
@app.post("/print")
def print_label(body: PrintBody) -> dict:
    # -- resolve + validate template ---------------------------------------- #
    template = REGISTRY.template(body.template)
    if template is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown template {body.template!r} "
                   f"(known: {sorted(REGISTRY.templates)})",
        )

    # -- resolve + validate printer ----------------------------------------- #
    if body.printer is not None:
        if REGISTRY.printer(body.printer) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown printer {body.printer!r} "
                       f"(known: {sorted(REGISTRY.printers)})",
            )
        if body.printer != template.printer:
            raise HTTPException(
                status_code=400,
                detail=f"template {template.name!r} is registered to printer "
                       f"{template.printer!r}, not {body.printer!r}",
            )
    printer = REGISTRY.printer(template.printer)
    assert printer is not None  # registry cross-validates at load

    if printer.dialect not in IMPLEMENTED_DIALECTS:
        raise HTTPException(
            status_code=400,
            detail=f"printer {printer.name!r} uses dialect {printer.dialect!r}, "
                   f"which is not implemented in v1 (only: "
                   f"{sorted(IMPLEMENTED_DIALECTS)}). The raster/PT-P750W path "
                   f"is a deferred phase.",
        )

    # -- validate fields (Argus M4: unknown -> 400 always; missing -> placeholder)
    declared = set(template.objects)
    unknown = sorted(set(body.fields) - declared)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown field name(s) for template {template.name!r}: "
                   f"{unknown}. Declared fields: {list(template.objects)}",
        )
    defaulted = sorted(declared - set(body.fields))
    fields = {name: body.fields.get(name, template.placeholder)
              for name in template.objects}

    # -- validate copies ----------------------------------------------------- #
    if not (COPIES_MIN <= body.copies <= COPIES_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"copies must be {COPIES_MIN}..{COPIES_MAX}, got {body.copies}",
        )

    # -- assemble ------------------------------------------------------------ #
    cmd = engine.build_command(fields, template.template_key, template.objects)

    # -- send (serialized per printer; Argus M3) ------------------------------ #
    # Log line carries template name + key so on-printer key drift after a
    # re-transfer is diagnosable (Argus M1; see templates.yaml warning).
    attempt = (
        f"template={template.name} key={template.template_key:03d} "
        f"printer={printer.name} addr={printer.ip}:{printer.port} "
        f"bytes={len(cmd)} copies={body.copies}"
    )
    if DRY_RUN:
        log.info("print DRY-RUN ok %s", attempt)
        return {
            "ok": True,
            "dry_run": True,
            "printer": printer.name,
            "template": template.name,
            "template_key": template.template_key,
            "copies": body.copies,
            "bytes": len(cmd),
            "defaulted_fields": defaulted,
        }

    lock = _PRINTER_LOCKS[printer.name]
    with lock:
        try:
            # copies semantics: the full stream N times, one connection each.
            for _ in range(body.copies):
                engine.send_to_printer(cmd, printer.ip, printer.port)
        except engine.PrinterUnreachable as exc:
            log.error("print FAILED %s error=%s", attempt, exc)
            raise HTTPException(status_code=502, detail=str(exc))
    log.info("print ok %s defaulted=%s", attempt, defaulted or "-")

    return {
        "ok": True,
        "printer": printer.name,
        "template": template.name,
        "template_key": template.template_key,
        "copies": body.copies,
        "bytes": len(cmd),
        "defaulted_fields": defaulted,
    }
