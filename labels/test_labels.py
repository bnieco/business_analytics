"""Label Printing Service v1 — offline test suite (engine + registry + service).

DRY-RUN ONLY: no test in this file ever opens a socket to a printer. The
service's transport is patched (`labels.engine.send_to_printer`) so /print is
exercised end-to-end with the assembled bytes CAPTURED instead of sent.

Run (plain python, no pytest needed):
    cd ~/PycharmProjects/business_analytics
    ./venv/bin/python -m labels.test_labels

The functions are pytest-collectable too if pytest is ever added.

What is covered
---------------
  * the P-touch Template byte grammar, assembled by hand and compared
    byte-for-byte against `engine.build_command` (incl. the required leading
    ESC i a 03, zero-padded ^TS key, ^ON null terminator, ^DI len16LE);
  * engine validation (bad key, empty objects, unknown field, missing field);
  * registry loading + cross-validation errors;
  * BOTH PATHS BYTE-EQUALITY: the filament template through the in-process
    `spoolman.webapp.ptouch` path and through the service's builder produce
    IDENTICAL bytes for the same logical fields (the Phase-M regression gate);
  * the service HTTP contract: /api/health, /templates, /printers, and every
    /print outcome — 200 (single + copies), missing-field placeholder
    defaulting, and the 400s (unknown template / unknown printer / printer
    mismatch / unknown fields / copies bounds / unimplemented dialect) plus
    502 (unreachable), using a mocked transport.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from labels import engine
from labels.registry import Registry, RegistryError

# --------------------------------------------------------------------------- #
# Fixed test vectors (offline — no Spoolman, no printer)
# --------------------------------------------------------------------------- #
# A synthetic Spoolman spool record shaped like the hardware-validated ones
# (spool 22 "PLA Galaxy Purple" was the original validation spool; it has
# since been deleted from Spoolman, so the regression vector is FROZEN here).
SPOOL_22_LIKE = {
    "id": 22,
    "location": "Cart",
    "filament": {
        "name": "PLA Galaxy Purple",
        "material": "PLA Galaxy",
        "vendor": {"name": "Bambu Lab"},
    },
}

FILAMENT_OBJECTS = (
    "QR_URL0001", "TXT_BRAND0002", "TXT_MATERIAL0003",
    "TXT_COLOR0004", "TXT_LOCATION0005", "TXT_SPOOLID0006",
)


def _hand_assembled(fields_by_object: dict[str, str], key: int,
                    objects=FILAMENT_OBJECTS) -> bytes:
    """The grammar assembled independently, straight from the Template Spec."""
    out = b"\x1b\x69\x61\x03"                     # ESC i a 03 — MUST be first
    out += b"\x1b\x40"                            # ESC @
    out += b"^II"
    out += b"^TS" + f"{key:03d}".encode("ascii")  # zero-padded 3 digits
    for name in objects:
        data = fields_by_object[name].encode("utf-8")
        out += b"^ON" + name.encode("ascii") + b"\x00"
        out += b"^DI" + len(data).to_bytes(2, "little") + data
    out += b"^FF"
    return out


# --------------------------------------------------------------------------- #
# Engine: byte grammar
# --------------------------------------------------------------------------- #
def test_engine_grammar_exact_bytes():
    fields = {
        "QR_URL0001": "http://10.0.0.16:7912/spool/show/22",
        "TXT_BRAND0002": "Bambu Lab",
        "TXT_MATERIAL0003": "PLA Galaxy",
        "TXT_COLOR0004": "Purple",
        "TXT_LOCATION0005": "Cart",
        "TXT_SPOOLID0006": "Spool #22",
    }
    cmd = engine.build_command(fields, 99, FILAMENT_OBJECTS)
    assert cmd == _hand_assembled(fields, 99), "engine bytes != hand-assembled spec"
    # Structural spot checks
    assert cmd.startswith(b"\x1b\x69\x61\x03\x1b\x40^II^TS099")
    assert cmd.endswith(b"^FF")
    assert b"^ONQR_URL0001\x00" in cmd
    data = "http://10.0.0.16:7912/spool/show/22".encode()
    assert b"^DI" + len(data).to_bytes(2, "little") + data in cmd


def test_engine_key_zero_padding_and_bounds():
    fields = {"A": "x"}
    assert b"^TS007" in engine.build_command(fields, 7, ("A",))
    assert b"^TS000" in engine.build_command(fields, 0, ("A",))
    assert b"^TS999" in engine.build_command(fields, 999, ("A",))
    for bad in (-1, 1000):
        try:
            engine.build_command(fields, bad, ("A",))
            raise AssertionError(f"key {bad} should have raised ValueError")
        except ValueError:
            pass


def test_engine_utf8_length_is_bytes_not_chars():
    # len16LE must count UTF-8 BYTES; 'Résumé' is 6 chars / 8 bytes.
    cmd = engine.build_command({"A": "Résumé"}, 1, ("A",))
    assert b"^DI" + (8).to_bytes(2, "little") in cmd


def test_engine_named_newline_degrades_to_single_di_line():
    """CORRECTED (2026-07-04): on the NAMED path a newline value is inserted
    VERBATIM by ^DI (docp000732 p.49 — ^DI bytes are opaque, prints one line).
    The removed hybrid raw-^CR-after-^ON branch must NOT reappear: no ^CR on
    the named path, ever. Byte-level / dry-run ONLY — nothing is sent."""
    data = "X\nY".encode("utf-8")
    cmd = engine.build_command({"A": "X\nY"}, 1, ("A",))
    expected = (b"\x1b\x69\x61\x03\x1b\x40^II^TS001"
                + b"^ONA\x00^DI" + len(data).to_bytes(2, "little") + data
                + b"^FF")
    assert cmd == expected, (cmd, expected)
    assert b"^CR" not in cmd, "named path must never emit the ^CR line-feed command"


def test_engine_ordered_multiline_exact_bytes():
    """ORDERED-FILL path (the doc-supported multi-line recipe, docp000732
    p.46/p.54): stream LEADS with the ^SS delimiter-pin (5E 53 53 30 31 09),
    then no ^ON, no ^DI; fields emitted positionally in `objects` order, joined
    by the Tab (09h) delimiter. ONLY the DESIGNATED multiline field's segments
    are joined by the ^CR line-feed command; other fields are verbatim cells.
    Byte-for-byte. Dry-run ONLY."""
    objects = ("QR_URL", "TXT_LINE1", "TXT_LINE2", "TXT_ROOM")
    fields = {"QR_URL": "http://x/1", "TXT_LINE1": "Garage",
              "TXT_LINE2": "AAA\nBBB\nCCC", "TXT_ROOM": "Attic"}
    cmd = engine.build_command(fields, 98, objects, mode="ordered",
                               multiline_fields=frozenset({"TXT_LINE2"}))
    expected = (
        b"\x1b\x69\x61\x03\x1b\x40^II^TS098"
        + b"^SS" + b"01" + b"\x09"                 # delimiter-pin: len 1, Tab
        + b"http://x/1" + b"\x09"
        + b"Garage" + b"\x09"
        + b"AAA" + b"^CR" + b"BBB" + b"^CR" + b"CCC" + b"\x09"
        + b"Attic"
        + b"^FF"
    )
    assert cmd == expected, (cmd, expected)
    assert b"^ON" not in cmd and b"^DI" not in cmd, "ordered fill uses neither"
    assert cmd.count(b"^CR") == 2, "two line feeds for a three-line value"
    # 3 delimiter-separators between 4 objects + the 1 Tab inside the ^SS pin.
    assert cmd.count(b"\x09") == 4
    # The ^SS pin leads the data, right after ^TS098.
    assert b"^TS098^SS01\x09" in cmd


def test_engine_ordered_empty_field_emits_single_space():
    """EMPTY-CELL BLANKING (hardware bug, 2026-07-05): on the ordered path an
    empty OR whitespace-only cell is emitted as a single space (0x20), NOT an
    empty cell, so its template object prints BLANK instead of its baked-in
    default text (the P-touch Template 'no data -> original text' behavior).
    Byte-for-byte. Dry-run ONLY."""
    objects = ("A", "B", "C")
    frame = b"\x1b\x69\x61\x03\x1b\x40^II^TS001" + engine.DELIM_PIN
    # Empty middle cell -> single 0x20 between the two Tabs (not an empty cell).
    cmd = engine.build_command({"A": "x", "B": "", "C": "y"}, 1, objects,
                               mode="ordered")
    expected = frame + b"x" + b"\x09" + b"\x20" + b"\x09" + b"y" + b"^FF"
    assert cmd == expected, (cmd, expected)
    # The blanked cell is exactly one space (would be zero bytes without the fix).
    assert b"x\x09\x20\x09y" in cmd
    # Whitespace-only value blanks identically to empty.
    cmd_ws = engine.build_command({"A": "x", "B": "   ", "C": "y"}, 1, objects,
                                  mode="ordered")
    assert cmd_ws == expected, (cmd_ws, expected)


def test_engine_ordered_empty_multiline_field_is_single_space():
    """An empty (or whitespace-only) DESIGNATED multiline field becomes ONE
    blank line = a single space, NOT an empty ^CR split, with the neighboring
    cells intact. This is the exact TXT_LABELLINE2 'no line 2' case Brian hit on
    hardware. Byte-for-byte. Dry-run ONLY."""
    objects = ("QR_URL", "TXT_LINE1", "TXT_LINE2", "TXT_ROOM")
    ml = frozenset({"TXT_LINE2"})
    frame = b"\x1b\x69\x61\x03\x1b\x40^II^TS098" + engine.DELIM_PIN
    for empty in ("", "   "):
        cmd = engine.build_command(
            {"QR_URL": "http://x/1", "TXT_LINE1": "Garage",
             "TXT_LINE2": empty, "TXT_ROOM": "Attic"},
            98, objects, mode="ordered", multiline_fields=ml)
        expected = (frame + b"http://x/1" + b"\x09" + b"Garage" + b"\x09"
                    + b"\x20" + b"\x09" + b"Attic" + b"^FF")
        assert cmd == expected, (empty, cmd, expected)
        # The empty multiline cell is a single space, and produces NO line feed.
        assert b"Garage\x09\x20\x09Attic" in cmd
        assert b"^CR" not in cmd, "empty multiline value must not emit a ^CR"
        # Neighbors untouched.
        assert b"http://x/1" in cmd and b"Attic" in cmd
    # Sanity: a NON-empty multiline value still ^CR-splits as before.
    cmd2 = engine.build_command(
        {"QR_URL": "http://x/1", "TXT_LINE1": "Garage",
         "TXT_LINE2": "AAA\nBBB", "TXT_ROOM": "Attic"},
        98, objects, mode="ordered", multiline_fields=ml)
    assert b"Garage\x09AAA^CRBBB\x09Attic" in cmd2


def test_engine_named_empty_field_not_blanked():
    """The NAMED path is UNTOUCHED by empty-cell blanking: an empty value stays a
    zero-length ^DI (^DI 00 00), NOT a space. This locks the filament regression
    gate — the space substitution lives ONLY in the ordered branch."""
    cmd = engine.build_command({"A": ""}, 1, ("A",))
    expected = (b"\x1b\x69\x61\x03\x1b\x40^II^TS001"
                + b"^ONA\x00^DI\x00\x00"        # zero-length data, no space
                + b"^FF")
    assert cmd == expected, (cmd, expected)
    assert b"^DI\x00\x00" in cmd, "named empty field must be a zero-length ^DI"


def test_engine_ordered_ss_pin_leads_and_pins_tab():
    """The ordered stream is self-contained: it begins (after ^TS) with the ^SS
    delimiter-pin exactly = 5E 53 53 30 31 09 (docp000732 p.31, '^SS n1 n2 data',
    length 1, data = Tab). Verified against the doc's worked '^SS01,' example."""
    assert engine.DELIM_PIN == b"\x5e\x53\x53\x30\x31\x09"
    cmd = engine.build_command({"A": "x"}, 5, ("A",), mode="ordered")
    assert cmd == (b"\x1b\x69\x61\x03\x1b\x40^II^TS005"
                   + b"\x5e\x53\x53\x30\x31\x09" + b"x" + b"^FF")


