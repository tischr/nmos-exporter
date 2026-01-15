FROM python:3.11-alpine

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY is12client.py .
COPY exporter.py .

RUN adduser -D -u 1000 exporter && \
    chown -R exporter:exporter /app

USER exporter

EXPOSE 9080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:9080/health || exit 1

CMD ["python", "exporter.py"]