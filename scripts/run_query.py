"""Run a saved query and print the result.

Usage:
    python scripts/run_query.py queries/01_open_orders.sql
"""
import sys
from pathlib import Path

from db import query_file


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_query.py <path/to/query.sql>")
        sys.exit(1)

    df = query_file(sys.argv[1])
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