def test_engine_ordered_only_designated_field_gets_cr():
    """A newline is line-split into ^CR ONLY for a designated multiline field;
    an undesignated field with a newline fails closed (never reaches ^CR)."""
    objs = ("QR_URL", "TXT_LINE2")
    # TXT_LINE2 designated -> ^CR; QR_URL verbatim, no ^CR anywhere near it.
    cmd = engine.build_command({"QR_URL": "http://x/1", "TXT_LINE2": "a\nb"},
                               98, objs, mode="ordered",
                               multiline_fields=frozenset({"TXT_LINE2"}))
    assert b"http://x/1\x09a^CRb^FF" in cmd
    # The QR/barcode cell must never contain a ^CR.
    qr_cell = cmd.split(b"^SS01\x09", 1)[1].split(b"\x09", 1)[0]
    assert b"^CR" not in qr_cell, "barcode/QR cell must never carry a line feed"


def test_engine_ordered_newline_in_nondesignated_field_fails_closed():
    """MAJOR-1 fail-closed: a newline in a NON-designated ordered field raises
    UnsafeFieldValue — it must not silently pass into the barcode/QR cell
    (docp000732 §4.2 p.16: a line feed in/around barcode data voids it)."""
    objs = ("QR_URL", "TXT_LINE2")
    # Newline in QR_URL (not designated) -> reject, even though TXT_LINE2 is
    # the declared multiline field.
    for bad in ({"QR_URL": "http://x/1\nEVIL", "TXT_LINE2": "ok"},   # \n in QR
                {"QR_URL": "http://x/1\rEVIL", "TXT_LINE2": "ok"}):  # \r in QR
        try:
            engine.build_command(bad, 98, objs, mode="ordered",
                                 multiline_fields=frozenset({"TXT_LINE2"}))
            raise AssertionError(f"expected UnsafeFieldValue for {bad}")
        except engine.UnsafeFieldValue:
            pass
    # With NO multiline_fields declared, ANY newline is rejected.
    try:
        engine.build_command({"QR_URL": "a", "TXT_LINE2": "a\nb"}, 98, objs,
                             mode="ordered")
        raise AssertionError("newline with no designated field should raise")
    except engine.UnsafeFieldValue:
        pass


