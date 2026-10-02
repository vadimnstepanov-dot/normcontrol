# Optional isolated portal for foundation testing; not a replacement of the live VPS.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY normcontrol-web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt 'whitenoise==6.12.0' && useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
COPY normcontrol-web/manage.py /app/manage.py
COPY normcontrol-web/portal /app/portal
COPY normcontrol-web/knowledge /app/knowledge
COPY sto_rag/knowledge_v2 /app/knowledge_v2
COPY sto_rag/pipeline.py sto_rag/token_cache.py sto_rag/word_source.py /app/
COPY normcontrol-web/templates /app/templates
COPY normcontrol-web/static /app/static
RUN APP_DEBUG=1 APP_DATA=/data python manage.py collectstatic --noinput
USER app
ENV APP_DATA=/data NORMCONTROL_KNOWLEDGE_V2=1 APP_STATIC_SELF_SERVE=1
STOPSIGNAL SIGTERM
CMD ["gunicorn", "portal.wsgi:application", "--bind", "0.0.0.0:8110", "--workers", "1", "--timeout", "60"]
