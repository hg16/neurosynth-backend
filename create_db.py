#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PostgreSQL loader (accelerated) with:
- Native x/y/z coordinate columns (no database extensions required)
- FTS (tsvector) + trigger (+ GIN) for metadata
- Fast annotations_terms via NumPy + COPY
- Optional annotations_json aggregation (+ GIN) via --enable-json

Default schema: ns
"""

import argparse
import os
import io
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from typing import List, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


# -----------------------------
# Args
# -----------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Load Neurosynth Parquet data into PostgreSQL.")
    ap.add_argument("--url", required=True, help="SQLAlchemy DB URL (postgresql://user:pass@host/db)")
    ap.add_argument("--data-dir", default="./", help="Directory containing Parquet files")
    ap.add_argument("--schema", default="ns", help="Target schema (default: ns)")
    ap.add_argument("--if-exists", choices=["replace", "append"], default="replace", help="Behavior for coordinates/metadata")
    ap.add_argument("--batch-cols", type=int, default=150, help="terms_* columns to melt per batch (smaller uses less RAM)")
    ap.add_argument("--stage-chunksize", type=int, default=10000, help="pandas.to_sql() insert batch size")
    ap.add_argument("--enable-json", action="store_true", help="Also build annotations_json (slow)")
    return ap.parse_args()


# -----------------------------
# Helpers
# -----------------------------
def load_parquet(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return pd.read_parquet(path)


def ensure_schema(engine: Engine, schema: str):
    if not schema or not schema.replace("_", "").isalnum() or schema[0].isdigit():
        raise ValueError("Schema names must use letters, numbers, and underscores, and cannot start with a number.")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema};"))
        conn.execute(text(f"SET search_path TO {schema}, public;"))


def is_finite_series(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").astype(float)
    return np.isfinite(s.to_numpy(copy=False))


# -----------------------------
# Coordinates (native PostgreSQL columns)
# -----------------------------
def build_coordinates(engine: Engine, df: pd.DataFrame, schema: str, chunksize: int, if_exists: str):
    print("→ coordinates: preparing dataframe")
    must_have = ["study_id", "x", "y", "z"]
    missing = [c for c in must_have if c not in df.columns]
    if missing:
        raise KeyError(f"coordinates missing columns: {missing}")
    df = df[must_have].copy()
    df["study_id"] = df["study_id"].astype(str)
    for c in ["x", "y", "z"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    finite_mask = is_finite_series(df["x"]) & is_finite_series(df["y"]) & is_finite_series(df["z"])
    bad = (~finite_mask).sum()
    if bad:
        print(f"   … dropping {bad:,} non-finite rows from coordinates")
    df = df[finite_mask].reset_index(drop=True)

    with engine.begin() as conn:
        if if_exists == "replace":
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.coordinates CASCADE;"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {schema}.coordinates (
                study_id TEXT NOT NULL,
                x DOUBLE PRECISION NOT NULL,
                y DOUBLE PRECISION NOT NULL,
                z DOUBLE PRECISION NOT NULL
            );
        """))

    print("→ coordinates: loading rows")
    df.to_sql("coordinates", engine, schema=schema, if_exists="append", index=False,
              chunksize=chunksize)

    with engine.begin() as conn:
        print("→ coordinates: indexing & analyze")
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_coordinates_study ON {schema}.coordinates (study_id);"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_coordinates_xyz ON {schema}.coordinates (x, y, z);"))
        conn.execute(text(f"ANALYZE {schema}.coordinates;"))
    print("→ coordinates (x/y/z) done.")


