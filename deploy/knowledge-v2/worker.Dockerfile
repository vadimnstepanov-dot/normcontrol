# Foundation image; resolve and pin the digest during deployment acceptance (stage 9).
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/sto_rag
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-draw python3-uno poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng fonts-dejavu-core && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pypdf==6.10.0 pdfplumber==0.11.7 Pillow==11.3.0 opencv-python-headless==4.12.0.88 numpy==2.2.6 lxml==6.1.3 \
    && useradd --uid 65532 --create-home app && mkdir -p /data && chown app:app /data
COPY sto_rag/knowledge_v2 /app/sto_rag/knowledge_v2
COPY sto_rag/pipeline.py sto_rag/token_cache.py sto_rag/word_source.py /app/sto_rag/
USER app
ENV NORMCONTROL_KNOWLEDGE_DATA=/data
STOPSIGNAL SIGTERM
CMD ["python", "-m", "knowledge_v2.bridge"]
