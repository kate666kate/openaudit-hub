from __future__ import annotations

import os


def enqueue_scan(job_id: str) -> str:
    try:
        from celery import Celery
    except ImportError as exc:  # pragma: no cover - installed in the Docker image.
        raise RuntimeError("Celery is not installed. Rebuild the portal container.") from exc

    broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    backend = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
    client = Celery("openaudit-client", broker=broker, backend=backend)
    result = client.send_task("openaudit.run_lighthouse", args=[job_id])
    return str(result.id)
