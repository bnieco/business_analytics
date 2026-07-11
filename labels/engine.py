"""Label print ENGINE — P-touch Template byte assembly + raw-9100 transport.

This is the generic, hardware-validated core extracted from
`spoolman/webapp/ptouch.py` (Filament Inventory Phase 3, validated on the
Brother QL-720NW on 2026-07-01). It knows NOTHING about filament, Spoolman,
or any other calling app:

  * `build_command(fields, key, objects)` assembles the exact P-touch
    Template byte stream for any transferred template — the caller supplies
    the ordered object-name list (`objects`) and a `fields` map keyed by
    those object names. There is no hardcoded template in this module.
  * `send_to_printer(cmd, ip, port)` is the raw TCP 9100 transport.

P-TOUCH TEMPLATE COMMAND GRAMMAR (bytes) — the validated contract
-----------------------------------------------------------------
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

MULTI-LINE OBJECT DATA — verified against docp000732 (QL-720NW P-touch
Template Command Reference, v1.0, 2012; page numbers below are that manual)
---------------------------------------------------------------------------
Two SEPARATE facts, each read directly from the reference (not extrapolated):

  1. The named ^ON/^DI path is SINGLE-LINE ONLY. ^DI "Directly insert object"
     (p.49): "Inserts a text string for the specified number of characters
     into the object ... Even if a print command or delimiter is within the
     specified number of characters, they are treated as data." So a line-feed
     byte (0A/0D) or a literal "^CR" placed INSIDE ^DI is opaque data, never a
     line break. This is doc-confirmed AND falsified on hardware four ways on
     2026-07-03: (a) raw 0A in ^DI data; (b) literal "^CR" in ^DI data; (c)
     ^CR command BETWEEN two ^DI chunks — the object pointer advanced and line
     2 scattered into the NEXT object; (d) Available-Return-Code set to 0A +
     raw 0A in ^DI. Every ^DI variant printed ONE line.

  2. Multi-line into an object IS possible, but ONLY on the ORDERED-FILL
     (delimiter) data path, which the reference documents explicitly:
       * ^CR "Line feed in object" (p.46): "Adds a line feed to the next line
         in the text object." Worked example: `1 ^CR 2 ^CR 3 ^FF` -> three
         printed lines. This example uses NEITHER ^ON NOR ^DI — it is raw data
         flowing in the ordered stream.
       * The delimiter (default 09h Tab; p.54 and §3(3)): "the symbol used to
         indicate when to move to the next object in the data that is being
         sent." The DELIMITER — not a re-issued ^ON — is how the ordered stream
         advances from one object to the next.
       * ^ON (p.48) / ^OS (p.47) only "insert data starting with an
         intermediary object": they set the STARTING object of an ordered
         stream, not a per-object addressing mode within it.
     Hence the doc-supported multi-line stream is ordered fill:
       ^TS{key}  ^SS"01"<09h>  data1 <TAB> data2 <TAB> line1 ^CR line2 <TAB> ...  ^FF
     positional (template object order), no ^ON, no ^DI, led by an explicit ^SS
     delimiter-pin. This engine builds it via `build_command(..., mode="ordered",
     multiline_fields=...)`.

  DELIMITER PIN (^SS, docp000732 p.31): the ordered stream leads with ^SS to SET
  its own object delimiter (length-1 Tab, 09h — the documented default) rather
  than trusting whatever "Data Delimiter" the device happens to hold. That makes
  the ordered job self-contained and removes any on-device "confirm delimiter =
  Tab" operator step. Grammar `^SS n1 n2 data` where (n1*10)+n2 is the delimiter
  length as two ASCII digits; pinned bytes `5E 53 53 30 31 09`. See DELIM_PIN.

  DESIGNATED MULTI-LINE FIELDS ONLY (MAJOR-1, docp000732 §4.2 p.16): ^CR line
  splitting is applied ONLY to the object names the caller passes in
  `multiline_fields`. Every other ordered cell — critically the QR/barcode cell
  — emits its value verbatim and a newline in such a cell fails closed
  (`UnsafeFieldValue`), because a line feed in/around barcode data corrupts/voids
  the barcode. The engine stays generic: the garage caller declares
  `multiline_fields={"TXT_LABELLINE2"}`; nothing garage-specific is hardcoded.

  REMOVED / NOT SUPPORTED (correction 2026-07-04): a prior revision emitted,
  for a newline value on the NAMED path, `^ON{name}\x00 line1 ^CR line2` (raw
  ^CR after a name-select, no ^DI) and asserted it worked. The reference does
  NOT document that hybrid — the ordered ^CR example carries no ^ON, and
  object-advance in a data stream is the delimiter's job, not a re-issued ^ON
  — and its exact bytes were NEVER printed on hardware (all four 7/3 tests were
  ^DI-based, so the hybrid is untested-and-not-doc-demonstrated, NOT
  falsified). That branch is gone. On the named path a newline now degrades to
  a single ^DI line (byte-safe, predictable), matching the parked garage state.

  PRECONDITION (independent of the data path): whether a multi-line value
  renders AT ALL also depends on the template text object's "Text Layout" in
  P-touch Editor (docp000732 §4.1 / §5.2, p.22). A "Fixed Frame Size / Clip
  Text" object sized for one line clips lines 2..n even when ^CR is correct;
  the object must use a layout that shows multiple lines (Automatic Length, or
  a Fixed Frame sized tall enough). TXT_LABELLINE2 must be confirmed multi-line
  in P-touch Editor regardless of which data path is used.

INPUT GUARD (ordered path only): the ordered stream is delimiter/prefix
sensitive, so every field routed to it is checked FAIL-CLOSED — a literal '^'
(0x5E, the command prefix) or a Tab (0x09, the delimiter) in ANY field raises
`UnsafeFieldValue` rather than risk a mis-parsed command or a spurious object
advance. A newline is allowed ONLY in a field the caller DESIGNATED multi-line
(`multiline_fields`), where it becomes the ^CR line-feed command; a newline in
any other ordered field (e.g. the barcode/QR cell) also raises. The named ^DI
path needs no guard: ^DI is length-prefixed and binary-safe.

DIALECTS (v1 = ptouch-template ONLY)
------------------------------------
This engine implements the `ptouch-template` dialect (QL-720NW). The printer
registry carries a per-printer `dialect` field so a `raster` builder (Brother
PT-P750W, CV_PTE550WP750WP710BT reference) can slot in as a separate builder
later WITHOUT changing callers — that is a deferred, separately-scoped phase
(Argus B1 / Brian 2026-07-02: "ship service + QL first").
"""
from __future__ import annotations

