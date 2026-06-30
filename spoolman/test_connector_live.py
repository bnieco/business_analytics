"""Live end-to-end test of the Spoolman intake connector against the REAL
Spoolman API (http://10.0.0.16:7912) and the REAL `business` DB.

Exercises:
  A. Option-3 path  — new `_TEST_` barcode -> creates Vendor + Filament + N
     spools and inserts the map row.
  B. Option-2 path  — same barcode again -> reuses the mapped filament_id and
     just adds spools; no new vendor/filament; no new map row.
  C. Idempotency    — find_or_create_vendor/filament return the same ids.

Then it DELETES every `_TEST_` artifact (spools, filament, vendor, map row) and
asserts both Spoolman and the DB are clean. The real `barcode` field DEFINITION
is never touched.

Run:  ./venv/bin/python -m spoolman.test_connector_live
Exit code 0 = all assertions passed and cleanup verified clean.
"""
from __future__ import annotations

import json
import sys
import time

from spoolman import connector as C

STAMP = time.strftime("%Y%m%d%H%M%S")
BARCODE = f"_TEST_BC_{STAMP}"
BRAND = f"_TEST_Vendor_{STAMP}"


def hr(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * 4}")


def main() -> int:
    client = C.SpoolmanClient()
    hr("PRECHECK")
    info = client.info()
    print(f"Spoolman {info['version']} db={info['db_type']} base={client.base_url}")
    assert C.lookup_barcode(BARCODE) is None, "test barcode unexpectedly already mapped"
    print(f"barcode {BARCODE} not in map (clean start): OK")

    record = C.FilamentRecord(
        barcode=BARCODE,
        brand=BRAND,
        material="PLA",
        color="_TEST_ Matte Black",
        color_hex="1A1A1A",
        diameter=1.75,
        spool_weight=250,
        # density intentionally omitted -> resolved from material (PLA=1.24)
        price=19.99,
    )

    # ----------------------------------------------------------------- A: Opt3
    hr("A. OPTION 3 (new barcode -> create vendor/filament/2 spools + map row)")
    r3 = C.intake(record, roll_count=2, location="_TEST_A1")
    print(f"path={r3.path} vendor_id={r3.vendor_id} (created={r3.vendor_created}) "
          f"filament_id={r3.filament_id} (created={r3.filament_created}) "
          f"spool_ids={r3.spool_ids} mapping_created={r3.mapping_created}")
    assert r3.path == "option3"
    assert r3.vendor_created and r3.filament_created and r3.mapping_created
    assert len(r3.spool_ids) == 2
    vendor_id, filament_id = r3.vendor_id, r3.filament_id

    # verify the advisory extra.barcode round-trips (JSON-encoded contract)
    fil = client.get_filament(filament_id)
    raw_extra = fil.get("extra", {}).get("barcode")
    decoded = json.loads(raw_extra) if raw_extra is not None else None
    print(f"filament.extra.barcode raw={raw_extra!r} decoded={decoded!r}")
    assert decoded == BARCODE, "extra.barcode did not round-trip"
    print(f"density set on filament = {fil.get('density')} (expected 1.24 PLA default)")

    # verify the map row landed
    row = C.lookup_barcode(BARCODE)
    print(f"map row: {row}")
    assert row and row["spoolman_filament_id"] == filament_id and row["source"] == "scan_bind"

    # ----------------------------------------------------------------- B: Opt2
    hr("B. OPTION 2 (same barcode -> reuse filament, add 1 spool, no new map row)")
    r2 = C.intake(record, roll_count=1, location="_TEST_A2")
    print(f"path={r2.path} filament_id={r2.filament_id} spool_ids={r2.spool_ids} "
          f"mapping_created={r2.mapping_created}")
    assert r2.path == "option2"
    assert r2.filament_id == filament_id, "Option 2 must reuse the mapped filament"
    assert len(r2.spool_ids) == 1
    assert r2.mapping_created is False, "Option 2 must not insert a new map row"

    # ----------------------------------------------------------------- C: idem
    hr("C. IDEMPOTENCY (find_or_create returns same ids; map insert is no-op)")
    v_id, v_created = client.find_or_create_vendor(BRAND)
    f_id, f_created = client.find_or_create_filament(record, v_id)
    map_again = C.insert_mapping(BARCODE, filament_id, source="scan_bind")
    print(f"vendor reuse -> id={v_id} created={v_created}; "
          f"filament reuse -> id={f_id} created={f_created}; "
          f"map insert again -> inserted={map_again}")
    assert v_id == vendor_id and not v_created
    assert f_id == filament_id and not f_created
    assert map_again is False

    all_spool_ids = r3.spool_ids + r2.spool_ids
    print(f"\nTotal spools created for filament {filament_id}: "
          f"{len(all_spool_ids)} -> {all_spool_ids} (expected 3)")
    assert len(all_spool_ids) == 3

    # ----------------------------------------------------------------- CLEANUP
    hr("CLEANUP (delete all _TEST_ spools, filament, vendor, map row)")
    for sid in all_spool_ids:
        client.delete_spool(sid)
    client.delete_filament(filament_id)
    client.delete_vendor(vendor_id)
    deleted = C.delete_mapping(BARCODE)
    print(f"deleted spools={all_spool_ids} filament={filament_id} "
          f"vendor={vendor_id} map_rows={deleted}")

    # ----------------------------------------------------------------- VERIFY CLEAN
    hr("VERIFY CLEAN")
    assert C.lookup_barcode(BARCODE) is None, "map row still present after cleanup"
    assert client.find_vendor(BRAND) is None, "vendor still present after cleanup"
    # filament + spools gone
    spool_404 = filament_404 = False
    try:
        client.get_filament(filament_id)
    except Exception as e:  # noqa: BLE001
        filament_404 = "404" in str(e)
    try:
        client._get(f"spool/{all_spool_ids[0]}")
    except Exception as e:  # noqa: BLE001
        spool_404 = "404" in str(e)
    print(f"map row gone=OK  vendor gone=OK  filament 404={filament_404}  spool 404={spool_404}")
    assert filament_404 and spool_404, "Spoolman objects not fully deleted"

    # confirm the barcode FIELD DEFINITION still exists (must NOT be removed)
    fields = client._get("field/filament")
    has_barcode_field = any(f.get("key") == "barcode" for f in fields)
    print(f"barcode field DEFINITION still present = {has_barcode_field} (must be True)")
    assert has_barcode_field

    print("\nALL ASSERTIONS PASSED — Spoolman + DB verified clean; field def intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
