"""ENR Filament Inventory — Phase 3 label-print CHAIN VALIDATOR (CLI).

WHAT THIS IS
------------
A standalone command-line exerciser for the ENTIRE label-print chain with ONE
test label. It:

  1. reads a real spool's fields from Spoolman,
  2. assembles the exact P-touch Template byte string that drives the
     Brother QL-720NW, and
  3. either dumps that byte string for eyeballing (``--dry-run``) or sends it to
     the printer over a raw TCP socket (default).

SINGLE SOURCE OF TRUTH (2026-07-01 refactor)
--------------------------------------------
The command-builder and the Spoolman-record -> label-field mapping this script
validated now live in ``spoolman/webapp/ptouch.py`` and are imported below —
they are NOT duplicated here. The FastAPI "Print label" endpoint imports the
SAME functions, so this CLI and the app can never drift. This file keeps only
the CLI: argument parsing, the annotated hex dump (presentation), and exit-code
handling. See ``ptouch.py`` for the byte grammar and field mapping.

THE PRINT CHAIN (all four links must be healthy)
------------------------------------------------
  Spoolman API (data) -> ptouch (P-touch Template command) ->
  raw TCP 9100 (transport) -> QL-720NW with the "filament" template transferred
  at Key Assign 99 (hardware).

RUN COMMANDS
------------
  Dry-run (assemble + hex-dump ONLY, no socket) -- do this FIRST:
      cd ~/PycharmProjects/business_analytics
      ./venv/bin/python -m spoolman.test_print_label 22 --dry-run

  Live print (opens TCP to the printer and sends the bytes):
      cd ~/PycharmProjects/business_analytics
      ./venv/bin/python -m spoolman.test_print_label 22

NOTE ON SPOOL IDS: Brian's two real cataloged spools are id 22 (PLA Galaxy
Purple) and id 23 (PLA Silk+ Rose Gold, location "Cart") as of 2026-07-01.
Ids 1/2 return 404.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

import requests  # to classify Spoolman HTTP errors for a friendly message

from spoolman.webapp import ptouch
from spoolman.webapp.ptouch import (
    ESC_AT,
    ESC_IA_TEMPLATE,
    OBJECTS,
    TOK_DI,
    TOK_FF,
    TOK_II,
    TOK_ON,
    TOK_TS,
    PrinterUnreachable,
)


# --------------------------------------------------------------------------- #
# Presentation (dry-run only — the byte assembly itself lives in ptouch)
# --------------------------------------------------------------------------- #
def _hexline(chunk: bytes) -> str:
    return " ".join(f"{b:02X}" for b in chunk)


def _printable(chunk: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)


def annotated_dump(fields: dict[str, str], key: int, cmd: bytes) -> str:
    """Human-readable, annotated breakdown of the command byte string."""
    lines: list[str] = []
    lines.append("Decoded field values")
    lines.append("--------------------")
    for field_key, obj_name in OBJECTS:
        lines.append(f"  {obj_name:<16} <- {field_key:<9} = {fields[field_key]!r}")
    lines.append("")
    lines.append("Annotated command (segment  ->  hex  |  ascii)")
    lines.append("----------------------------------------------")

    def seg(label: str, chunk: bytes) -> None:
        lines.append(f"  {label}")
        lines.append(f"      {_hexline(chunk)}   |  {_printable(chunk)}")

    seg("ESC i a 03 (switch to P-touch Template command mode)", ESC_IA_TEMPLATE)
    seg("ESC @  (init)", ESC_AT)
    seg("^II  (template-mode init)", TOK_II)
    key_bytes = TOK_TS + f"{key:03d}".encode("ascii")
    seg(f"^TS{key:03d}  (select template key {key}, zero-padded)", key_bytes)

    for field_key, obj_name in OBJECTS:
        data = fields[field_key].encode("utf-8")
        name_seg = TOK_ON + obj_name.encode("ascii") + b"\x00"
        seg(f"^ON {obj_name}\\x00  (address object by name)", name_seg)
        length = len(data)
        di_head = TOK_DI + length.to_bytes(2, "little")
        seg(f"^DI len={length} ({length.to_bytes(2, 'little').hex(' ').upper()} LE)  (data header)", di_head)
        seg(f"     data = {fields[field_key]!r}", data)

    seg("^FF  (feed + PRINT trigger)", TOK_FF)

    lines.append("")
    lines.append(f"Total command length: {len(cmd)} bytes")
    lines.append("")
    lines.append("Full raw hex")
    lines.append("------------")
    for i in range(0, len(cmd), 16):
        chunk = cmd[i:i + 16]
        lines.append(f"  {i:04X}  {_hexline(chunk):<47}  |  {_printable(chunk)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m spoolman.test_print_label",
        description="Validator for the ENR filament label-print chain "
                    "(Spoolman -> P-touch Template -> raw TCP -> QL-720NW).",
    )
    parser.add_argument("spool_id", type=int, help="Spoolman spool id (e.g. 22 or 23)")
    parser.add_argument("--ip", default=ptouch.DEFAULT_PRINTER_IP,
                        help=f"printer IP (default {ptouch.DEFAULT_PRINTER_IP})")
    parser.add_argument("--port", type=int, default=ptouch.DEFAULT_PRINTER_PORT,
                        help=f"printer port (default {ptouch.DEFAULT_PRINTER_PORT})")
    parser.add_argument("--key", type=int, default=ptouch.DEFAULT_TEMPLATE_KEY,
                        help=f"template Key Assign (default {ptouch.DEFAULT_TEMPLATE_KEY})")
    parser.add_argument("--dry-run", action="store_true",
                        help="assemble + hex-dump only; do NOT open the socket")
    args = parser.parse_args(argv)

    # 1. Fetch the spool record (via the shared connector-backed fetch).
    try:
        spool = ptouch.fetch_spool(args.spool_id)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        print(f"ERROR: Spoolman returned HTTP {code} for spool {args.spool_id}. "
              f"Real cataloged spools today are 22 (Galaxy Purple) and 23 (Silk+ Rose Gold).",
              file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"ERROR: could not reach Spoolman: {exc}", file=sys.stderr)
        return 2

    # 2. Map fields (never crashes on a missing field).
    fields, warnings = ptouch.build_fields(spool, args.spool_id)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    # 3. Assemble the command.
    cmd = ptouch.build_command(fields, args.key)

    # 4a. Dry-run: dump and stop.
    if args.dry_run:
        print(f"=== DRY RUN — spool {args.spool_id}, template key {args.key} "
              f"(no socket opened) ===\n")
        print(annotated_dump(fields, args.key, cmd))
        print(f"\nWould send {len(cmd)} bytes to {args.ip}:{args.port} on a live run.")
        return 0

    # 4b. Live: send it.
    print(f"Sending {len(cmd)} bytes to printer {args.ip}:{args.port} "
          f"(spool {args.spool_id}, template key {args.key}) ...")
    try:
        ptouch.send_to_printer(cmd, args.ip, args.port)
    except PrinterUnreachable as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("SUCCESS: bytes sent. Check the QL-720NW for a printed label "
          "(the ^FF trigger should have fed + cut it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
