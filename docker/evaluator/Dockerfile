FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg

ARG OPTIPROFILER_VERSION=1.3.4
ARG PYYAML_VERSION=6.0.3
ARG PRIMA_GIT_REF=1d76fb88aeffb427cd17ed1e9d0d3b34f414913f

RUN pip install --no-cache-dir \
    "optiprofiler==${OPTIPROFILER_VERSION}" "PyYAML==${PYYAML_VERSION}"

# The independent PRIMA package is optional at runtime but required by the
# built-in prima_newuoa challenger. Pin the tested upstream commit so a run can
# be reproduced after PRIMA changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cmake g++ gcc gfortran git ninja-build \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /tmp/prima \
    && cd /tmp/prima \
    && git init \
    && git remote add origin https://github.com/libprima/prima.git \
    && git fetch --depth 1 origin "${PRIMA_GIT_REF}" \
    && git checkout --detach FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && pip install --no-cache-dir \
        "scikit-build-core==0.9.10" setuptools-scm pybind11 \
    && pip install --no-cache-dir --no-build-isolation /tmp/prima \
    && rm -rf /tmp/prima

COPY pyproject.toml README.md LICENSE /tmp/optiprofiler-evolve/
COPY src /tmp/optiprofiler-evolve/src
RUN pip install --no-cache-dir --no-deps /tmp/optiprofiler-evolve \
    && rm -rf /tmp/optiprofiler-evolve

WORKDIR /output
