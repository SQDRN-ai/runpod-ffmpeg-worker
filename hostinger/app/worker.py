import importlib
import os
import shutil
import time
import traceback

from redis.exceptions import LockError

from app.common import load_job, redis_client, save_job, settings


def get_handler(kind: str):
    module_name = "render_handler" if kind == "render" else "voice_handler"
    return importlib.import_module(module_name).handler


def cleanup_tmp() -> None:
    """Remove per-job media while keeping the model cache mounted at /models."""
    try:
        entries = list(os.scandir("/tmp"))
    except FileNotFoundError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except FileNotFoundError:
            pass


def recover_interrupted_jobs(client, config: dict) -> None:
    """Return jobs left in the processing list after a container restart."""
    while True:
        job_id = client.rpoplpush(config["processing_queue"], config["queue"])
        if not job_id:
            return
        job = load_job(client, job_id)
        if job and job.get("status") == "IN_PROGRESS":
            job.update({"status": "IN_QUEUE", "updatedAt": int(time.time())})
            save_job(client, job)


def run() -> None:
    config = settings()
    client = redis_client()
    handler = get_handler(config["kind"])
    recover_interrupted_jobs(client, config)

    while True:
        job_id = client.brpoplpush(config["queue"], config["processing_queue"], timeout=5)
        if not job_id:
            continue
        job = load_job(client, job_id)
        if not job or job.get("status") != "IN_QUEUE":
            client.lrem(config["processing_queue"], 1, job_id)
            continue

        heavy_lock = client.lock(config["heavy_lock"], timeout=config["heavy_lock_ttl"])
        if not heavy_lock.acquire(blocking=False):
            client.lrem(config["processing_queue"], 1, job_id)
            client.rpush(config["queue"], job_id)
            time.sleep(2)
            continue

        try:
            job.update({"status": "IN_PROGRESS", "workerId": config["worker_id"], "updatedAt": int(time.time())})
            save_job(client, job)
            cleanup_tmp()
            output = handler(job["input"])
            job.update({"status": "COMPLETED", "output": output, "updatedAt": int(time.time())})
        except Exception as exc:
            job.update({"status": "FAILED", "error": str(exc), "details": traceback.format_exc(limit=20), "updatedAt": int(time.time())})
        finally:
            cleanup_tmp()
            save_job(client, job)
            client.lrem(config["processing_queue"], 1, job_id)
            try:
                heavy_lock.release()
            except LockError:
                pass


if __name__ == "__main__":
    run()
