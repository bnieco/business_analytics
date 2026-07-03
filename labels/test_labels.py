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


def test_engine_multiline_object_uses_cr_not_di():
    """A value with a newline is emitted for that object as raw line segments
    joined by the ^CR line-feed COMMAND (5E 43 52), with NO ^DI wrapper.
    Single-line objects in the SAME command keep the ^ON + ^DI path and stay
    byte-identical to the pre-multiline engine (the regression gate).

    Byte-level / dry-run ONLY — nothing is sent to a printer.
    """
    objects = ("TXT_LINE1", "TXT_LINE2")
    fields = {"TXT_LINE1": "Garage", "TXT_LINE2": "AAA\nBBB\nCCC"}
    cmd = engine.build_command(fields, 98, objects)

    # Frame is unchanged.
    assert cmd.startswith(b"\x1b\x69\x61\x03\x1b\x40^II^TS098")
    assert cmd.endswith(b"^FF")

    # Object 1 (single line) is the EXACT old ^ON + ^DI grammar, byte-for-byte.
    line1 = "Garage".encode()
    seg1 = b"^ONTXT_LINE1\x00" + b"^DI" + len(line1).to_bytes(2, "little") + line1
    assert seg1 in cmd

    # Object 2 (multi-line): ^ON + raw "AAA" ^CR "BBB" ^CR "CCC", and NO ^DI
    # anywhere in that object's segment.
    seg2 = b"^ONTXT_LINE2\x00" + b"AAA" + b"^CR" + b"BBB" + b"^CR" + b"CCC"
    assert seg2 in cmd
    # The whole command, assembled by hand, matches exactly.
    expected = (b"\x1b\x69\x61\x03\x1b\x40^II^TS098" + seg1 + seg2 + b"^FF")
    assert cmd == expected, (cmd, expected)
    # And the multi-line object carries no ^DI byte-count insert.
    assert cmd.count(b"^DI") == 1, "multi-line object must not emit ^DI"
    assert cmd.count(b"^CR") == 2, "two line feeds for a three-line value"


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


def test_engine_multiline_normalizes_crlf_and_cr():
    """\\r\\n and \\r are normalized to the same segmentation as \\n."""
    a = engine.build_command({"A": "X\r\nY"}, 1, ("A",))
    b = engine.build_command({"A": "X\rY"}, 1, ("A",))
    c = engine.build_command({"A": "X\nY"}, 1, ("A",))
    assert a == b == c
    assert b"^ONA\x00X^CRY" in c


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
