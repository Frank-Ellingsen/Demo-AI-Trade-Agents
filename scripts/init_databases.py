"""Initialize SQLite (WAL mode) and DuckDB analytical schemas for the 5-agent OBX trading desk."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.duckdb_store import init_duckdb
from src.db.sqlite_store import init_sqlite_db


def main() -> None:
    sqlite_path = init_sqlite_db()
    duckdb_path = init_duckdb()
    print(f"[OK] Agent 4 SQLite WAL Queue initialized : {sqlite_path}")
    print(f"[OK] Agent 1/2/3/5 DuckDB Store initialized: {duckdb_path}")


if __name__ == "__main__":
    main()
