"""Shared database connection helpers for the `business` Postgres DB."""
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_engine() -> Engine:
    """Build a SQLAlchemy engine from .env."""
    user = os.environ["BUSINESS_DB_USER"]
    password = os.environ["BUSINESS_DB_PASSWORD"]
    host = os.environ.get("BUSINESS_DB_HOST", "10.0.0.16")
    port = os.environ.get("BUSINESS_DB_PORT", "5433")
    db = os.environ.get("BUSINESS_DB_NAME", "business")
    return create_engine(f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}")


def query(sql: str, **params) -> pd.DataFrame:
    """Run SQL and return a DataFrame."""
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or None)


def query_file(path: str | Path, **params) -> pd.DataFrame:
    """Run a .sql file and return a DataFrame."""
    sql_path = PROJECT_ROOT / path if not Path(path).is_absolute() else Path(path)
    return query(sql_path.read_text(), **params)
