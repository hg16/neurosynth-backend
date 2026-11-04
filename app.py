"""Neurosynth functional-dissociation API (Flask + PostgreSQL).

Database tables are created by create_db.py in the `ns` schema. Coordinate
distance queries use native x/y/z columns, so the service does not require
PostGIS or any other PostgreSQL extension.
Set DB_URL (or DATABASE_URL) in the deployment environment.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_file
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

_engine = None


def get_engine():
    """Create one pooled SQLAlchemy engine per application process."""
    global _engine
    if _engine is not None:
        return _engine

    db_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("Missing DB_URL (or DATABASE_URL) environment variable.")
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    connect_args = {}
    # Render/external PostgreSQL connections commonly require TLS.
    if os.getenv("DB_SSLMODE"):
        connect_args["sslmode"] = os.environ["DB_SSLMODE"]

    _engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=connect_args,
    )
    return _engine


def normalize_term(value: str) -> str:
    """Accept ordinary spaces or underscores in URL path terms."""
    return " ".join(value.replace("_", " ").strip().lower().split())


def parse_coords(value: str) -> tuple[float, float, float]:
    try:
        parts = value.split("_")
        if len(parts) != 3:
            raise ValueError
        xyz = tuple(float(part) for part in parts)
        if not all(math.isfinite(v) for v in xyz):
            raise ValueError
        return xyz  # type: ignore[return-value]
    except (TypeError, ValueError):
        raise ValueError("Coordinates must use the format x_y_z, e.g. 0_-52_26.")


def get_limit() -> int:
    raw = request.args.get("limit", "100")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("limit must be an integer.")
    if not 1 <= value <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    return value


def get_radius() -> float:
    raw = request.args.get("radius", "6")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("radius must be a number (in the coordinate units used by the dataset).")
    if not math.isfinite(value) or not 0 < value <= 30:
        raise ValueError("radius must be greater than 0 and no more than 30.")
    return value


def rows_as_dicts(rows) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in rows]


def metadata_for_studies(conn, study_ids: list[str], limit: int):
    """Return metadata records for a set of matching study IDs."""
    if not study_ids:
        return []
    result = conn.execute(
        text("""
            SELECT m.*
            FROM ns.metadata AS m
            WHERE m.study_id = ANY(:study_ids)
            ORDER BY m.study_id
            LIMIT :limit
        """),
        {"study_ids": study_ids, "limit": limit},
    ).fetchall()
    return rows_as_dicts(result)


def studies_for_term(conn, term: str, limit: int) -> list[dict[str, Any]]:
    term = normalize_term(term)
    ids = conn.execute(
        text("""
            SELECT DISTINCT study_id
            FROM ns.annotations_terms
            WHERE lower(trim("term")) = :term
            ORDER BY study_id
            LIMIT :limit
        """),
        {"term": term, "limit": limit},
    ).scalars().all()
    return metadata_for_studies(conn, list(ids), limit)


def studies_for_location(conn, xyz: tuple[float, float, float], radius: float, limit: int):
    x, y, z = xyz
    ids = conn.execute(
        text("""
            SELECT DISTINCT study_id
            FROM ns.coordinates
            WHERE x BETWEEN :x_min AND :x_max
              AND y BETWEEN :y_min AND :y_max
              AND z BETWEEN :z_min AND :z_max
              AND (x - :x) * (x - :x)
                + (y - :y) * (y - :y)
                + (z - :z) * (z - :z) <= :radius_squared
            ORDER BY study_id
            LIMIT :limit
        """),
        {
            "x": x, "y": y, "z": z,
            "x_min": x - radius, "x_max": x + radius,
            "y_min": y - radius, "y_max": y + radius,
            "z_min": z - radius, "z_max": z + radius,
            "radius_squared": radius * radius,
            "limit": limit,
        },
    ).scalars().all()
    return metadata_for_studies(conn, list(ids), limit)


def dissociate_terms(conn, term_a: str, term_b: str, limit: int):
    a, b = normalize_term(term_a), normalize_term(term_b)
    ids = conn.execute(
        text("""
            SELECT DISTINCT a.study_id
            FROM ns.annotations_terms AS a
            WHERE lower(trim(a.term)) = :term_a
              AND NOT EXISTS (
                  SELECT 1
                  FROM ns.annotations_terms AS b
                  WHERE b.study_id = a.study_id
                    AND lower(trim(b.term)) = :term_b
              )
            ORDER BY a.study_id
            LIMIT :limit
        """),
        {"term_a": a, "term_b": b, "limit": limit},
    ).scalars().all()
    return metadata_for_studies(conn, list(ids), limit)


def dissociate_locations(conn, xyz_a, xyz_b, radius: float, limit: int):
    ax, ay, az = xyz_a
    bx, by, bz = xyz_b
    ids = conn.execute(
        text("""
            SELECT DISTINCT a.study_id
            FROM ns.coordinates AS a
            WHERE a.x BETWEEN :ax_min AND :ax_max
              AND a.y BETWEEN :ay_min AND :ay_max
              AND a.z BETWEEN :az_min AND :az_max
              AND (a.x - :ax) * (a.x - :ax)
                + (a.y - :ay) * (a.y - :ay)
                + (a.z - :az) * (a.z - :az) <= :radius_squared
              AND NOT EXISTS (
                  SELECT 1
                  FROM ns.coordinates AS b
                  WHERE b.study_id = a.study_id
                    AND b.x BETWEEN :bx_min AND :bx_max
                    AND b.y BETWEEN :by_min AND :by_max
                    AND b.z BETWEEN :bz_min AND :bz_max
                    AND (b.x - :bx) * (b.x - :bx)
                      + (b.y - :by) * (b.y - :by)
                      + (b.z - :bz) * (b.z - :bz) <= :radius_squared
              )
            ORDER BY a.study_id
            LIMIT :limit
        """),
        {
            "ax": ax, "ay": ay, "az": az,
            "bx": bx, "by": by, "bz": bz,
            "ax_min": ax - radius, "ax_max": ax + radius,
            "ay_min": ay - radius, "ay_max": ay + radius,
            "az_min": az - radius, "az_max": az + radius,
            "bx_min": bx - radius, "bx_max": bx + radius,
            "by_min": by - radius, "by_max": by + radius,
            "bz_min": bz - radius, "bz_max": bz + radius,
            "radius_squared": radius * radius, "limit": limit,
        },
    ).scalars().all()
    return metadata_for_studies(conn, list(ids), limit)


def create_app():
    app = Flask(__name__)

    @app.get("/")
    def health():
        return jsonify({"ok": True, "service": "Neurosynth Backend", "docs": "/docs"})

    @app.get("/openapi.json")
    def openapi_spec():
        return jsonify({
            "openapi": "3.0.3",
            "info": {
                "title": "Neurosynth Functional Dissociation API",
                "version": "1.0.0",
                "description": (
                    "Find studies that mention one term or MNI location but not another. "
                    "Dissociation endpoints return both A without B and B without A. "
                    "Location radius is Euclidean distance in the dataset's coordinate units."
                ),
            },
            "servers": [{"url": "/"}],
            "paths": {
                "/": {"get": {"summary": "Health check", "responses": {
                    "200": {"description": "Service is running."}
                }}},
                "/img": {"get": {"summary": "Sample amygdala image", "responses": {
                    "200": {"description": "GIF image."}
                }}},
                "/test_db": {"get": {"summary": "Check database connection", "responses": {
                    "200": {"description": "Database connection and table counts."},
                    "503": {"description": "Database is unavailable or not populated."}
                }}},
                "/terms/{term}/studies": {"get": {
                    "summary": "Find studies for a term",
                    "parameters": [
                        {"name": "term", "in": "path", "required": True,
                         "schema": {"type": "string"},
                         "description": "Term; spaces may be written as underscores."},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}
                    ],
                    "responses": {"200": {"description": "Matching study metadata."}, "400": {"description": "Invalid query parameter."}, "503": {"description": "Database query failed."}}
                }},
                "/locations/{coords}/studies": {"get": {
                    "summary": "Find studies near an MNI coordinate",
                    "parameters": [
                        {"name": "coords", "in": "path", "required": True,
                         "schema": {"type": "string", "example": "0_-52_26"},
                         "description": "Coordinates in x_y_z format."},
                        {"name": "radius", "in": "query", "schema": {"type": "number", "default": 6, "exclusiveMinimum": 0, "maximum": 30}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}
                    ],
                    "responses": {"200": {"description": "Matching study metadata."}, "400": {"description": "Invalid coordinates or query parameter."}, "503": {"description": "Database query failed."}}
                }},
                "/dissociate/terms/{term_a}/{term_b}": {"get": {
                    "summary": "Dissociate two terms",
                    "description": "Returns studies in A but not B, and studies in B but not A.",
                    "parameters": [
                        {"name": "term_a", "in": "path", "required": True, "schema": {"type": "string", "example": "posterior_cingulate"}},
                        {"name": "term_b", "in": "path", "required": True, "schema": {"type": "string", "example": "ventromedial_prefrontal"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}
                    ],
                    "responses": {"200": {"description": "A-only and B-only study groups."}, "400": {"description": "Invalid query parameter."}, "503": {"description": "Database query failed."}}
                }},
                "/dissociate/locations/{coords_a}/{coords_b}": {"get": {
                    "summary": "Dissociate two MNI locations",
                    "description": "Returns studies near A but not B, and near B but not A.",
                    "parameters": [
                        {"name": "coords_a", "in": "path", "required": True, "schema": {"type": "string", "example": "0_-52_26"}},
                        {"name": "coords_b", "in": "path", "required": True, "schema": {"type": "string", "example": "-2_50_-6"}},
                        {"name": "radius", "in": "query", "schema": {"type": "number", "default": 6, "exclusiveMinimum": 0, "maximum": 30}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000}}
                    ],
                    "responses": {"200": {"description": "A-only and B-only study groups."}, "400": {"description": "Invalid coordinates or query parameter."}, "503": {"description": "Database query failed."}}
                }}
            }
        })

    @app.get("/docs")
    def api_docs():
        return Response("""<!doctype html>
