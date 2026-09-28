FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
WORKDIR /app
RUN pip install --no-cache-dir 'torch==2.14.0+cpu' --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir 'sentence-transformers==6.1.0'
COPY knowledge_v2 /app/knowledge_v2
USER 65532:65532
ENTRYPOINT ["python","-m","knowledge_v2.embedding_service"]
