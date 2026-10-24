# PostgreSQL 16 + Apache AGE 1.6.0 + pgvector for the pipeline integration tests
# (migration 0022 needs pgvector, as Azure Database for PostgreSQL and the DGX Spark provide).
FROM apache/age:release_PG16_1.6.0@sha256:16aa423d20a31aed36a3313244bf7aa00731325862f20ed584510e381f2feaed
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
