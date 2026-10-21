FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libpq5 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml /app/
COPY crv_agent /app/crv_agent
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 crv
USER 10001
EXPOSE 8000
CMD ["crv-agent", "serve", "--host", "0.0.0.0"]