def test_engine_ordered_multiline_fields_must_be_declared():
    """A multiline_fields name not in `objects` is an UnknownField (typo trap)."""
    try:
        engine.build_command({"A": "x"}, 1, ("A",), mode="ordered",
                             multiline_fields=frozenset({"NOPE"}))
        raise AssertionError("undeclared multiline field should raise")
    except engine.UnknownField:
        pass


def test_engine_ordered_normalizes_crlf_and_cr():
    """\\r\\n, \\r and \\n segment identically on the ordered path (-> ^CR) for a
    DESIGNATED multiline field."""
    ml = frozenset({"A"})
    a = engine.build_command({"A": "X\r\nY"}, 1, ("A",), mode="ordered", multiline_fields=ml)
    b = engine.build_command({"A": "X\rY"}, 1, ("A",), mode="ordered", multiline_fields=ml)
    c = engine.build_command({"A": "X\nY"}, 1, ("A",), mode="ordered", multiline_fields=ml)
    assert a == b == c
    assert b"^TS001^SS01\x09X^CRY^FF" in c


def test_engine_ordered_guard_fail_closed():
    """The ordered path guards EVERY field fail-closed: a literal '^' (0x5E) or
    Tab (0x09) in ANY field raises UnsafeFieldValue, even when a DIFFERENT field
    is the multi-line one (so a stray '^'/Tab can't slip through a sibling of the
    multi-line field). A newline is allowed ONLY in a designated field."""
    objs = ("A", "B")
    ml = frozenset({"A"})
    for bad in ({"A": "ok", "B": "2^3"},            # '^' in a plain field
                {"A": "a\tb", "B": "ok"},            # Tab in a plain field
                {"A": "l1\nl2", "B": "safe^bad"}):   # '^' in sibling of multi-line
        try:
            engine.build_command(bad, 1, objs, mode="ordered", multiline_fields=ml)
            raise AssertionError(f"expected UnsafeFieldValue for {bad}")
        except engine.UnsafeFieldValue:
            pass
    # Newline in the DESIGNATED field is permitted and becomes ^CR.
    cmd = engine.build_command({"A": "l1\nl2", "B": "ok"}, 1, objs,
                               mode="ordered", multiline_fields=ml)
    assert b"l1^CRl2\x09ok^FF" in cmd


