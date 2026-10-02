# Build the foundation from deploy/knowledge-v2/worker.Dockerfile first.
ARG CORE_BASE=normcontrol/review-worker:1.0.0
FROM ${CORE_BASE}
USER root
RUN pip install --no-cache-dir python-docx==1.2.0 lxml==6.1.3 pymorphy3==2.0.6 pymorphy3-dicts-ru==2.4.417150.4580142 psutil==7.1.0
COPY sto_rag /app/sto_rag
ENV PYTHONPATH=/app/sto_rag PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NORMCONTROL_NATIVE_DATA=/data/nc5 NORMCONTROL_NATIVE_ROOT=/app NORMCONTROL_KNOWLEDGE_DATA=/data/nc5/knowledge
USER 65532
STOPSIGNAL SIGTERM
CMD ["python","-m","native_core.runtime","worker"]
