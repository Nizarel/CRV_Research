FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress fonts-dejavu-core python3 \
    && rm -rf /var/lib/apt/lists/*
COPY convert.py /opt/convert.py
USER 10001:10001
ENTRYPOINT ["python3", "/opt/convert.py"]
