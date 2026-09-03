import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.common import load_job, new_job, redis_client, save_job, settings

app = FastAPI(title="Birthday worker API", docs_url=None, redoc_url=None)


def require_token(authorization: str | None) -> None:
    expected = os.environ.get("WORKER_API_TOKEN", "")
    if not expected:
        raise RuntimeError("WORKER_API_TOKEN is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> dict:
    try:
        redis_client().ping()
    except Exception as exc:  # exercised by container health check
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})
    return {"ok": True, "worker": settings()["kind"]}


@app.post("/v1/jobs")
async def create_job(request: Request, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("input"), dict):
        raise HTTPException(status_code=400, detail="Body must match { input: { ... } }")

    client = redis_client()
    job = new_job(payload)
    save_job(client, job)
    client.lpush(settings()["queue"], job["id"])
    return {"id": job["id"], "status": job["status"]}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    job = load_job(redis_client(), job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")

    response = {"id": job["id"], "status": job["status"]}
    if job.get("workerId"):
        response["workerId"] = job["workerId"]
    if "output" in job:
        response["output"] = job["output"]
    if "error" in job:
        response["error"] = job["error"]
    return response

