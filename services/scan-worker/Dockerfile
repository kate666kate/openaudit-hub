FROM python:3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends chromium nodejs npm dumb-init \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/lighthouse-report/package.json /app/package.json
RUN npm install --omit=dev
COPY services/portal/database.py /app/database.py
COPY services/scan-worker/tasks.py /app/tasks.py
COPY services/scan-worker/crawler.py /app/crawler.py
COPY services/scan-worker/content_quality.py /app/content_quality.py
COPY services/scan-worker/pa11y-runner.js /app/pa11y-runner.js
COPY services/scan-worker/capture-evidence.js /app/capture-evidence.js
COPY services/scan-worker/visual_evidence.py /app/visual_evidence.py
COPY services/scan-worker/sampling.py /app/sampling.py
COPY services/scan-worker/budgets.py /app/budgets.py
RUN pip install --no-cache-dir celery==5.4.0 redis==5.0.8 SQLAlchemy==2.0.36 "psycopg[binary]==3.2.3" requests==2.32.3 beautifulsoup4==4.12.3 lxml==5.3.0

ENV CHROME_PATH=/usr/bin/chromium
CMD ["dumb-init", "celery", "-A", "tasks.celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
