"""ENR Filament label-print module (Phase 3) — the single source of truth.

This is the promoted, production version of the P-touch Template print logic that
`test_print_label.py` validated on the Brother QL-720NW hardware on 2026-07-01.
The command-builder and the Spoolman-record -> label-field mapping live HERE and
nowhere else: both the FastAPI "Print label" endpoint (`webapp/app.py`) and the
standalone CLI (`test_print_label.py`) import from this module, so the validated
byte stream can never drift between two copies.

THE PRINT CHAIN (all four links must be healthy)
------------------------------------------------
  Spoolman API (data) -> this module (P-touch Template command) ->
  raw TCP 9100 (transport) -> QL-720NW with the "filament" template transferred
  at Key Assign 99 (hardware).

P-TOUCH TEMPLATE COMMAND GRAMMAR (bytes)
----------------------------------------
  ESC i a 03       1B 69 61 03                switch to P-touch Template command
                                              mode for this job — MUST be first;
                                              without it the QL-720NW silently
                                              no-ops over 9100 (confirmed on
                                              hardware 2026-07-01)
  ESC @            1B 40                      initialize
  ^II              5E 49 49                   template-mode init
  ^TS{nnn}         5E 54 53 + 3 ASCII digits  select template by key, ZERO-PADDED
                                              (key 99 -> "099" -> 30 39 39)
  per object:
    ^ON{name}\x00  5E 4F 4E + name + 00       address object BY NAME (null-term)
    ^DI{lo}{hi}{d} 5E 44 49 + len16LE + data  replace that object's data;
                                              len = len(data).to_bytes(2,'little')
  ^FF              5E 46 46                   feed + PRINT (the trigger)

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
import socket
from typing import Optional

from spoolman import connector as C

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

SOCKET_TIMEOUT = 5.0   # printer connect/send

# Brian's decision (2026-07-01): for an empty field, print the literal string
# "None" on the label — NOT a blank object and NOT the old "(?)" placeholder.
EMPTY = "None"

# Per-job mode switch. `ESC i a 03` (1B 69 61 03) dynamically switches the
# printer into P-touch Template command mode for THIS job. It MUST be the first
# command in the stream, before ESC @. Without it the QL-720NW silently accepts
# the bytes over raw TCP 9100 and prints nothing — no error, nothing ejected.
# Confirmed on hardware 2026-07-01: adding this prefix is what turned the first
# (silent no-op) attempt into a printed label.
ESC_IA_TEMPLATE = b"\x1b\x69\x61\x03"

# P-touch Template literal tokens (ASCII; '^' = 0x5E)
ESC_AT = b"\x1b\x40"
TOK_II = b"^II"
TOK_TS = b"^TS"
TOK_ON = b"^ON"
TOK_DI = b"^DI"
TOK_FF = b"^FF"

# Template object names in print order. (field_key, object_name); field_key is
# resolved by build_fields(). Names are exact + case-sensitive.
OBJECTS: list[tuple[str, str]] = [
    ("qr_url", "QR_URL0001"),
    ("brand", "TXT_BRAND0002"),
    ("material", "TXT_MATERIAL0003"),
    ("color", "TXT_COLOR0004"),
    ("location", "TXT_LOCATION0005"),
    ("spool_id", "TXT_SPOOLID0006"),
]


class PrinterUnreachable(RuntimeError):
    """Raised when the printer cannot be reached over TCP (socket failure)."""


# --------------------------------------------------------------------------- #
# Spoolman fetch + field mapping (SHARED — the only copy)
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
# P-touch Template byte assembly (SHARED — the only copy)
# --------------------------------------------------------------------------- #
def build_command(fields: dict[str, str], key: int) -> bytes:
    """Assemble the exact P-touch Template byte string.

    ESC i a 03  ESC @  ^II  ^TS{key:03d}  then per object  ^ON{name}\\x00
    ^DI{len16LE}{data}  then ^FF. Objects are addressed by name and emitted in
    OBJECTS order. The leading ESC i a 03 switches the printer into P-touch
    Template command mode for the job — without it the printer no-ops silently
    over 9100.
    """
    if not (0 <= key <= 999):
        raise ValueError("template key must be a 3-digit value (0..999)")

    out = bytearray()
    out += ESC_IA_TEMPLATE  # first: switch to P-touch Template command mode
    out += ESC_AT
    out += TOK_II
    out += TOK_TS + f"{key:03d}".encode("ascii")  # zero-padded 3 digits

    for field_key, obj_name in OBJECTS:
        data = fields[field_key].encode("utf-8")
        out += TOK_ON + obj_name.encode("ascii") + b"\x00"
        out += TOK_DI + len(data).to_bytes(2, "little") + data

    out += TOK_FF
    return bytes(out)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def send_to_printer(cmd: bytes, ip: str, port: int, timeout: float = SOCKET_TIMEOUT) -> None:
    """Open a raw TCP socket to the printer, send the command bytes, close.

    Raises `PrinterUnreachable` with a short, operator-facing message on any
    network problem so an unreachable printer reads as a transport issue rather
    than a bare stack trace.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(cmd)
    except (socket.timeout, TimeoutError) as exc:
        raise PrinterUnreachable(
            f"could not reach printer at {ip}:{port}: timed out after {timeout:.0f}s "
            f"(is it powered on and on the network?)"
        ) from exc
    except OSError as exc:
        raise PrinterUnreachable(
            f"could not reach printer at {ip}:{port}: {exc}"
        ) from exc


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