import socket
from typing import Mapping, Sequence

SOCKET_TIMEOUT = 5.0   # printer connect/send (seconds)

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
# ^CR (5E 43 52) — the P-touch Template "line feed in object" CONTROL command
# (docp000732 p.46). It line-feeds ONLY on the ordered-fill data path (raw data
# between delimiters); placed inside ^DI data it is treated as literal bytes.
# See the MULTI-LINE section in the module docstring.
TOK_CR = b"^CR"

# Ordered-fill object delimiter. docp000732 (p.54 / §3(3)) documents the default
# as 09h (TAB): "the symbol used to indicate when to move to the next object in
# the data that is being sent."
TOK_DELIM = b"\x09"

# ^SS "Specify delimiter" (docp000732 p.31). Grammar (verified byte-for-byte
# against the reference, incl. its worked example — NOT guessed):
#     ASCII:        ^  S  S  n1  n2  data
#     Hexadecimal:  5E 53 53 n1  n2  data
#   n1,n2 are the delimiter text LENGTH as two ASCII decimal digits (zero-
#   padded): value = (n1*10)+n2, range 1..20. `data` is the delimiter text.
#   docp000732's own example, "change the delimiter to ',' (2Ch)": one char, so
#   n1='0' n2='1' -> "^SS01," = 5E 53 53 30 31 2C.
# We pin the delimiter to a length-1 Tab (09h), the printer's documented default
# ("The default text string for the delimiter is '09h' (Tab code)."):
#     ^SS "01" <09h>  =  5E 53 53 30 31 09
# Emitting this at the head of the ordered stream makes the ordered job self-
# contained: the delimiter is set FOR THIS JOB regardless of the device's saved
# P-touch Template "Data Delimiter" setting. ^SS is a dynamic command, so it
# takes effect for the data that follows it within the same job.
TOK_SS = b"^SS"
DELIM_PIN = TOK_SS + b"01" + TOK_DELIM  # 5E 53 53 30 31 09 -> pin delimiter = Tab