def test_engine_bad_mode_rejected():
    try:
        engine.build_command({"A": "x"}, 1, ("A",), mode="hybrid")
        raise AssertionError("unknown mode should raise ValueError")
    except ValueError:
        pass


def test_engine_singleline_is_byte_identical_after_multiline_feature():
    """PROOF that adding the multi-line path did not regress single-line output:
    the full filament vector still assembles byte-for-byte to the frozen spec."""
    fields = {
        "QR_URL0001": "http://10.0.0.16:7912/spool/show/22",
        "TXT_BRAND0002": "Bambu Lab",
        "TXT_MATERIAL0003": "PLA Galaxy",
        "TXT_COLOR0004": "Purple",
        "TXT_LOCATION0005": "Cart",
        "TXT_SPOOLID0006": "Spool #22",
    }
    cmd = engine.build_command(fields, 99, FILAMENT_OBJECTS)
    assert cmd == _hand_assembled(fields, 99)
    assert b"^CR" not in cmd, "no ^CR should appear for all-single-line values"


def test_engine_validation():
    try:
        engine.build_command({"A": "x"}, 1, ())
        raise AssertionError("empty objects should raise ValueError")
    except ValueError:
        pass
    try:
        engine.build_command({"A": "x", "TYPO": "y"}, 1, ("A",))
        raise AssertionError("unknown field should raise UnknownField")
    except engine.UnknownField:
        pass
    try:
        engine.build_command({}, 1, ("A",))
        raise AssertionError("missing field should raise KeyError")
    except KeyError:
        pass


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_seed_loads():
    reg = Registry.load()
    p = reg.printer("ql720nw")
    assert p is not None and p.ip == "10.0.0.200" and p.port == 9100
    assert p.dialect == "ptouch-template"
    t = reg.template("filament_spool")
    assert t is not None and t.printer == "ql720nw" and t.template_key == 99
    assert t.objects == FILAMENT_OBJECTS
    assert t.placeholder == "None"
    assert t.owner_app == "filament-intake"


