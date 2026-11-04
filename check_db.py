#!/usr/bin/env python3
"""Report PostgreSQL version and whether common extensions are installed."""

import argparse
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine, text


def main():
    parser = argparse.ArgumentParser(description="Inspect a PostgreSQL database without changing it.")
    parser.add_argument("--url", required=True, help="PostgreSQL connection URL")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    db_url = urlunparse(parsed._replace(query=urlencode(query)))

    engine = create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 20})
    try:
        with engine.connect() as conn:
            print("PostgreSQL version:", conn.execute(text("SHOW server_version")).scalar_one())
            print("Database name:", conn.execute(text("SELECT current_database()")).scalar_one())
            print("Port:", conn.execute(text("SHOW port")).scalar_one())
            installed = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
            print("Extensions (installed status only; this command does not enable extensions):")
            for extension in ("postgis", "pg_trgm", "unaccent"):
                print(f"  {extension}: {'installed' if extension in installed else 'not installed'}")
            print("Required for this backend: none.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
