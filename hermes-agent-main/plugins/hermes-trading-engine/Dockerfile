# Hermes Trading Engine - BTC 5-min pulse paper engine (standalone container)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTE_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Test-only deps (pytest). Installed on top of the runtime requirements so the
# suite runs inside the container with no manual `pip install`. Does not change
# runtime behavior — nothing in engine/ imports these.
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r /app/requirements-dev.txt

COPY engine /app/engine
COPY scripts /app/scripts
# Ship the test suite (+ conftest.py) at /app/tests so `python -m pytest` works in-container.
COPY pytest.ini /app/pytest.ini
COPY tests /app/tests

# CI / regression gate: the image build FAILS if the test suite is red, so a broken suite can
# never ship to the VPS. Override with --build-arg RUN_TESTS=0 ONLY for an emergency build.
ARG RUN_TESTS=1
RUN if [ "$RUN_TESTS" = "1" ]; then python -m pytest -q; \
    else echo "WARNING: skipping test gate (RUN_TESTS=0)"; fi

RUN mkdir -p /data
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/api/health').status==200 else 1)"

CMD ["uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8800"]
