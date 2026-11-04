#!/usr/bin/env python3
"""Check the database connection and bundled data before importing."""

import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine, text


def main():
    url = os.environ.get("DB_URL")
    if not url:
        raise SystemExit("Missing GitHub Actions secret DB_URL.")

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    url = urlunparse(parsed._replace(query=urlencode(query)))

    required_files = ["coordinates.parquet", "metadata.parquet", "annotations.parquet"]
    missing = [name for name in required_files if not os.path.isfile(name)]
    if missing:
        raise SystemExit("Missing project data files: " + ", ".join(missing))

    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 20})
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar_one()
            db_name = conn.execute(text("SELECT current_database()")).scalar_one()
            size = conn.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar_one()
            print(f"Connected to PostgreSQL {version}; database={db_name}; current size={size}.")
            print("The importer uses native PostgreSQL features and does not require extensions.")
    finally:
        engine.dispose()

    print("Preflight passed. Ready to import the Neurosynth tables.")


if __name__ == "__main__":
    main()
