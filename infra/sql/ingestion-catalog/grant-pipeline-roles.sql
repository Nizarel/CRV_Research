-- Map the pipeline workload identities to PostgreSQL and grant the runtime role.
-- Run by an Entra administrator AFTER infra/ingestion-pipeline.bicep and migration 0003.
-- Automated (dry run by default) by grant-pipeline-roles.py via invoke-evidence-smoke.ps1.
--
-- Step 1, connected to the `postgres` database (principal names = identity names):
--   SELECT * FROM pgaadauth_create_principal('id-crv-pipeline-coordinator', false, false);
--   SELECT * FROM pgaadauth_create_principal('id-crv-pipeline-worker', false, false);
--
-- Step 2, connected to the `ingestion-catalog` database (this file):

GRANT CONNECT ON DATABASE "ingestion-catalog" TO "id-crv-pipeline-coordinator", "id-crv-pipeline-worker";
GRANT tizert_ingest_worker TO "id-crv-pipeline-coordinator", "id-crv-pipeline-worker";
