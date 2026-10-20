# CRV ingestion worker. Build from src/ingestion:
#   docker buildx build --platform linux/amd64 --target runtime -t <acr>/crv-pipeline:<tag> .
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build
# Networks that block files.pythonhosted.org pass their approved mirror with
#   --build-arg PIP_INDEX_URL=https://<mirror>/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_INDEX_URL=${PIP_INDEX_URL}
WORKDIR /src
COPY pyproject.toml ./
COPY ingest_pipeline ./ingest_pipeline
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install ".[azure,parsers,tokenizer]"

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "ingest_pipeline.cli"]