<html><head><meta charset="utf-8"><title>Neurosynth API docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'});</script>
</body></html>""", mimetype="text/html")

    @app.get("/img")
    def show_img():
        image_path = Path(__file__).with_name("amygdala.gif")
        return send_file(image_path, mimetype="image/gif")

    @app.get("/test_db")
    def test_db():
        try:
            with get_engine().begin() as conn:
                conn.execute(text("SET search_path TO ns, public"))
                payload = {
                    "ok": True,
                    "database_version": conn.exec_driver_sql("SELECT version()").scalar(),
                    "coordinates_count": conn.execute(text("SELECT COUNT(*) FROM ns.coordinates")).scalar(),
                    "metadata_count": conn.execute(text("SELECT COUNT(*) FROM ns.metadata")).scalar(),
                    "annotations_terms_count": conn.execute(text("SELECT COUNT(*) FROM ns.annotations_terms")).scalar(),
                }
            return jsonify(payload)
        except Exception as exc:
            app.logger.exception("Database health check failed")
            return jsonify({"ok": False, "error": "Database connection failed or tables are missing."}), 503

    @app.get("/terms/<term>/studies")
    def get_studies_by_term(term):
        try:
            limit = get_limit()
            with get_engine().begin() as conn:
                studies = studies_for_term(conn, term, limit)
            return jsonify({"term": normalize_term(term), "count": len(studies), "studies": studies})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Term query failed")
            return jsonify({"error": "Database query failed. Check the service logs and database setup."}), 503

    @app.get("/locations/<coords>/studies")
    def get_studies_by_coordinates(coords):
        try:
            xyz, radius, limit = parse_coords(coords), get_radius(), get_limit()
            with get_engine().begin() as conn:
                studies = studies_for_location(conn, xyz, radius, limit)
            return jsonify({"coordinates": xyz, "radius": radius, "count": len(studies), "studies": studies})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Coordinate query failed")
            return jsonify({"error": "Database query failed. Check the service logs and database setup."}), 503

    @app.get("/dissociate/terms/<term_a>/<term_b>")
    def dissociate_by_terms(term_a, term_b):
        try:
            limit = get_limit()
            a, b = normalize_term(term_a), normalize_term(term_b)
            with get_engine().begin() as conn:
                a_only = dissociate_terms(conn, a, b, limit)
                b_only = dissociate_terms(conn, b, a, limit)
            return jsonify({
                "type": "term_functional_dissociation",
                "A": a, "B": b,
                "A_without_B": {"count": len(a_only), "studies": a_only},
                "B_without_A": {"count": len(b_only), "studies": b_only},
                "limit_per_direction": limit,
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Term dissociation query failed")
            return jsonify({"error": "Database query failed. Check the service logs and database setup."}), 503

    @app.get("/dissociate/locations/<coords_a>/<coords_b>")
    def dissociate_by_locations(coords_a, coords_b):
        try:
            xyz_a, xyz_b = parse_coords(coords_a), parse_coords(coords_b)
            radius, limit = get_radius(), get_limit()
            with get_engine().begin() as conn:
                a_only = dissociate_locations(conn, xyz_a, xyz_b, radius, limit)
                b_only = dissociate_locations(conn, xyz_b, xyz_a, radius, limit)
            return jsonify({
                "type": "location_functional_dissociation",
                "A": xyz_a, "B": xyz_b, "radius": radius,
                "A_without_B": {"count": len(a_only), "studies": a_only},
                "B_without_A": {"count": len(b_only), "studies": b_only},
                "limit_per_direction": limit,
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Location dissociation query failed")
            return jsonify({"error": "Database query failed. Check the service logs and database setup."}), 503

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Endpoint not found"}), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
