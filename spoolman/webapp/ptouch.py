"""ENR Filament label-print glue (Phase 3 / Label Service Phase S).

WHAT THIS MODULE IS NOW (Phase S extraction, 2026-07-02)
--------------------------------------------------------
The generic print ENGINE — P-touch Template byte assembly, the raw TCP 9100
transport, the command-grammar constants, and `PrinterUnreachable` — moved to
the shared, app-agnostic `labels.engine` package (Label Printing Service v1).
This module keeps everything FILAMENT: the Spoolman fetch, the record -> six-
field mapping, the field_key <-> object_name pairing, and the one-call
orchestration. Its PUBLIC API IS UNCHANGED — `app.py` and `test_print_label.py`
import the same names and get byte-identical output (regression-verified
against the pre-extraction stream for the live spools).

The filament app deliberately calls the engine IN-PROCESS (the locked hybrid
architecture, Argus B2): no HTTP hop, no dependency on the label-service
container, zero regression to the shipped "Print label" button. Other apps use
the `labels.service` HTTP API instead — same engine, two front doors.

THE PRINT CHAIN (all four links must be healthy)
------------------------------------------------
  Spoolman API (data) -> this module (field mapping) -> labels.engine
  (P-touch Template command) -> raw TCP 9100 (transport) -> QL-720NW with the
  "filament" template transferred at Key Assign 99 (hardware).

The full byte-grammar documentation lives in `labels/engine.py` (authoritative)
and the Template Spec vault doc.

DATA FETCH vs. QR TARGET (two different Spoolman URLs — do not conflate)
-----------------------------------------------------------------------
The label's DATA is fetched through the connector's env-driven `SpoolmanClient`,
so inside the NAS container it hits the peer `http://spoolman:8000` and on Brian's
Mac it hits `http://10.0.0.16:7912` (the connector default). But the QR code that
gets PRINTED onto the label must encode the LAN-reachable Spoolman URL the phone
opens when it scans — `http://10.0.0.16:7912/spool/show/{id}` — NOT the
container-internal `spoolman:8000` address. So `SPOOLMAN_QR_BASE` is a separate
constant (overridable via `SPOOLMAN_PUBLIC_URL`) and defaults to the validated LAN
address regardless of where the container talks to Spoolman.
"""
from __future__ import annotations

import os
from typing import Optional

from spoolman import connector as C

from labels import engine as _engine

# Re-exported engine names — the public API of this module is unchanged.
# (test_print_label.py and app.py import these from HERE.)
from labels.engine import (  # noqa: F401  (re-exports)
    ESC_AT,
    ESC_IA_TEMPLATE,
    SOCKET_TIMEOUT,
    TOK_DI,
    TOK_FF,
    TOK_II,
    TOK_ON,
    TOK_TS,
    PrinterUnreachable,
    send_to_printer,
)

# --------------------------------------------------------------------------- #
# Constants (from the hardware-validated Template Spec — authoritative)
# --------------------------------------------------------------------------- #
DEFAULT_PRINTER_IP = os.environ.get("PRINTER_IP", "10.0.0.200")
DEFAULT_PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
DEFAULT_TEMPLATE_KEY = int(os.environ.get("PTOUCH_TEMPLATE_KEY", "99"))

# QR target = the LAN address the phone opens on scan (see module docstring).
SPOOLMAN_QR_BASE = (
    os.environ.get("SPOOLMAN_PUBLIC_URL", "http://10.0.0.16:7912").rstrip("/")
    + "/spool/show"
)

# Brian's decision (2026-07-01): for an empty field, print the literal string
# "None" on the label — NOT a blank object and NOT the old "(?)" placeholder.
# This is the FILAMENT app's empty-field policy; the shared engine has none
# (each app owns its own empties — Argus M4).
EMPTY = "None"

# Template object names in print order. (field_key, object_name); field_key is
# resolved by build_fields(). Names are exact + case-sensitive. This pairing is
# the filament app's field_key <-> object_name map (the Argus M2 seam) — the
# shared engine and the service registry deal in object names only.
OBJECTS: list[tuple[str, str]] = [
    ("qr_url", "QR_URL0001"),
    ("brand", "TXT_BRAND0002"),
    ("material", "TXT_MATERIAL0003"),
    ("color", "TXT_COLOR0004"),
    ("location", "TXT_LOCATION0005"),
    ("spool_id", "TXT_SPOOLID0006"),
]

# The ordered object-name list the engine consumes (derived, single source).
OBJECT_NAMES: list[str] = [obj_name for _field_key, obj_name in OBJECTS]


