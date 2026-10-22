# Indra OCR spike image. Pinned package; the models are baked into the image so nothing downloads at run time.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 INDRA_OCR_MODEL_DIR=/opt/indra-models
RUN pip install --no-cache-dir indra-ocr==0.3.2
# the build fills INDRA_OCR_MODEL_DIR (see prefetch.py); run time needs no network
COPY prefetch.py /opt/prefetch.py
RUN python /opt/prefetch.py
WORKDIR /work
COPY run_indra.py /work/run_indra.py
ENTRYPOINT ["python", "/work/run_indra.py"]
