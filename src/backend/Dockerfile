FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
# Optional package index (for example the organisation's PyPI proxy); TLS is always verified.
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libpq5 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml /app/
COPY crv_agent /app/crv_agent
RUN PIP_INDEX_URL="$PIP_INDEX_URL" pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 crv
USER 10001
EXPOSE 8000
CMD ["crv-agent", "serve", "--host", "0.0.0.0"]