# EMPTY-CELL BLANKING (ordered path only) — a single space (0x20).
# --------------------------------------------------------------------------
# Hardware bug (Brian, 2026-07-05): on the ordered/delimiter fill path, when a
# template object receives an EMPTY field (zero bytes between two delimiters, or
# a trailing empty final cell), the QL-720NW prints that object's ORIGINAL
# design-time text (its baked-in default) instead of nothing. This is the
# documented P-touch Template "no data" behavior — an object given no replacement
# data keeps and prints its original text. It bites TXT_LABELLINE2 constantly
# (many garage bins have no line 2), so the default "Line 2" text leaks onto the
# label. To force the object to print BLANK we must send it NON-empty data whose
# glyphs are invisible: a single ASCII space. The QL-720NW P-touch Template
# reference (docp000732 / the RJ-series sibling manual) carries the legend
# '"■" indicates that a space is printed' — a 0x20 is real, printable data that
# renders as a blank space, so the object now HAS data and the "no data ->
# original text" fallback does not fire. There is NO documented rule treating a
# lone space as "no data" (line-feed codes 0D/0A are "read, then discarded", but
# no such discard applies to 0x20). We substitute this for any empty- or
# whitespace-only ORDERED cell (including a designated multiline field, where an
# empty value becomes ONE blank line = a single space, NOT a ^CR split). The
# space is inherently safe on the guarded ordered path: it is neither '^' (0x5E),
# a Tab (0x09), nor a newline.
ORDERED_BLANK = b"\x20"


def _split_lines(value: str) -> list[str]:
    """Split a field value into line segments on \\r\\n, \\r, or \\n.

    Normalizes the three newline conventions to a single split so a value that
    reaches us as "AAA\\r\\nBBB" and one that reaches us as "AAA\\nBBB" both
    produce ["AAA", "BBB"]. A value with no newline returns a single-element
    list (and stays on the byte-identical single-line ^DI path).
    """
    return value.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _is_blank(value: str) -> bool:
    """True if `value` is empty or whitespace-only.

    Used ONLY on the ordered-fill path to decide when a cell must be replaced
    with a single space so its template object prints BLANK instead of falling
    back to its baked-in default text (see ORDERED_BLANK). `"".strip()` and
    `"   ".strip()` are both `""`; a value with any visible character is not
    blank and is emitted verbatim (or ^CR-split for a designated field).
    """
    return value.strip() == ""


class PrinterUnreachable(RuntimeError):
    """Raised when the printer cannot be reached over TCP (socket failure)."""


class UnknownField(ValueError):
    """Raised when `fields` names an object the template does not declare."""


class UnsafeFieldValue(ValueError):
    """Raised (fail-closed) when a field routed to the ordered-fill path holds a
    byte that could corrupt that delimiter/prefix-sensitive stream: a literal
    '^' (0x5E, the command prefix — could form a `^XX` command such as ^CR/^FF)
    or a Tab (0x09, the object delimiter — would spuriously advance to the next
    object). A newline is allowed ONLY in a field the caller DESIGNATED
    multi-line (`multiline_fields`); a newline in any NON-designated ordered
    field is rejected here too, so it can never leak into a barcode/QR cell (a
    line feed in/around barcode data voids the barcode — docp000732 §4.2 p.16)."""


# Bytes that must never appear verbatim in ANY field on the ordered-fill path.
_ORDERED_FORBIDDEN = {0x5E: "'^' (0x5E command prefix)", 0x09: "Tab (0x09 delimiter)"}
# Newline bytes — forbidden only in NON-designated (single-line) ordered fields.
_NEWLINE_FORBIDDEN = {0x0A: "newline (0x0A)", 0x0D: "carriage return (0x0D)"}


