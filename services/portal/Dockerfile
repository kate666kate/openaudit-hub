FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py
COPY database.py /app/database.py
COPY scan_queue.py /app/scan_queue.py
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY templates /app/templates
COPY static /app/static
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080
CMD ["/app/docker-entrypoint.sh"]
