# Aasaan: one small container (run by docker-compose.yml on a single VM).
# SQLite means ONE app instance only (do not scale this service horizontally).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend \
    AASAAN_DB=/app/data/aasaan.db \
    AASAAN_UPLOADS=/app/data/uploads

WORKDIR /app

# Create a dedicated non-root user and persistent storage directory
RUN useradd --create-home --uid 10001 aasaan \
    && mkdir -p /app/data/uploads \
    && chown -R aasaan:aasaan /app

# Copy dependency specifications first to leverage Docker layer caching
COPY --chown=aasaan:aasaan backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Optional offline OCR (Indra, tiers A and B of Intake): pinned package + models baked into the image, so nothing downloads at run time.
# Build without it:  docker build --build-arg WITH_INDRA=0 .   (Intake then uses the PDF text layer and the vision model only)
ARG WITH_INDRA=1
ENV INDRA_OCR_MODEL_DIR=/opt/indra-models
RUN if [ "$WITH_INDRA" = "1" ]; then pip install --no-cache-dir indra-ocr==0.3.2     && python -c "import os; from huggingface_hub import snapshot_download as d; from indra_ocr.models import HF_REPO, HF_REVISION; d(HF_REPO, revision=HF_REVISION, local_dir=os.environ['INDRA_OCR_MODEL_DIR'], allow_patterns=['*.onnx', 'meta.json', 'config.json'])"     && chmod -R a+rX "$INDRA_OCR_MODEL_DIR"; fi

# Copy application source code and configurations
COPY --chown=aasaan:aasaan backend backend
COPY --chown=aasaan:aasaan frontend frontend
COPY --chown=aasaan:aasaan config config

# Run as non-root user
USER aasaan

# SQLite database and user uploads volume
VOLUME ["/app/data"]

EXPOSE 8000

# Health check against the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Start server bound to 0.0.0.0 on port 8000 using the FastAPI factory entry point
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