def _guard_ordered_field(name: str, value: str, allow_newline: bool) -> None:
    """Fail-closed check for one field on the ordered-fill path.

    Always rejects any '^' (0x5E) or Tab (0x09) in the value. A '^' is rejected
    wholesale (not just recognized `^XX` tokens) because ANY '^' can begin a
    command token once it reaches the printer's ordered stream; this is strictly
    stronger than a token allowlist and leaves no bypass.

    When `allow_newline` is False (the field is NOT a designated multi-line
    object), a newline / carriage-return byte is ALSO rejected. This is the
    MAJOR-1 guarantee: only explicitly-designated objects may carry a ^CR line
    feed, and a stray newline in an undesignated cell — most dangerously the
    barcode/QR cell — fails closed instead of silently flowing in and corrupting
    the barcode (docp000732 §4.2 p.16). Applied to EVERY field of an ordered
    label, so one designated multi-line field cannot drag a sibling's unsafe
    byte through unchecked.
    """
    forbidden = dict(_ORDERED_FORBIDDEN)
    if not allow_newline:
        forbidden.update(_NEWLINE_FORBIDDEN)
    for ch in value:
        code = ord(ch)
        if code in forbidden:
            raise UnsafeFieldValue(
                f"field {name!r} contains {forbidden[code]}, which is unsafe on "
                f"the ordered-fill path"
                + ("" if allow_newline else
                   " for a non-multiline object (declare it in multiline_fields "
                   "if it is meant to carry line feeds)")
                + "; reject or sanitize upstream"
            )


