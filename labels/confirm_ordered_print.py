"""Confirming print for the ORDERED-FILL multi-line garage template.

Purpose
-------
Prove, on hardware, that the ordered-fill path (mode="ordered") renders a
multi-line TXT_LABELLINE2 on the QL-720NW — the ONE confirming print gated by
Argus after the re-transfer. Mirrors the shape of the proven single-line
/tmp/success_print.py, but drives the engine's ORDERED path with the ^SS
delimiter-pin and the designated multiline field.

SAFETY (read before running)
-----------------------------
This script is DRY BY DEFAULT: run with no flags and it only BUILDS the stream
and prints the annotated hex — it opens NO socket, sends NO bytes. It transmits
to the printer ONLY when invoked explicitly with `--send`. Importing this module
does nothing (no top-level send). This guard exists so an accidental run or
import can never fire the printer (delegated-outward-actions rule).

    # dry (safe) — build + show bytes, no printer contact:
    ./venv/bin/python -m labels.confirm_ordered_print

    # LIVE — actually print one label (Carson runs this WITH Brian's go, only
    # AFTER the re-transfer + KEY below is set to the new Key Assign):
    ./venv/bin/python -m labels.confirm_ordered_print --send

EMPTY-LINE-2 CONFIRMING PRINT (blank-cell bug fix, 2026-07-05)
-------------------------------------------------------------
Pass `--empty-line2` to set TXT_LABELLINE20003 to "" while the other five keep
their sentinels. This is the ONE print that confirms the empty-cell blanking fix
on hardware: with the fix, the empty Line 2 must now print BLANK (a single
space) — NOT the object's baked-in default "Line 2" text — and the five
neighboring objects must render their sentinels intact.

    # dry (safe) — see that Line 2's cell is a single 0x20 between Tabs:
    ./venv/bin/python -m labels.confirm_ordered_print --empty-line2

    # LIVE — fire the one confirming print (Carson runs WITH Brian's go, KEY set):
    ./venv/bin/python -m labels.confirm_ordered_print --empty-line2 --send

BEFORE THE LIVE RUN — set KEY + OBJECTS from the re-transfer
-----------------------------------------------------------
After Brian re-numbers the objects in P-touch Editor and re-transfers, he reads
the new Key Assign off Transfer Manager. Update `KEY` below to that number and
confirm `OBJECTS` matches the numbered object order (QR_URL first, the six in
fill order). MULTILINE_FIELDS stays {"TXT_LABELLINE2"} unless Brian also makes
Line 1 multi-line.
"""
from __future__ import annotations

import sys

from labels import engine as E

# --------------------------------------------------------------------------- #
# Parameters — UPDATE `KEY` to the new Key Assign after the re-transfer.
# --------------------------------------------------------------------------- #
IP, PORT = "10.0.0.200", 9100          # QL-720NW (unchanged)

KEY = 98                                # <-- REPLACE with the new Key Assign
                                        #     Transfer Manager reports post-re-transfer.

# The template's object names in FILL ORDER (the 4-digit suffix Brian appends in
# P-touch Editor encodes this order). QR/barcode object MUST be first so its cell
# is never adjacent to a ^CR. Replace with the EXACT numbered names off the
# Expanded tab after re-transfer (e.g. QR_URL0001, TXT_LABELLINE10002, ...).
OBJECTS = [
    "QR_URL0001",          # item URL / QR (verbatim, single cell, no ^CR)
    "TXT_LABELLINE10002",
    "TXT_LABELLINE20003",  # the MULTI-LINE object
    "TXT_ROOM0004",
    "TXT_LOCATION0005",
    "TXT_LOCCODE0006",
]

# ONLY these object names are ^CR line-split. Everything else (incl. QR) is
# a verbatim single cell; a newline in any of them fails closed.
MULTILINE_FIELDS = frozenset({"TXT_LABELLINE20003"})

# Recognizable sentinels: a 3-line Line-2 value plus a distinct marker in each of
# the other five objects, so the printed label is unambiguous about what rendered.
FIELDS = {
    "QR_URL0001":         "http://10.0.0.16:8745/item/999",
    "TXT_LABELLINE10002": "LINE1-OK",
    "TXT_LABELLINE20003": "AAA\nBBB\nCCC",   # <-- the 3-line multi-line proof
    "TXT_ROOM0004":       "ROOM-OK",
    "TXT_LOCATION0005":   "LOC-OK",
    "TXT_LOCCODE0006":    "CODE-OK",
}


def _readable(cmd: bytes) -> bytes:
    return (cmd.replace(b"\x1b", b"<ESC>")
               .replace(b"\x09", b"<TAB>")
               .replace(b"\x00", b"<NUL>"))


def main(argv: list[str]) -> int:
    send = "--send" in argv[1:]
    empty_line2 = "--empty-line2" in argv[1:]

    fields = dict(FIELDS)
    if empty_line2:
        # Blank ONLY Line 2; the other five keep their sentinels. Confirms the
        # empty-cell blanking fix: this cell must now print BLANK (single space),
        # not the object's baked-in default text.
        fields["TXT_LABELLINE20003"] = ""

    cmd = E.build_command(
        fields, KEY, OBJECTS,
        mode="ordered",
        multiline_fields=MULTILINE_FIELDS,
    )

    print(f"scenario     : {'EMPTY Line 2 (blank-cell fix)' if empty_line2 else 'multi-line Line 2'}")
    print(f"template key : {KEY}  (^TS{KEY:03d})")
    print(f"objects      : {OBJECTS}")
    print(f"multiline    : {sorted(MULTILINE_FIELDS)}")
    print(f"Line 2 value : {fields['TXT_LABELLINE20003']!r}")
    print(f"stream length: {len(cmd)} bytes")
    print(f"hex          : {cmd.hex()}")
    print(f"readable     : {_readable(cmd)!r}")

    if not send:
        print("\nDRY RUN — nothing sent. Re-run with --send to print "
              "(only after the re-transfer + KEY is set to the new Key Assign).")
        return 0

    print(f"\n--send given: transmitting to {IP}:{PORT} ...")
    E.send_to_printer(cmd, IP, PORT)
    print("SENT (raw 9100 has no ack — check the physical label).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