# -----------------------------
# Metadata (FTS tsvector + trigger + GIN)
# -----------------------------
def build_metadata(engine: Engine, df: pd.DataFrame, schema: str, if_exists: str):
    print("→ metadata: preparing & creating table")
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    # Ensure 'year' column is stored as integer (not float)
    if "year" in df.columns:
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    with engine.begin() as conn:
        if if_exists == "replace":
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.metadata CASCADE;"))
        cols = []
        for c in df.columns:
            if pd.api.types.is_numeric_dtype(df[c]):
                cols.append(f"{c} INTEGER" if c == "year" else f"{c} DOUBLE PRECISION")
            else:
                cols.append(f"{c} TEXT")
        cols.append("fts tsvector")
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {schema}.metadata ({', '.join(cols)});"))

    print("→ metadata: bulk inserting (to_sql)")
    df.to_sql("metadata", engine, schema=schema, if_exists="append", index=False, chunksize=2000)

    with engine.begin() as conn:
        if "study_id" in df.columns:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_metadata_study_id ON {schema}.metadata (study_id);"))
        res = conn.execute(text(f"""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = 'metadata';
        """), {"schema": schema}).fetchall()
        text_cols = [r[0] for r in res if r[0] != "fts" and r[1] in ("text", "character varying")]
        if text_cols:
            cols_expr = " || ' ' || ".join([f"coalesce({c},'')" for c in text_cols])
            print("→ metadata: computing tsvector over text columns")
            conn.execute(text(f"UPDATE {schema}.metadata SET fts = to_tsvector('pg_catalog.english', {cols_expr});"))
            print("→ metadata: creating GIN index & trigger")
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_metadata_fts ON {schema}.metadata USING GIN (fts);"))
            conn.execute(text(f"DROP TRIGGER IF EXISTS metadata_fts_update ON {schema}.metadata;"))
            conn.execute(text(f"""
                CREATE TRIGGER metadata_fts_update
                BEFORE INSERT OR UPDATE ON {schema}.metadata
                FOR EACH ROW EXECUTE FUNCTION tsvector_update_trigger(
                    'fts', 'pg_catalog.english', {", ".join(text_cols)}
                );
            """))
            conn.execute(text(f"ANALYZE {schema}.metadata;"))
    print("→ metadata (FTS + trigger) done.")


# -----------------------------
# Annotations -> sparse terms via NumPy + COPY (+ optional JSONB)
# -----------------------------
def copy_terms(engine: Engine, schema: str, rows: List[Tuple[str, str, str, float]]):
    buf = io.StringIO()

    def copy_escape(value: str) -> str:
        return (str(value).replace("\\", "\\\\").replace("\t", "\\t")
                .replace("\n", "\\n").replace("\r", "\\r"))

    for study_id, contrast_id, term, weight in rows:
        # COPY text format uses \N for NULL
        cval = r'\N' if contrast_id is None else copy_escape(contrast_id)
        buf.write(f"{copy_escape(study_id)}\t{cval}\t{copy_escape(term)}\t{weight}\n")
    buf.seek(0)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("SET LOCAL synchronous_commit = off;")
            cur.copy_expert(
                f"COPY {schema}.annotations_terms (study_id, contrast_id, term, weight) FROM STDIN WITH (FORMAT text)",
                buf,
            )
        raw.commit()
    finally:
        raw.close()