# --------------------------------------------------------------------------- #
# Byte assembly (dialect: ptouch-template)
# --------------------------------------------------------------------------- #
def build_command(
    fields: Mapping[str, str],
    key: int,
    objects: Sequence[str],
    mode: str = "named",
    multiline_fields: frozenset[str] = frozenset(),
) -> bytes:
    """Assemble the exact P-touch Template byte string for one label.

    Args:
      fields:  object-name -> replacement text. Keys MUST be a subset of
               `objects`; a key not in `objects` raises `UnknownField` (it
               would otherwise silently no-op on the printer — the classic
               typo trap). Every name in `objects` MUST be present in
               `fields` (missing -> KeyError; defaulting/placeholder policy
               is the CALLER's job — each app owns its own empties).
      key:     the template's Key Assign slot (the ^TS{nnn} value, 0..999).
      objects: the template's object names in the template's order. In
               `named` mode this is print order for ^ON addressing; in
               `ordered` mode it is the POSITIONAL fill order (the objects
               must be numbered in P-touch Editor to match — see below).
      mode:    "named" (default) or "ordered".
               * "named"  — ^ON{name}\\x00 ^DI{len16LE}{data} per object; each
                 object addressed by name. SINGLE-LINE ONLY: ^DI is verbatim
                 (docp000732 p.49), so a newline in a value is inserted as
                 opaque bytes and prints on one line. This is the proven
                 filament path and stays byte-identical for single-line values.
               * "ordered" — the doc-supported MULTI-LINE path (docp000732
                 p.46/p.54): no ^ON, no ^DI; field values are emitted
                 positionally in `objects` order, joined by the Tab delimiter.
                 The stream LEADS with an explicit ^SS delimiter-pin (see
                 below). ONLY fields named in `multiline_fields` are split into
                 lines and joined by the ^CR line-feed command; every other
                 field is emitted as a single verbatim cell. Every field is
                 guarded fail-closed first (`UnsafeFieldValue` on a literal '^',
                 a Tab, or — for a NON-designated field — a newline). An empty-
                 or whitespace-only cell is emitted as a single space
                 (`ORDERED_BLANK`) so its template object prints BLANK instead of
                 its baked-in design-time text (the P-touch Template "no data ->
                 original text" behavior); for a designated multiline field an
                 empty value is one blank line, not an empty ^CR split. Requires
                 the template's objects to be numbered so their fill order
                 matches `objects`.
      multiline_fields:
               (ordered mode only) the set of object names that MAY carry a
               multi-line value — these and ONLY these are ^CR-split. Any name
               here must be one of `objects`. A newline in a field NOT listed
               here is a fail-closed `UnsafeFieldValue`, so a stray line feed
               can never flow into the barcode/QR cell (docp000732 §4.2 p.16
               warns a line feed in/around barcode data voids the barcode).
               Default: no field is multi-line. Ignored in `named` mode.

    Frame (named): ESC i a 03  ESC @  ^II  ^TS{key:03d} ...objects... ^FF.
    Frame (ordered): ESC i a 03  ESC @  ^II  ^TS{key:03d}  ^SS"01"<09h>
                     cell0 <TAB> cell1 <TAB> ... ^FF. The leading ESC i a 03
    switches the printer into P-touch Template command mode for the job —
    without it the printer no-ops silently over 9100. The ^SS delimiter-pin
    makes the ordered job self-contained regardless of the device's saved
    delimiter setting.
    """
    if mode not in ("named", "ordered"):
        raise ValueError(f"mode must be 'named' or 'ordered', got {mode!r}")
    if not (0 <= key <= 999):
        raise ValueError("template key must be a 3-digit value (0..999)")
    if not objects:
        raise ValueError("objects list must not be empty")
    extra = set(fields) - set(objects)
    if extra:
        raise UnknownField(
            f"field names not declared by this template: {sorted(extra)} "
            f"(declared: {list(objects)})"
        )
    ml_extra = set(multiline_fields) - set(objects)
    if ml_extra:
        raise UnknownField(
            f"multiline_fields names not declared by this template: "
            f"{sorted(ml_extra)} (declared: {list(objects)})"
        )

    out = bytearray()
    out += ESC_IA_TEMPLATE  # first: switch to P-touch Template command mode
    out += ESC_AT
    out += TOK_II
    out += TOK_TS + f"{key:03d}".encode("ascii")  # zero-padded 3 digits

    if mode == "named":
        for obj_name in objects:
            value = fields[obj_name]  # KeyError if missing — by design
            # SINGLE-LINE named path: ^ON + ^DI, byte-identical to the pre-
            # multiline engine (the filament regression gate depends on this).
            # A newline value is inserted verbatim by ^DI and prints on one
            # line — the doc-confirmed ^DI behavior, NOT the removed hybrid.
            data = value.encode("utf-8")
            out += TOK_ON + obj_name.encode("ascii") + b"\x00"
            out += TOK_DI + len(data).to_bytes(2, "little") + data
    else:  # mode == "ordered"
        # Guard EVERY field first (fail-closed), so a label with one multi-line
        # field cannot slip another field's '^'/Tab/newline through unchecked.
        # A newline is permitted ONLY in a DESIGNATED multiline field.
        for obj_name in objects:
            _guard_ordered_field(
                obj_name, fields[obj_name],  # KeyError if missing
                allow_newline=obj_name in multiline_fields,
            )
        # Pin the delimiter in-stream so the job does not depend on the device's
        # saved P-touch Template "Data Delimiter" setting (docp000732 p.31 ^SS).
        out += DELIM_PIN
        # Positional fill: object data joined by the Tab delimiter. ONLY a
        # DESIGNATED multiline field is line-split (segments joined by ^CR); all
        # other fields — including the QR/barcode cell — emit their value
        # verbatim as a single cell, so no ^CR can ever land in a barcode cell.
        # No ^ON, no ^DI.
        #
        # EMPTY-CELL BLANKING: an empty- or whitespace-only cell is emitted as a
        # single space (ORDERED_BLANK) so its template object prints BLANK
        # instead of its baked-in default text (the P-touch Template "no data ->
        # original text" behavior — see ORDERED_BLANK). This check comes FIRST,
        # ahead of the multiline branch, so an empty designated multiline field
        # becomes ONE blank line (a single space), NOT an empty ^CR split. The
        # substitution runs AFTER the fail-closed guard above, which is unchanged
        # and still inspects the raw values, so a value that is only a Tab/newline
        # (unsafe, whitespace-ish) still fails closed rather than being blanked.
        cells = []
        for obj_name in objects:
            value = fields[obj_name]
            if _is_blank(value):
                cells.append(ORDERED_BLANK)
            elif obj_name in multiline_fields:
                segments = [line.encode("utf-8") for line in _split_lines(value)]
                cells.append(TOK_CR.join(segments))
            else:
                cells.append(value.encode("utf-8"))
        out += TOK_DELIM.join(cells)

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

    NOTE (send-success is not print-success): a clean `sendall` only proves the
    bytes left this host. The QL-720NW accepts and silently discards a stream
    whose template key is empty/stale (the documented ESC i a 03 / key-drift
    no-op). Callers that need "did a label come out?" must rely on the print-
    attempt log + the operator's eyes; raw 9100 offers no print acknowledgment.
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
