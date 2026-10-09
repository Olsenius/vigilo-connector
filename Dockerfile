# Remote MCP-gateway for vigilo-connector. Se docs/remote-gateway.md og README.
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM ${PYTHON_IMAGE}
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vigilo \
    && mkdir -p /data && chown vigilo:vigilo /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

ENV DATA_DIR=/data \
    VIGILO_CONFIG_DIR=/data/vigilo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER vigilo
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"]
CMD ["vigilo-gateway"]
