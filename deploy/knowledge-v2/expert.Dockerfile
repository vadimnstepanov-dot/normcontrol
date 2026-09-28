FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KNOWLEDGE_EXPERT_ONLY=1
WORKDIR /app
COPY sto_rag/knowledge_v2 /app/knowledge_v2
# Same canonical-volume owner as the existing knowledge/index workers.
RUN useradd --uid 65532 --create-home app
USER app
STOPSIGNAL SIGTERM
CMD ["python", "-m", "knowledge_v2.bridge"]