def test_registry_rejects_unknown_printer_reference():
    with tempfile.TemporaryDirectory() as tmp:
        pr = Path(tmp) / "printers.yaml"
        tp = Path(tmp) / "templates.yaml"
        pr.write_text(
            "printers:\n  p1:\n    model: X\n    ip: 1.2.3.4\n    port: 9100\n"
            "    transport: raw9100\n    dialect: ptouch-template\n"
        )
        tp.write_text(
            "templates:\n  t1:\n    printer: NOPE\n    template_key: 1\n"
            "    objects: [A]\n"
        )
        try:
            Registry.load(pr, tp)
            raise AssertionError("unknown printer reference should raise")
        except RegistryError:
            pass


# --------------------------------------------------------------------------- #
# BOTH PATHS: in-process filament module vs the service's builder
# --------------------------------------------------------------------------- #
def test_both_paths_byte_equality():
    """The Phase-M regression gate: same logical fields -> identical bytes
    whether assembled by the filament in-process path (ptouch) or by the
    engine using the SERVICE's registry data (template_key + objects)."""
    from spoolman.webapp import ptouch

    # Path A: the filament app's in-process assembly (field_key-keyed fields).
    fields_a, _warn = ptouch.build_fields(SPOOL_22_LIKE, 22)
    bytes_a = ptouch.build_command(fields_a, 99)

    # Path B: what the service assembles from the registry + object-named fields.
    reg = Registry.load()
    t = reg.template("filament_spool")
    fields_b = {obj: fields_a[fk] for fk, obj in ptouch.OBJECTS}
    bytes_b = engine.build_command(fields_b, t.template_key, t.objects)

    assert bytes_a == bytes_b, "in-process and service paths diverged!"
    # And both match the independently hand-assembled grammar.
    assert bytes_a == _hand_assembled(fields_b, 99)


# --------------------------------------------------------------------------- #
# Service HTTP contract (transport MOCKED — no socket is ever opened)
# --------------------------------------------------------------------------- #
def _client():
    from labels.service import app as service_app
    return TestClient(service_app.app), service_app


FULL_FIELDS = {
    "QR_URL0001": "http://10.0.0.16:7912/spool/show/22",
    "TXT_BRAND0002": "Bambu Lab",
    "TXT_MATERIAL0003": "PLA Galaxy",
    "TXT_COLOR0004": "Purple",
    "TXT_LOCATION0005": "Cart",
    "TXT_SPOOLID0006": "Spool #22",
}


def test_service_read_endpoints():
    client, _ = _client()
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["dialects"] == ["ptouch-template"]

    r = client.get("/templates")
    assert r.status_code == 200
    (t,) = [x for x in r.json() if x["name"] == "filament_spool"]
    assert t["fields"] == list(FILAMENT_OBJECTS) and t["template_key"] == 99

    r = client.get("/printers")
    assert r.status_code == 200
    (p,) = [x for x in r.json() if x["name"] == "ql720nw"]
    assert p["ip"] == "10.0.0.200" and p["port"] == 9100


