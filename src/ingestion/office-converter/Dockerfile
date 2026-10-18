FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress fonts-dejavu-core python3 \
    && rm -rf /var/lib/apt/lists/*
COPY convert.py /opt/convert.py
ENTRYPOINT ["python3", "/opt/convert.py"]
