import importlib
import time
import traceback

from app.common import load_job, redis_client, save_job, settings


def get_handler(kind: str):
    module_name = "render_handler" if kind == "render" else "voice_handler"
    return importlib.import_module(module_name).handler


def run() -> None:
    config = settings()
    client = redis_client()
    handler = get_handler(config["kind"])

    while True:
        item = client.brpop(config["queue"], timeout=5)
        if not item:
            continue
        _, job_id = item
        job = load_job(client, job_id)
        if not job or job.get("status") != "IN_QUEUE":
            continue

        job.update({"status": "IN_PROGRESS", "workerId": config["worker_id"], "updatedAt": int(time.time())})
        save_job(client, job)
        try:
            output = handler(job["input"])
            job.update({"status": "COMPLETED", "output": output, "updatedAt": int(time.time())})
        except Exception as exc:
            job.update({"status": "FAILED", "error": str(exc), "details": traceback.format_exc(limit=20), "updatedAt": int(time.time())})
        save_job(client, job)


if __name__ == "__main__":
    run()