def test_service_print_ok_and_copies_and_bytes():
    client, _ = _client()
    sent: list[tuple[bytes, str, int]] = []

    def fake_send(cmd, ip, port, timeout=engine.SOCKET_TIMEOUT):
        sent.append((cmd, ip, port))

    with patch("labels.engine.send_to_printer", side_effect=fake_send):
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": FULL_FIELDS})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["copies"] == 1
        assert body["template_key"] == 99 and body["defaulted_fields"] == []
        assert len(sent) == 1
        cmd, ip, port = sent[0]
        assert (ip, port) == ("10.0.0.200", 9100)
        # The service's bytes == the in-process filament path's bytes.
        assert cmd == _hand_assembled(FULL_FIELDS, 99)

        sent.clear()
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": FULL_FIELDS, "copies": 3})
        assert r.status_code == 200
        assert len(sent) == 3 and all(c == sent[0] for c in sent)


def test_service_missing_fields_default_to_placeholder():
    client, _ = _client()
    sent = []
    with patch("labels.engine.send_to_printer",
               side_effect=lambda cmd, ip, port, timeout=5.0: sent.append(cmd)):
        partial = {"QR_URL0001": "http://x/1", "TXT_BRAND0002": "Bambu Lab"}
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": partial})
        assert r.status_code == 200
        assert r.json()["defaulted_fields"] == [
            "TXT_COLOR0004", "TXT_LOCATION0005", "TXT_MATERIAL0003",
            "TXT_SPOOLID0006",
        ]
        expected = dict(partial, **{k: "None" for k in FILAMENT_OBJECTS
                                    if k not in partial})
        assert sent[0] == _hand_assembled(expected, 99)


def test_service_400s():
    client, _ = _client()
    with patch("labels.engine.send_to_printer") as mock_send:
        # unknown template
        r = client.post("/print", json={"template": "nope", "fields": {}})
        assert r.status_code == 400 and "unknown template" in r.json()["detail"]
        # unknown printer
        r = client.post("/print", json={"template": "filament_spool",
                                        "printer": "nope", "fields": FULL_FIELDS})
        assert r.status_code == 400 and "unknown printer" in r.json()["detail"]
        # unknown field names -> 400 ALWAYS
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": dict(FULL_FIELDS, TYPO0001="x")})
        assert r.status_code == 400 and "unknown field name" in r.json()["detail"]
        # copies bounds
        for copies in (0, 21, -3):
            r = client.post("/print", json={"template": "filament_spool",
                                            "fields": FULL_FIELDS,
                                            "copies": copies})
            assert r.status_code == 400 and "copies" in r.json()["detail"]
        assert mock_send.call_count == 0, "a 400 path must never touch transport"


def test_service_printer_mismatch_and_unimplemented_dialect():
    """Needs a registry with a second printer (incl. a raster one), so build
    temp YAMLs, point the env overrides at them, and reload the service."""
    from labels.service import app as service_app

    with tempfile.TemporaryDirectory() as tmp:
        pr = Path(tmp) / "printers.yaml"
        tp = Path(tmp) / "templates.yaml"
        pr.write_text(
            "printers:\n"
            "  ql720nw:\n    model: QL-720NW\n    ip: 10.0.0.200\n    port: 9100\n"
            "    transport: raw9100\n    dialect: ptouch-template\n"
            "  ptp750w:\n    model: PT-P750W\n    ip: 10.0.0.118\n    port: 9100\n"
            "    transport: raw9100\n    dialect: raster\n"
        )
        tp.write_text(
            "templates:\n"
            "  t_ql:\n    printer: ql720nw\n    template_key: 5\n    objects: [A]\n"
            "  t_pt:\n    printer: ptp750w\n    template_key: 1\n    objects: [A]\n"
        )
        os.environ["LABELS_PRINTERS_YAML"] = str(pr)
        os.environ["LABELS_TEMPLATES_YAML"] = str(tp)
        try:
            importlib.reload(service_app)
            client = TestClient(service_app.app)
            with patch("labels.engine.send_to_printer") as mock_send:
                # printer given but not the template's registered printer -> 400
                r = client.post("/print", json={"template": "t_ql",
                                                "printer": "ptp750w",
                                                "fields": {"A": "x"}})
                assert r.status_code == 400 and "registered to printer" in r.json()["detail"]
                # template whose printer dialect is raster -> 400 (v1 unimplemented)
                r = client.post("/print", json={"template": "t_pt",
                                                "fields": {"A": "x"}})
                assert r.status_code == 400 and "raster" in r.json()["detail"]
                assert mock_send.call_count == 0
        finally:
            del os.environ["LABELS_PRINTERS_YAML"]
            del os.environ["LABELS_TEMPLATES_YAML"]
            importlib.reload(service_app)  # restore the real registries


