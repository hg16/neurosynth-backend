# Neurosynth Backend

A Flask API for functional dissociation queries over the supplied Neurosynth dataset and a PostgreSQL database. The dissociation routes return studies in **A without B** and **B without A**.

Coordinate distance is calculated in the dataset's x/y/z units. It is a 3D Euclidean radius, not a distance in kilometres.

## API

- `GET /` — health check and docs link
- `GET /docs` — interactive Swagger UI
- `GET /openapi.json` — OpenAPI specification
- `GET /img` — sample amygdala image
- `GET /test_db` — database connection and imported table counts
- `GET /terms/<term>/studies` — studies containing a term
- `GET /locations/<x_y_z>/studies` — studies with a coordinate within a radius
- `GET /dissociate/terms/<term_a>/<term_b>` — A-only and B-only studies
- `GET /dissociate/locations/<x1_y1_z1>/<x2_y2_z2>` — A-only and B-only studies

The `limit` parameter defaults to 100 and accepts 1–1000 results per direction. Location routes accept `radius` (default 6, maximum 30). Terms can use underscores in place of spaces.

Examples:

```text
/dissociate/terms/posterior_cingulate/ventromedial_prefrontal
/dissociate/terms/ventromedial_prefrontal/posterior_cingulate
/dissociate/locations/0_-52_26/-2_50_-6
/dissociate/locations/-2_50_-6/0_-52_26
```

Each dissociation response includes `A_without_B` and `B_without_A`, each with a count and metadata records for the studies.

## Run locally

1. Install Python 3.10 or newer and PostgreSQL 12 or newer.
2. Install the importer packages with `pip install -r requirements-import.txt`. Render installs only the smaller API runtime dependencies from `requirements.txt`.
3. Set `DB_URL` to a PostgreSQL connection URL. For local development, the URL can point at a local database; for Render, use the Internal Database URL in the web service's environment settings.
4. Import the bundled dataset once:

   ```bash
   python create_db.py --url "$DB_URL" --data-dir . --schema ns --if-exists replace
   ```

5. Start the API:

   ```bash
   gunicorn app:app --bind 0.0.0.0:${PORT:-5000}
   ```

If a connection needs TLS, add `?sslmode=require` to `DB_URL` or set `DB_SSLMODE=require` for the web service.

## Deploy with Render and GitHub

The included `render.yaml` defines a Singapore web service. The database is already provisioned, so keep its credentials outside the repository.

1. Upload the project contents, including the three `.parquet` files and `.github/workflows/import-database.yml`, to a GitHub repository. A private repository is recommended because it contains course data.
2. In GitHub, add an Actions repository secret named `DB_URL` with the Render **External Database URL**.
3. Open **Actions → Import Neurosynth data to Render PostgreSQL → Run workflow**. Enter `IMPORT` in the confirmation field. The import replaces tables in the `ns` schema.
4. Create a Render Blueprint from the repository. Set the web service's `DB_URL` to the Render **Internal Database URL**.
5. Open the service root, then `/test_db` and `/docs`.

The free database plan may have storage or lifetime limits. Check the current database usage in Render before starting the import; the compressed source files do not show the final PostgreSQL storage size.

Never commit a database URL or password. `check_db.py` can report the PostgreSQL version and installed extension status without changing the database:

```bash
python check_db.py --url "$DB_URL"
```

## Files

- `app.py` — Flask API
- `create_db.py` — Parquet-to-PostgreSQL importer
- `requirements.txt` — API runtime dependencies
- `requirements-import.txt` — additional dependencies for importing Parquet files
- `check_db.py` — read-only PostgreSQL and extension status report
- `preflight_db.py` — GitHub Actions connectivity and dataset preflight
- `coordinates.parquet`, `metadata.parquet`, `annotations.parquet` — supplied dataset
