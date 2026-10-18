# Build from a reviewed Debian-based Python 3.10+ image pinned by digest.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY deploy/query-service/requirements.txt /app/deploy/query-service/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/deploy/query-service/requirements.txt
COPY agent_forge_service /app/agent_forge_service
COPY tools /app/tools
COPY *.schema.json /app/
COPY deploy/query-service/entrypoint.sh /app/deploy/query-service/entrypoint.sh
USER 10001:10001
EXPOSE 8791
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD python -c "from urllib.request import build_opener,ProxyHandler; build_opener(ProxyHandler({})).open('http://127.0.0.1:8791/health/ready',timeout=4).close()"
ENTRYPOINT ["/bin/sh", "/app/deploy/query-service/entrypoint.sh"]
