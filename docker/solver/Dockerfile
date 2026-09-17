FROM python:3.12-slim-bookworm

# This image contains numerical dependencies, never benchmark/provider code.
# Users can extend it for their algorithm's dependencies without changing the
# trusted evaluator image or exposing its filesystem.
RUN pip install --no-cache-dir numpy==1.26.4 scipy==1.17.1
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /candidate

# Record which source a campaign image was built from; "unknown" for ad-hoc local builds.
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/optiprofiler/optiprofiler-evolve" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
