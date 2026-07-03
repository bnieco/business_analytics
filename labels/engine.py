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

MULTI-LINE OBJECT DATA (values containing a newline)
----------------------------------------------------
  ^DI is a byte-length "directly insert object" command: per the Brother
  P-touch Template Command Reference (QL-720NW, docp000732, p.49) it inserts
  exactly its len bytes into the selected object and — crucially — "Even if a
  print command or delimiter is within the specified number of characters,
  they are treated as data." So a line-feed byte (0A/0D/0D0A) OR the literal
  token "^CR" placed INSIDE ^DI is never interpreted — the line-feed bytes are
  read-then-discarded (ref p.20, 4.6.2) and "^CR" prints literally. ^DI can
  therefore only ever produce a SINGLE line, which is why every raw-byte and
  inside-^DI-^CR attempt printed one line.

  The line feed inside a text object is the ^CR CONTROL COMMAND (5E 43 52),
  which the reference documents (p.46) as "Line feed in object ... Adds a line
  feed to the next line in the text object. Valid even if the text string for
  the line feed command has been changed" — i.e. unconditionally valid,
  independent of the printer's Line Feed Code setting. Brother's own worked
  example is `1 ^CR 2 ^CR 3 ^FF` (31 5E4352 32 5E4352 33 5E4646) -> three
  printed lines. The line segments are streamed as RAW data (no ^DI wrapper)
  with ^CR commands between them; raw data runs until the next command token
  (the next object's ^ON, or the final ^FF) closes it.

  So for a value that contains a newline this engine emits, for that object:
    ^ON{name}\x00  {line1_utf8}  ^CR  {line2_utf8}  ^CR  ...  {lineN_utf8}
  with NO ^DI. Single-line values keep the ^ON + ^DI path and are byte-
  identical to before (regression-guarded in the test suite).

  KNOWN LIMIT (multi-line only): raw-stream data has no byte-length guard, so a
  literal '^' (0x5E) or the configured delimiter inside a multi-line value
  could be mis-parsed as a command. Single-line values are unaffected (^DI is
  length-prefixed and binary-safe). Garage-bin label lines are short human
  text where this is not expected; documented here for the caller's awareness.

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
# ^CR (5E 43 52) — the P-touch Template "line feed in object" CONTROL command.
# It is a line feed ONLY when emitted as a standalone command between raw data
# segments; placed inside ^DI data it is treated as literal bytes (^DI inserts
# its len bytes verbatim). See the MULTI-LINE section in the module docstring.
TOK_CR = b"^CR"


def _split_lines(value: str) -> list[str]:
    """Split a field value into line segments on \\r\\n, \\r, or \\n.

    Normalizes the three newline conventions to a single split so a value that
    reaches us as "AAA\\r\\nBBB" and one that reaches us as "AAA\\nBBB" both
    produce ["AAA", "BBB"]. A value with no newline returns a single-element
    list (and stays on the byte-identical single-line ^DI path).
    """
    return value.replace("\r\n", "\n").replace("\r", "\n").split("\n")


class PrinterUnreachable(RuntimeError):
    """Raised when the printer cannot be reached over TCP (socket failure)."""


class UnknownField(ValueError):
    """Raised when `fields` names an object the template does not declare."""


# --------------------------------------------------------------------------- #
# Byte assembly (dialect: ptouch-template)
# --------------------------------------------------------------------------- #
def build_command(
    fields: Mapping[str, str],
    key: int,
    objects: Sequence[str],
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
      objects: the template's object names in print order (the per-template
               data that used to be the hardcoded OBJECTS constant).

    Byte layout: ESC i a 03  ESC @  ^II  ^TS{key:03d}  then per object
    ^ON{name}\\x00  ^DI{len16LE}{data}  then ^FF. Objects are addressed by
    name and emitted in `objects` order. The leading ESC i a 03 switches the
    printer into P-touch Template command mode for the job — without it the
    printer no-ops silently over 9100.
    """
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

    out = bytearray()
    out += ESC_IA_TEMPLATE  # first: switch to P-touch Template command mode
    out += ESC_AT
    out += TOK_II
    out += TOK_TS + f"{key:03d}".encode("ascii")  # zero-padded 3 digits

    for obj_name in objects:
        value = fields[obj_name]  # KeyError if missing — by design
        out += TOK_ON + obj_name.encode("ascii") + b"\x00"
        if "\n" in value or "\r" in value:
            # MULTI-LINE: stream raw line segments joined by the ^CR line-feed
            # COMMAND, with NO ^DI. ^DI would swallow ^CR as literal data and
            # advance the object pointer (that is why the interleaved
            # ^DI/^CR/^DI attempt scattered lines 2..n into the FOLLOWING
            # objects and left only line 1 here). See the module docstring.
            segments = [line.encode("utf-8") for line in _split_lines(value)]
            out += TOK_CR.join(segments)
        else:
            # SINGLE-LINE: unchanged, byte-identical to the pre-multiline
            # engine (the filament path's regression gate depends on this).
            data = value.encode("utf-8")
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