def build_annotations(engine: Engine, df: pd.DataFrame, schema: str, batch_cols: int, enable_json: bool = False):
    print("→ annotations: preparing")
    fixed = {"id", "study_id", "contrast_id"}
    term_cols = [c for c in df.columns if c not in fixed and str(c).startswith("terms_")]
    if not term_cols:
        raise RuntimeError("No term columns found in annotations.*")

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {schema}.annotations_terms CASCADE;"))
        conn.execute(text(f"""
            CREATE TABLE {schema}.annotations_terms (
                study_id    TEXT NOT NULL,
                contrast_id TEXT,
                term        TEXT NOT NULL,
                weight      DOUBLE PRECISION NOT NULL
            );
        """))
        if enable_json:
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.annotations_json CASCADE;"))
            conn.execute(text(f"""
                CREATE TABLE {schema}.annotations_json (
                    study_id    TEXT NOT NULL,
                    contrast_id TEXT,
                    terms       JSONB NOT NULL,
                    PRIMARY KEY (study_id, contrast_id)
                );
            """))

    id_vars = ["study_id", "contrast_id"]
    total_inserted = 0

    # Pre-extract ID arrays once
    sid_arr = df["study_id"].astype(str).to_numpy(copy=False)
    cid_series = df["contrast_id"] if "contrast_id" in df.columns else pd.Series([None]*len(df))
    cid_arr = cid_series.where(pd.notna(cid_series), None).astype(object).to_numpy(copy=False)

    for i in range(0, len(term_cols), batch_cols):
        cols = term_cols[i:i+batch_cols]

        # Filter out columns that are entirely null or <= 0 to avoid useless processing
        nonempty = [c for c in cols if (pd.to_numeric(df[c], errors="coerce") > 0).any()]
        if not nonempty:
            continue

        # Build rows with NumPy (avoid huge melted DataFrame)
        term_rows: List[Tuple[str, str, str, float]] = []
        for c in nonempty:
            col = pd.to_numeric(df[c], errors="coerce").to_numpy(copy=False)
            mask = np.isfinite(col) & (col > 0)
            if not mask.any():
                continue
            idx = np.nonzero(mask)[0]
            # Feature columns use prefixes such as terms_abstract_tfidf__;
            # remove the complete feature family before storing the vocabulary.
            term = re.sub(r"^terms_.*?__", "", str(c)).strip().lower()
            term_rows.extend(zip(sid_arr[idx], cid_arr[idx], [term]*len(idx), col[idx].astype(float)))

        if term_rows:
            copy_terms(engine, schema, term_rows)
            total_inserted += len(term_rows)
            print(f"   … copied {len(term_rows):,} rows (cumulative {total_inserted:,})")

    # Indexes AFTER bulk load (faster)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_annotations_terms_study ON {schema}.annotations_terms (study_id);"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_annotations_terms_normalized_study ON {schema}.annotations_terms (lower(trim(term)), study_id);"))
        conn.execute(text(f"ANALYZE {schema}.annotations_terms;"))
        # The source contrast_id may be nullable, so do not impose a composite
        # primary key. The query indexes below are sufficient for this API.

        if enable_json:
            print("→ annotations_json: aggregating (this may take a while)")
            conn.execute(text("SET LOCAL work_mem = '512MB';"))
            conn.execute(text("SET LOCAL maintenance_work_mem = '1GB';"))
            conn.execute(text(f"""
                INSERT INTO {schema}.annotations_json (study_id, contrast_id, terms)
                SELECT study_id, contrast_id, jsonb_object_agg(term, weight)
                FROM {schema}.annotations_terms
                GROUP BY study_id, contrast_id
                ON CONFLICT (study_id, contrast_id) DO UPDATE
                SET terms = EXCLUDED.terms;
            """))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_annotations_json_terms_gin ON {schema}.annotations_json USING GIN (terms);"))
            conn.execute(text(f"ANALYZE {schema}.annotations_json;"))

    print(f"→ annotations_terms total inserted: {total_inserted:,}")
    if enable_json:
        print("   … annotations_json populated and indexed.")
    print("   … annotations done.")


# -----------------------------
# Main
# -----------------------------
def main():
    args = parse_args()
    # Render external PostgreSQL connections require TLS. Add sslmode=require
    # only when the supplied URL does not already specify an SSL mode.
    parsed = urlparse(args.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    db_url = urlunparse(parsed._replace(query=urlencode(query)))
    engine = create_engine(db_url, pool_pre_ping=True)

    ensure_schema(engine, args.schema)

    # --- Basic connectivity sanity checks & status prints ---
    with engine.begin() as conn:
        sv = conn.execute(text("SELECT version();")).fetchone()
        db = conn.execute(text("SELECT current_database();")).fetchone()
        sch = conn.execute(text("SELECT current_schema();")).fetchone()
    print("✅ server_version:", sv[0].splitlines()[0])
    print("✅ current_database:", db[0])
    print("✅ current_schema:", sch[0])

    # Load Parquet files
    print("📦 loading Parquet files...")
    coords = load_parquet(os.path.join(args.data_dir, "coordinates.parquet"))
    meta   = load_parquet(os.path.join(args.data_dir, "metadata.parquet"))
    ann    = load_parquet(os.path.join(args.data_dir, "annotations.parquet"))
    print(f"📐 shapes -> coordinates: {coords.shape}, metadata: {meta.shape}, annotations: {ann.shape}")

    # Build
    print("\n=== Build: coordinates ===")
    build_coordinates(engine, coords, args.schema, args.stage_chunksize, args.if_exists)

    print("\n=== Build: metadata ===")
    build_metadata(engine, meta, args.schema, args.if_exists)

    print("\n=== Build: annotations ===")
    build_annotations(engine, ann, args.schema, args.batch_cols, enable_json=args.enable_json)

    print("\n=== Ready ===")
    print(f"- coordinates  : {args.schema}.coordinates (x/y/z + B-tree index)")
    print(f"- metadata     : {args.schema}.metadata (FTS + trigger + GIN)")
    print(f"- annotations  : {args.schema}.annotations_terms (sparse via COPY)" + (" + annotations_json (GIN)" if args.enable_json else ""))


if __name__ == "__main__":
    main()
