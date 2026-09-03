import json
import os
import socket
import time
import uuid

import redis


def settings() -> dict:
    kind = os.environ.get("WORKER_KIND", "render").strip().lower()
    if kind not in {"render", "theme", "voice"}:
        raise RuntimeError("WORKER_KIND must be 'render', 'theme' or 'voice'")
    return {
        "kind": kind,
        "queue": f"birthday:{kind}:queue",
        "processing_queue": f"birthday:{kind}:processing",
        "key_prefix": f"birthday:{kind}:job:",
        "ttl": int(os.environ.get("JOB_TTL_SECONDS", "604800")),
        "heavy_lock": os.environ.get("HEAVY_LOCK_KEY", "birthday:heavy:lock"),
        "heavy_lock_ttl": int(os.environ.get("HEAVY_LOCK_TTL_SECONDS", "43200")),
        "post_job_delay": float(os.environ.get("WORKER_POST_JOB_DELAY_SECONDS", "0")),
        "worker_id": f"{socket.gethostname()}-{os.getpid()}",
    }


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)


def job_key(job_id: str) -> str:
    return f"{settings()['key_prefix']}{job_id}"


def now() -> int:
    return int(time.time())


def load_job(client: redis.Redis, job_id: str) -> dict | None:
    raw = client.get(job_key(job_id))
    return json.loads(raw) if raw else None


def save_job(client: redis.Redis, job: dict) -> None:
    client.set(job_key(job["id"]), json.dumps(job, separators=(",", ":")), ex=settings()["ttl"])


def new_job(payload: dict) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": "IN_QUEUE",
        "input": payload,
        "createdAt": now(),
        "updatedAt": now(),
    }
