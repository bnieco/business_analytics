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

Config is environment-driven so the SAME code runs in local dev and in the NAS
container:

  * Spoolman API base: ``SPOOLMAN_API_URL`` (default the local-dev
    ``http://10.0.0.16:7912``). The ``/api/v1`` suffix is appended automatically
    if absent, so the NAS container just sets ``SPOOLMAN_API_URL=http://spoolman:8000``
    (container name + internal port on the ``postgres_default`` Docker network).
    The legacy ``SPOOLMAN_BASE_URL`` (which may include ``/api/v1``) is still honored.
  * Business DB: ``BUSINESS_DB_HOST`` / ``BUSINESS_DB_PORT`` / ``BUSINESS_DB_NAME`` /
    ``BUSINESS_DB_USER`` / ``BUSINESS_DB_PASSWORD``. Local dev reads these from the
    analytics repo ``.env`` (host ``10.0.0.16``, port ``5433``); the NAS container
    sets host ``postgres16`` port ``5432`` (peer container, internal port).
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

def _resolve_spoolman_base_url() -> str:
    """Resolve the Spoolman ``/api/v1`` base URL from the environment.

    Precedence: ``SPOOLMAN_API_URL`` (new, the documented knob), then the legacy
    ``SPOOLMAN_BASE_URL``, then the local-dev default. Whatever is supplied, the
    ``/api/v1`` path suffix is appended if missing so callers can set a bare
    origin (e.g. ``http://spoolman:8000`` on the NAS) and still hit the API.
    """
    raw = (
        os.environ.get("SPOOLMAN_API_URL")
        or os.environ.get("SPOOLMAN_BASE_URL")
        or "http://10.0.0.16:7912"
    ).rstrip("/")
    if not raw.endswith("/api/v1"):
        raw = f"{raw}/api/v1"
    return raw


SPOOLMAN_BASE_URL = _resolve_spoolman_base_url()
HTTP_TIMEOUT = float(os.environ.get("SPOOLMAN_HTTP_TIMEOUT", "15"))

# Accepted manufacturer-barcode lengths: UPC-A (12) and EAN-13 (13). The scanner
# was previously false-decoding 8-digit (UPC_E/EAN_8) fragments of the real
# EAN-13 box code and binding garbage; this set is the server-side guard. Tunable
# so a future format (e.g. EAN_8) can be allowed by editing one line.
VALID_BARCODE_LENGTHS: set[int] = {12, 13}


class InvalidBarcodeError(ValueError):
    """Raised when a barcode is not a valid UPC-A / EAN-13 numeric string."""


def validate_barcode(barcode: Optional[str]) -> str:
    """Validate and normalize a scanned/typed barcode.

    Accepts only an all-numeric string whose length is in ``VALID_BARCODE_LENGTHS``
    (12 = UPC-A, 13 = EAN-13). Leading/trailing whitespace is stripped. Raises
    ``InvalidBarcodeError`` (a ``ValueError``) with an operator-facing message on
    anything else. Returns the cleaned barcode on success.
    """
    cleaned = (barcode or "").strip()
    if not cleaned.isdigit() or len(cleaned) not in VALID_BARCODE_LENGTHS:
        lengths = "- or ".join(str(n) for n in sorted(VALID_BARCODE_LENGTHS))
        raise InvalidBarcodeError(
            f"Barcode must be a {lengths}-digit UPC/EAN (got '{cleaned}')."
        )
    return cleaned

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

    def filament_exists(self, filament_id: int) -> bool:
        """Return True if `filament_id` still resolves in Spoolman, False on 404.

        Used to detect orphaned mappings (a map row whose Spoolman filament was
        deleted). Any non-404 HTTP/transport error is re-raised so a transient
        Spoolman outage is NOT mistaken for a deletion.
        """
        try:
            r = self.session.get(self._url(f"filament/{filament_id}"), timeout=self.timeout)
        except requests.RequestException:
            raise
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

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


def resolve_mapping(
    barcode: str, client: Optional["SpoolmanClient"] = None
) -> Optional[dict]:
    """Look up a barcode AND verify the mapped Spoolman filament still exists.

    This is the orphan-aware resolver (Fix 4). On a map HIT whose
    `spoolman_filament_id` no longer resolves in Spoolman (404 — the filament was
    deleted out from under the binding), the stale map row is DELETED and None is
    returned, so callers treat it as a miss (prompt for re-entry / re-create).

    Returns the mapping dict on a live hit, else None.
    """
    client = client or SpoolmanClient()
    hit = lookup_barcode(barcode)
    if not hit:
        return None
    if client.filament_exists(hit["spoolman_filament_id"]):
        return hit
    # Orphaned binding: drop the stale row so the barcode is re-bindable.
    delete_mapping(barcode)
    return None


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
    # Orphan-aware: a HIT whose mapped filament was deleted in Spoolman is
    # cleaned up here and treated as a miss, so we fall through to Option 3 and
    # re-create + re-bind from the provided attributes instead of erroring.
    hit = resolve_mapping(record.barcode, client)

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
