# Deployment checklist

1. Upload the project files and all three Parquet datasets to a GitHub repository. A private repository is recommended for course data.
2. Add GitHub Actions secret `DB_URL` containing the Render **External Database URL**.
3. Run **Actions → Import Neurosynth data to Render PostgreSQL** and enter `IMPORT` to confirm replacing the tables in schema `ns`.
4. Create a Render Blueprint from the repository and set the web service's `DB_URL` to the Render **Internal Database URL**.
5. Open `/`, `/test_db`, and `/docs` on the deployed service.

The supplied Render database is PostgreSQL 18 in Singapore on the Free plan. PostGIS, `pg_trgm`, and `unaccent` were unconfirmed in the database page; this backend does not require them. The location API stores and compares the source x/y/z values directly.

Do not commit either database URL or its password. Check available storage in Render before importing; the final PostgreSQL size can exceed the compressed Parquet file sizes.
