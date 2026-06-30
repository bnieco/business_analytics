"""Spoolman box-scan intake connector (ENR Filament Inventory System, Phase 2).

This is the server-side glue behind the iPhone intake page (Stage B). Given a
parsed filament record, a roll count, and a shelf-code location, it logs spools
into Spoolman through the REST API and maintains the ENR-owned barcode->filament
mapping table (`inventory.upc_filament_map` in the `business` DB).

Two intake paths (Scope §3 "Option 2 then Option 3 decision flow"):

  * Option 2 (barcode HIT in the map table): retrieve the mapped Spoolman
    `filament_id` and create N spools (one per physical roll) at `location`.

  * Option 3 (barcode MISS — the expected first-encounter case): find-or-create
    the Vendor, find-or-create the Filament (writing the manufacturer barcode
    into the filament's `extra.barcode` advisory copy), create N spools, then
    INSERT the authoritative barcode->filament binding row (source='scan_bind').

Vendor/Filament find-or-create are idempotent, so a repeated Option-3 submission
for the same SKU does not duplicate the vendor or filament.

Verified Spoolman API contract (Phase 1, v0.23.1):
  * Base path is `/api/v1/`; hierarchy is Vendor -> Filament -> Spool.
  * `POST /filament` REQUIRES `density` (g/cm^3).
  * Extra-field values are stored JSON-ENCODED, so `extra.barcode` must be set
    to `json.dumps(<barcode>)`, and read back with `json.loads(...)`.

Config comes from the analytics repo `.env` (BUSINESS_DB_* keys). The Spoolman
base URL defaults to the live NAS instance and can be overridden with
SPOOLMAN_BASE_URL.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SPOOLMAN_BASE_URL = os.environ.get(
    "SPOOLMAN_BASE_URL", "http://10.0.0.16:7912/api/v1"
).rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("SPOOLMAN_HTTP_TIMEOUT", "15"))

# Common filament densities (g/cm^3). `POST /filament` requires a density; for
# Option-3 manual entry we accept an explicit value, else fall back by material,
# else to PLA. These are nominal values for autofill convenience only.
DEFAULT_DENSITY: dict[str, float] = {
    "PLA": 1.24,
    "PLA+": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "NYLON": 1.14,
    "PA": 1.14,
    "PC": 1.20,
    "HIPS": 1.04,
    "PVA": 1.23,
}
_FALLBACK_DENSITY = 1.24


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class FilamentRecord:
    """A parsed filament definition coming off the intake form / barcode scan.

    `barcode` is the scanned manufacturer UPC/EAN and is always required.
    For Option 2 (known SKU) only `barcode` is consulted; the rest are ignored.
    For Option 3 (new SKU) brand/material/color drive Vendor + Filament creation.
    """

    barcode: str
    brand: Optional[str] = None          # -> Spoolman Vendor.name
    material: Optional[str] = None        # -> Filament.material (e.g. "PLA")
    color: Optional[str] = None           # human color name, used in Filament.name
    color_hex: Optional[str] = None       # e.g. "1A1A1A" (no '#')
    diameter: float = 1.75                 # mm
    spool_weight: Optional[float] = None  # nominal empty-spool weight (g)
    density: Optional[float] = None        # g/cm^3; defaulted by material if None
    price: Optional[float] = None          # filament price
    name: Optional[str] = None            # explicit Filament.name; derived if None

    def resolved_density(self) -> float:
        if self.density is not None:
            return float(self.density)
        if self.material:
            return DEFAULT_DENSITY.get(self.material.strip().upper(), _FALLBACK_DENSITY)
        return _FALLBACK_DENSITY

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        parts = [p for p in (self.material, self.color) if p]
        return " ".join(parts) if parts else (self.brand or "Filament")


@dataclass
class IntakeResult:
    """Outcome of an intake() call."""

    path: str                              # "option2" or "option3"
    barcode: str = ""
    vendor_id: Optional[int] = None
    filament_id: Optional[int] = None
    spool_ids: list[int] = field(default_factory=list)
    mapping_created: bool = False
    vendor_created: bool = False
    filament_created: bool = False


# --------------------------------------------------------------------------- #
# Spoolman REST client (thin)
# --------------------------------------------------------------------------- #
class SpoolmanClient:
    """Thin wrapper over the Spoolman `/api/v1/` REST API."""

    def __init__(self, base_url: str = SPOOLMAN_BASE_URL, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # -- low level ---------------------------------------------------------- #
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, path: str, **params) -> Any:
        r = self.session.get(self._url(path), params=params or None, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> Any:
        r = self.session.post(self._url(path), json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        r = self.session.delete(self._url(path), timeout=self.timeout)
        r.raise_for_status()

    # -- health ------------------------------------------------------------- #
    def info(self) -> dict:
        return self._get("info")

    # -- vendor ------------------------------------------------------------- #
    def find_vendor(self, name: str) -> Optional[dict]:
        """Return the vendor whose name matches `name` (case-insensitive), else None."""
        target = name.strip().casefold()
        for v in self._get("vendor"):
            if (v.get("name") or "").strip().casefold() == target:
                return v
        return None

    def find_or_create_vendor(self, name: str) -> tuple[int, bool]:
        """Idempotent. Returns (vendor_id, created)."""
        existing = self.find_vendor(name)
        if existing:
            return existing["id"], False
        created = self._post("vendor", {"name": name})
        return created["id"], True

    # -- filament ----------------------------------------------------------- #
    def find_filament(
        self, vendor_id: int, name: str, material: Optional[str] = None
    ) -> Optional[dict]:
        """Find an existing filament for this vendor by (name, material).

        Idempotency key is vendor_id + name (+ material if given). Spoolman's
        `GET /filament` cannot filter by extra fields, but it does filter by
        vendor.id, so we scope server-side then match name/material client-side.
        """
        name_target = name.strip().casefold()
        mat_target = material.strip().casefold() if material else None
        for f in self._get("filament", **{"vendor.id": vendor_id}):
            if (f.get("name") or "").strip().casefold() != name_target:
                continue
            if mat_target is not None and (f.get("material") or "").strip().casefold() != mat_target:
                continue
            return f
        return None

    def find_or_create_filament(self, rec: "FilamentRecord", vendor_id: int) -> tuple[int, bool]:
        """Idempotent. Returns (filament_id, created).

        Writes the manufacturer barcode into the advisory `extra.barcode` field
        (JSON-encoded, per the verified Spoolman contract).
        """
        name = rec.resolved_name()
        existing = self.find_filament(vendor_id, name, rec.material)
        if existing:
            return existing["id"], False

        payload: dict[str, Any] = {
            "vendor_id": vendor_id,
            "name": name,
            "diameter": rec.diameter,
            "density": rec.resolved_density(),  # REQUIRED by POST /filament
        }
        if rec.material:
            payload["material"] = rec.material
        if rec.color_hex:
            payload["color_hex"] = rec.color_hex.lstrip("#")
        if rec.spool_weight is not None:
            payload["spool_weight"] = rec.spool_weight
        if rec.price is not None:
            payload["price"] = rec.price
        if rec.barcode:
            # Extra-field values are stored JSON-encoded (Phase 1 finding).
            payload["extra"] = {"barcode": json.dumps(rec.barcode)}

        created = self._post("filament", payload)
        return created["id"], True

    def get_filament(self, filament_id: int) -> dict:
        return self._get(f"filament/{filament_id}")

    # -- spool -------------------------------------------------------------- #
    def create_spool(
        self,
        filament_id: int,
        location: Optional[str] = None,
        lot_nr: Optional[str] = None,
        price: Optional[float] = None,
        remaining_weight: Optional[float] = None,
    ) -> dict:
        payload: dict[str, Any] = {"filament_id": filament_id}
        if location:
            payload["location"] = location
        if lot_nr:
            payload["lot_nr"] = lot_nr
        if price is not None:
            payload["price"] = price
        if remaining_weight is not None:
            payload["remaining_weight"] = remaining_weight
        return self._post("spool", payload)

    def create_spools(
        self,
        filament_id: int,
        count: int,
        location: Optional[str] = None,
        lot_nr: Optional[str] = None,
        price: Optional[float] = None,
        remaining_weight: Optional[float] = None,
    ) -> list[int]:
        """Create `count` physical spools (one Spoolman Spool row per roll)."""
        if count < 1:
            raise ValueError("roll count must be >= 1")
        ids: list[int] = []
        for _ in range(count):
            spool = self.create_spool(
                filament_id, location=location, lot_nr=lot_nr,
                price=price, remaining_weight=remaining_weight,
            )
            ids.append(spool["id"])
        return ids

    # -- delete (used by tests / reconciliation) ---------------------------- #
    def delete_spool(self, spool_id: int) -> None:
        self._delete(f"spool/{spool_id}")

    def delete_filament(self, filament_id: int) -> None:
        self._delete(f"filament/{filament_id}")

    def delete_vendor(self, vendor_id: int) -> None:
        self._delete(f"vendor/{vendor_id}")


# --------------------------------------------------------------------------- #
# Mapping-table data access (`inventory.upc_filament_map` in `business` DB)
# --------------------------------------------------------------------------- #
def _db_conn():
    """Open a psycopg2 connection to the `business` DB from .env."""
    return psycopg2.connect(
        host=os.environ.get("BUSINESS_DB_HOST", "10.0.0.16"),
        port=os.environ.get("BUSINESS_DB_PORT", "5433"),
        dbname=os.environ.get("BUSINESS_DB_NAME", "business"),
        user=os.environ["BUSINESS_DB_USER"],
        password=os.environ["BUSINESS_DB_PASSWORD"],
    )


def lookup_barcode(barcode: str, conn=None) -> Optional[dict]:
    """Return the mapping row for `barcode`, or None. The map is the SSOT."""
    own = conn is None
    conn = conn or _db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT barcode, spoolman_filament_id, brand, material, color, "
                "source, created_at FROM inventory.upc_filament_map WHERE barcode = %s",
                (barcode,),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def insert_mapping(
    barcode: str,
    spoolman_filament_id: int,
    brand: Optional[str] = None,
    material: Optional[str] = None,
    color: Optional[str] = None,
    source: str = "scan_bind",
    conn=None,
) -> bool:
    """Insert a barcode->filament binding. Idempotent (ON CONFLICT DO NOTHING).

    Returns True if a new row was inserted, False if the barcode already existed.
    """
    own = conn is None
    conn = conn or _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inventory.upc_filament_map "
                "(barcode, spoolman_filament_id, brand, material, color, source) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (barcode) DO NOTHING",
                (barcode, spoolman_filament_id, brand, material, color, source),
            )
            inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    finally:
        if own:
            conn.close()


def delete_mapping(barcode: str, conn=None) -> int:
    """Delete a mapping row (used by tests / reconciliation). Returns rows deleted."""
    own = conn is None
    conn = conn or _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM inventory.upc_filament_map WHERE barcode = %s", (barcode,)
            )
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Intake orchestration (the Option 2 / Option 3 decision flow)
# --------------------------------------------------------------------------- #
def intake(
    record: FilamentRecord,
    roll_count: int,
    location: str,
    *,
    client: Optional[SpoolmanClient] = None,
    lot_nr: Optional[str] = None,
    price: Optional[float] = None,
    remaining_weight: Optional[float] = None,
) -> IntakeResult:
    """Log `roll_count` physical spools for a scanned box at `location`.

    Decision flow (Scope §3):
      1. Look the scanned barcode up in `inventory.upc_filament_map`.
      2. HIT  -> Option 2: reuse the mapped filament_id, create N spools.
      3. MISS -> Option 3: find-or-create Vendor + Filament (with extra.barcode),
                 create N spools, then INSERT the barcode->filament mapping row.

    `record.barcode` is required. For Option 3, `record.brand` (vendor) is also
    required. Returns an IntakeResult with the ids touched.
    """
    if not record.barcode:
        raise ValueError("record.barcode is required")
    if roll_count < 1:
        raise ValueError("roll_count must be >= 1")

    client = client or SpoolmanClient()
    hit = lookup_barcode(record.barcode)

    if hit:
        # -------- Option 2: known SKU, fast path ---------------------------- #
        filament_id = hit["spoolman_filament_id"]
        spool_ids = client.create_spools(
            filament_id, roll_count, location=location,
            lot_nr=lot_nr, price=price, remaining_weight=remaining_weight,
        )
        return IntakeResult(
            path="option2",
            barcode=record.barcode,
            filament_id=filament_id,
            spool_ids=spool_ids,
        )

    # ------------ Option 3: first encounter, manual entry + bind ------------ #
    if not record.brand:
        raise ValueError("Option 3 (new barcode) requires record.brand for the vendor")

    vendor_id, vendor_created = client.find_or_create_vendor(record.brand)
    filament_id, filament_created = client.find_or_create_filament(record, vendor_id)
    spool_ids = client.create_spools(
        filament_id, roll_count, location=location,
        lot_nr=lot_nr, price=price, remaining_weight=remaining_weight,
    )
    mapping_created = insert_mapping(
        record.barcode,
        filament_id,
        brand=record.brand,
        material=record.material,
        color=record.color,
        source="scan_bind",
    )
    return IntakeResult(
        path="option3",
        barcode=record.barcode,
        vendor_id=vendor_id,
        filament_id=filament_id,
        spool_ids=spool_ids,
        mapping_created=mapping_created,
        vendor_created=vendor_created,
        filament_created=filament_created,
    )
