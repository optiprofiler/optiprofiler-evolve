FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg

ARG OPTIPROFILER_VERSION=1.3.4
ARG PYYAML_VERSION=6.0.3

RUN pip install --no-cache-dir \
    "optiprofiler==${OPTIPROFILER_VERSION}" "PyYAML==${PYYAML_VERSION}"

COPY pyproject.toml README.md LICENSE /tmp/optiprofiler-evolve/
COPY src /tmp/optiprofiler-evolve/src
RUN pip install --no-cache-dir --no-deps /tmp/optiprofiler-evolve \
    && rm -rf /tmp/optiprofiler-evolve

WORKDIR /output