# --------------------------------------------------------------------------- #
# Spoolman fetch + field mapping (FILAMENT-SPECIFIC — the only copy)
# --------------------------------------------------------------------------- #
def fetch_spool(spool_id: int, client: Optional[C.SpoolmanClient] = None) -> dict:
    """GET the spool record (with embedded filament + vendor) from Spoolman.

    Uses the connector's env-driven `SpoolmanClient`, so this resolves to the
    peer `spoolman:8000` inside the container and `10.0.0.16:7912` on the Mac.
    Raises `requests.HTTPError` on a 404 (unknown spool id) or other HTTP error.
    """
    client = client or C.SpoolmanClient()
    return client.get_spool(spool_id)


def _derive_color(fil_name: str, material: str) -> Optional[str]:
    """Best-effort color name.

    Bambu spools carry no discrete color field; the color lives in the filament
    name (e.g. "PLA Galaxy Purple" with material "PLA Galaxy" -> "Purple";
    "PLA Silk+ Rose Gold" / "PLA Silk+" -> "Rose Gold"). If the name starts with
    the material string, the remainder is the color; else fall back to the full
    name. Returns None if nothing usable is found.
    """
    name = (fil_name or "").strip()
    mat = (material or "").strip()
    if not name:
        return None
    if mat and name.lower().startswith(mat.lower()):
        remainder = name[len(mat):].strip()
        if remainder:
            return remainder
    return name  # no clean split — show the whole name rather than nothing


def _text(value: Optional[str]) -> str:
    """Coerce a Spoolman field to printable text, or the literal EMPTY sentinel."""
    text = (value or "").strip() if isinstance(value, str) else (value or "")
    return str(text) if text else EMPTY


def build_fields(spool: dict, spool_id: int) -> tuple[dict[str, str], list[str]]:
    """Map the Spoolman record to the six label field values.

    Returns (fields, warnings). Field mapping, as validated on hardware:
      brand    <- vendor.name
      material <- filament.material
      color    <- derived from filament.name minus the material prefix
      location <- spool.location, or the literal "None" when empty (Brian's call)
      spool_id <- "Spool #{id}" text
      qr_url   <- {SPOOLMAN_QR_BASE}/{id}  (LAN address, printed into the QR)
    Any empty field is filled with the literal EMPTY ("None") and a warning is
    recorded; this never crashes on a missing field.
    """
    warnings: list[str] = []
    fil = spool.get("filament") or {}
    vendor = fil.get("vendor") or {}

    def field(value: Optional[str], label: str) -> str:
        out = _text(value)
        if out == EMPTY:
            warnings.append(f"{label} empty in Spoolman -> printing literal {EMPTY!r}")
        return out

    fields = {
        "qr_url": f"{SPOOLMAN_QR_BASE}/{spool_id}",
        "brand": field(vendor.get("name"), "brand/vendor"),
        "material": field(fil.get("material"), "material"),
        "color": field(_derive_color(fil.get("name", ""), fil.get("material", "")), "color"),
        # location lives on the SPOOL, not the filament; often unset until shelved.
        "location": field(spool.get("location"), "location"),
        "spool_id": f"Spool #{spool_id}",
    }
    return fields, warnings


# --------------------------------------------------------------------------- #
# Byte assembly — thin wrapper over the shared engine (same signature, same bytes)
# --------------------------------------------------------------------------- #
def build_command(fields: dict[str, str], key: int) -> bytes:
    """Assemble the P-touch Template byte string for the filament label.

    Same public signature and byte-identical output as the pre-extraction
    version: `fields` is keyed by the filament field_keys (`qr_url`, `brand`,
    ...); this wrapper translates them to the template's object names via
    OBJECTS and delegates to the shared `labels.engine.build_command`.
    """
    by_object_name = {obj_name: fields[field_key] for field_key, obj_name in OBJECTS}
    return _engine.build_command(by_object_name, key, OBJECT_NAMES)


# --------------------------------------------------------------------------- #
# High-level: the one call the app + CLI make
# --------------------------------------------------------------------------- #
def print_spool_label(
    spool_id: int,
    *,
    printer_ip: str,
    port: int = DEFAULT_PRINTER_PORT,
    template_key: int = DEFAULT_TEMPLATE_KEY,
    client: Optional[C.SpoolmanClient] = None,
) -> None:
    """Fetch a spool, assemble the validated label command, and send it.

    Fetches the spool's fields through the connector, maps them to the six label
    objects, assembles the P-touch Template byte stream (with the required
    `ESC i a 03` prefix), then opens a socket to `printer_ip:port` and sends it.
    Raises `PrinterUnreachable` on a socket failure and `requests.HTTPError` if
    the spool id does not resolve in Spoolman.
    """
    spool = fetch_spool(spool_id, client=client)
    fields, _warnings = build_fields(spool, spool_id)
    cmd = build_command(fields, template_key)
    send_to_printer(cmd, printer_ip, port)
