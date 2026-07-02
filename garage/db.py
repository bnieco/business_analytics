"""Business-DB connection for the Garage Inventory App.

Env-driven so the SAME code runs in local dev and in the NAS container,
mirroring the filament-intake pattern (but standalone — no spoolman import):

  * Local dev reads the analytics repo ``.env`` (host ``10.0.0.16``, port
    ``5433`` — the NAS host-published Postgres port).
  * The NAS container sets ``BUSINESS_DB_HOST=postgres16`` and
    ``BUSINESS_DB_PORT=5432`` (peer container on ``postgres_default``, internal
    port — bridge containers cannot hairpin the host-published port).

Variables: ``BUSINESS_DB_HOST`` / ``BUSINESS_DB_PORT`` / ``BUSINESS_DB_NAME`` /
``BUSINESS_DB_USER`` / ``BUSINESS_DB_PASSWORD``.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TABLE = "inventory.garage_items"
IDX_SEQ = "inventory.garage_items_idx_seq"


def connect():
    """Open a psycopg2 connection to the `business` DB from the environment."""
    return psycopg2.connect(
        host=os.environ.get("BUSINESS_DB_HOST", "10.0.0.16"),
        port=os.environ.get("BUSINESS_DB_PORT", "5433"),
        dbname=os.environ.get("BUSINESS_DB_NAME", "business"),
        user=os.environ["BUSINESS_DB_USER"],
        password=os.environ["BUSINESS_DB_PASSWORD"],
    )


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def mint_loc_code(room: str, location: str, idx: int) -> str:
    """The SOP's Loc Code rule: first(Room) + first(Location) + Index.

    Applied ONCE, at creation. The stored value is never recomputed on edit —
    the code is printed on the physical bin (scope §2 nuance).
    """
    return f"{room.strip()[0].upper()}{location.strip()[0].upper()}{idx}"