def test_service_unreachable_maps_to_502():
    client, _ = _client()
    with patch("labels.engine.send_to_printer",
               side_effect=engine.PrinterUnreachable("could not reach printer at 10.0.0.200:9100: test")):
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": FULL_FIELDS})
        assert r.status_code == 502
        assert "could not reach printer" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# GARAGE ordered-fill wiring (registry + service + garage client)
# --------------------------------------------------------------------------- #
# The exact 102-byte ordered stream the confirming print sent on the QL-720NW
# (key 98) on 2026-07-04 — the hardware-proven multi-line garage label. The
# wired service path (registry-resolved garage_bin) must reproduce this exactly.
PROVEN_GARAGE_ORDERED_HEX = (
    "1b6961031b405e49495e54533039385e5353303109687474703a2f2f31302e302e302e"
    "31363a383734352f6974656d2f393939094c494e45312d4f4b094141415e4352424242"
    "5e435243434309524f4f4d2d4f4b094c4f432d4f4b09434f44452d4f4b5e4646"
)

GARAGE_OBJECTS = (
    "QR_URL0001", "TXT_LABELLINE10002", "TXT_LABELLINE20003",
    "TXT_ROOM0004", "TXT_LOCATION0005", "TXT_LOCCODE0006",
)


def test_registry_garage_bin_ordered_config():
    """garage_bin loads as the ordered-fill template (key 98, numbered objects,
    TXT_LABELLINE20003 multiline); filament_spool stays on the 'named' default."""
    reg = Registry.load()
    t = reg.template("garage_bin")
    assert t is not None and t.template_key == 98
    assert t.objects == GARAGE_OBJECTS
    assert t.fill_mode == "ordered"
    assert t.multiline_fields == ("TXT_LABELLINE20003",)
    # Filament is untouched: default named path, no multiline field.
    f = reg.template("filament_spool")
    assert f.fill_mode == "named" and f.multiline_fields == ()


def test_service_garage_bin_reproduces_confirming_print():
    """END-TO-END PROOF: garage_bin fields POSTed to the service assemble to the
    EXACT 102-byte ordered stream the confirming print used on hardware — i.e.
    the wired service path == the proven direct-script path. Transport MOCKED."""
    from labels import confirm_ordered_print as C

    client, _ = _client()
    sent = []
    with patch("labels.engine.send_to_printer",
               side_effect=lambda cmd, ip, port, timeout=5.0: sent.append(cmd)):
        r = client.post("/print", json={"template": "garage_bin",
                                        "fields": C.FIELDS})
        assert r.status_code == 200, r.text
        assert r.json()["template_key"] == 98 and r.json()["defaulted_fields"] == []
        assert len(sent) == 1
    cmd = sent[0]
    # (a) byte-identical to the direct-script ordered stream, and
    # (b) byte-identical to the frozen 102-byte proven hex.
    assert cmd == engine.build_command(
        C.FIELDS, C.KEY, C.OBJECTS, mode="ordered",
        multiline_fields=C.MULTILINE_FIELDS)
    assert cmd == bytes.fromhex(PROVEN_GARAGE_ORDERED_HEX)
    assert len(cmd) == 102
    # Ordered markers present, named markers absent, two ^CR line feeds (3 lines).
    assert b"^TS098^SS01\x09" in cmd
    assert b"^ON" not in cmd and b"^DI" not in cmd
    assert cmd.count(b"^CR") == 2


