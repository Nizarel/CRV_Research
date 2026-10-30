# CRV PostgreSQL 16 staging server for the DGX Spark (linux/arm64; also builds on amd64): the
# official postgres:16 Debian image plus Apache AGE and pgvector, built from pinned,
# checksum-verified source releases at the versions the Azure source server runs.
#   docker build -t crv-postgres16:age1.6.0-pgvector0.8.2 infra/dgx/postgres16
ARG POSTGRES_IMAGE=postgres:16-bookworm@sha256:537d0998e20878d435e5f4bd2f672dc5400a2f832ee3b9c0625f71437d2bde43

FROM ${POSTGRES_IMAGE} AS build
ARG AGE_VERSION=1.6.0
ARG AGE_SHA512=1e0a434d53d669e5d067721e1eba8c3c3c2ae072709d5f90783beeba6f5e88f592fa5d8808d621b720c22edfe8c698baa9b0adb9c7b4e3e2a83e7a753bd795c8
ARG PGVECTOR_VERSION=0.8.2
ARG PGVECTOR_SHA256=69f4019389af05dc1c9548deb8628e62878e6e207c03907f2b8af2016472cdaa
# The server headers must match the server binaries exactly (PG_MAJOR and PG_VERSION come from
# the base image).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends bison build-essential ca-certificates curl flex \
        "postgresql-server-dev-${PG_MAJOR}=${PG_VERSION}"; \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
# with_llvm=no: no JIT bitcode (the server runs with jit=off, as on Azure).
RUN set -eux; \
    curl -fsSL --retry 3 -o age.tar.gz \
        "https://archive.apache.org/dist/age/PG${PG_MAJOR}/${AGE_VERSION}/apache-age-${AGE_VERSION}-src.tar.gz"; \
    echo "${AGE_SHA512}  age.tar.gz" | sha512sum -c -; \
    mkdir age; \
    tar -xzf age.tar.gz -C age --strip-components=1; \
    make -C age with_llvm=no; \
    make -C age with_llvm=no install DESTDIR=/out
# OPTFLAGS="": portable code instead of -march=native.
RUN set -eux; \
    curl -fsSL --retry 3 -o pgvector.tar.gz \
        "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz"; \
    echo "${PGVECTOR_SHA256}  pgvector.tar.gz" | sha256sum -c -; \
    mkdir pgvector; \
    tar -xzf pgvector.tar.gz -C pgvector --strip-components=1; \
    make -C pgvector with_llvm=no OPTFLAGS=""; \
    make -C pgvector with_llvm=no OPTFLAGS="" install DESTDIR=/out

FROM ${POSTGRES_IMAGE}
ARG AGE_VERSION=1.6.0
ARG PGVECTOR_VERSION=0.8.2
COPY --from=build /out/ /
LABEL org.opencontainers.image.title="crv-postgres16" \
      org.opencontainers.image.description="PostgreSQL 16 with Apache AGE ${AGE_VERSION} and pgvector ${PGVECTOR_VERSION} for the CRV DGX staging restore"
