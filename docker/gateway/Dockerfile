FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --no-create-home --uid 10001 gateway

COPY src/optiprofiler_evolve/provider_gateway.py /opt/optiprofiler-evolve/provider_gateway.py
RUN chmod 0555 /opt/optiprofiler-evolve/provider_gateway.py

USER gateway
ENTRYPOINT ["python", "/opt/optiprofiler-evolve/provider_gateway.py"]