def test_service_filament_routes_named_not_ordered():
    """Regression guard alongside the byte-equality gate: filament assembles on
    the NAMED path — ^ON/^DI present, and NONE of the ordered-only markers
    (^SS pin / ^CR) appear."""
    client, _ = _client()
    sent = []
    with patch("labels.engine.send_to_printer",
               side_effect=lambda cmd, ip, port, timeout=5.0: sent.append(cmd)):
        r = client.post("/print", json={"template": "filament_spool",
                                        "fields": FULL_FIELDS})
        assert r.status_code == 200
    cmd = sent[0]
    assert cmd == _hand_assembled(FULL_FIELDS, 99)  # byte-identical, named path
    assert b"^ON" in cmd and b"^DI" in cmd
    assert engine.DELIM_PIN not in cmd and b"^CR" not in cmd


def test_service_garage_bin_unsafe_field_is_clean_400():
    """A garage field carrying an ordered-path unsafe byte ('^', Tab, or a
    newline in a NON-multiline cell) is a clean 400 with a readable message —
    NOT a 500 — and transport is never touched."""
    from labels import confirm_ordered_print as C

    client, _ = _client()
    with patch("labels.engine.send_to_printer") as mock_send:
        for bad_fields in (
            dict(C.FIELDS, TXT_LABELLINE10002="LINE1^BAD"),   # '^' in Line 1
            dict(C.FIELDS, TXT_ROOM0004="A\tB"),              # Tab in Room
            dict(C.FIELDS, TXT_LOCCODE0006="GS\n644"),        # \n in a non-ML cell
        ):
            r = client.post("/print", json={"template": "garage_bin",
                                            "fields": bad_fields})
            assert r.status_code == 400, r.text
            assert "unsafe" in r.json()["detail"].lower()
        # Multi-line \n in the DESIGNATED field is fine (200).
        ok = dict(C.FIELDS, TXT_LABELLINE20003="one\ntwo\nthree")
        r = client.post("/print", json={"template": "garage_bin", "fields": ok})
        assert r.status_code == 200, r.text
        assert mock_send.call_count == 1  # only the good one printed


# --------------------------------------------------------------------------- #
# GARAGE CLIENT (garage.webapp.labels) — field mapping + clean value validation
# --------------------------------------------------------------------------- #
def _fake_garage_template() -> dict:
    return {"name": "garage_bin", "template_key": 98,
            "fields": list(GARAGE_OBJECTS), "fill_mode": "ordered",
            "multiline_fields": ["TXT_LABELLINE20003"]}


def test_garage_client_maps_numbered_names_and_flows_multiline():
    """The garage client's name-substring mapping still resolves the NUMBERED
    object names, and a multi-line label_line2 (with \\n) flows through to
    TXT_LABELLINE20003. POST is mocked — no socket, no real service."""
    from garage.webapp import labels as glabels

    item = {"label_line1": "Paint & brushes", "label_line2": "row A\nrow B",
            "room": "Garage", "location": "Shelf 3", "loc_code": "GS644"}
    captured = {}

    class _Resp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):  # noqa: D401
            return None

        def json(self):
            return {"ok": True}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _Resp()

    with patch.object(glabels, "_get_templates", return_value=[_fake_garage_template()]), \
            patch("garage.webapp.labels.requests.post", side_effect=fake_post):
        glabels.print_item_label(item, "http://10.0.0.16:8745/item/644")

    f = captured["json"]["fields"]
    assert f["QR_URL0001"] == "http://10.0.0.16:8745/item/644"
    assert f["TXT_LABELLINE10002"] == "Paint & brushes"
    assert f["TXT_LABELLINE20003"] == "row A\nrow B"   # multiline preserved
    assert f["TXT_ROOM0004"] == "Garage"
    assert f["TXT_LOCATION0005"] == "Shelf 3"
    assert f["TXT_LOCCODE0006"] == "GS644"


def test_garage_client_rejects_unsafe_value_with_clean_error():
    """An unsafe byte ('^') in a garage field raises FieldValueError (clean,
    field-named) BEFORE any POST — the app layer maps it to a 400, not a 500."""
    from garage.webapp import labels as glabels

    item = {"label_line1": "Bins", "label_line2": "ok",
            "room": "Garage", "location": "Shelf^3", "loc_code": "GS644"}
    with patch.object(glabels, "_get_templates", return_value=[_fake_garage_template()]), \
            patch("garage.webapp.labels.requests.post") as mock_post:
        try:
            glabels.print_item_label(item, "http://x/item/644")
            raise AssertionError("expected FieldValueError")
        except glabels.FieldValueError as exc:
            assert "Location" in str(exc) and "^" in str(exc)
        assert mock_post.call_count == 0  # rejected before the POST


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
