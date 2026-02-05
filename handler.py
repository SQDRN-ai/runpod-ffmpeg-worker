import os
import subprocess
import uuid
import runpod
import boto3

def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

def upload_to_r2(local_path: str, key: str):
    bucket = os.environ["R2_BUCKET"]
    s3 = r2_client()
    s3.upload_file(local_path, bucket, key)
    public_base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public_base}/{key}" if public_base else None
    return {"bucket": bucket, "key": key, "url": url}

def handler(event):
    inp = event.get("input", {})
    prompt = inp.get("prompt", "")
    if not prompt:
        return {"error": "Missing input.prompt"}

    p = subprocess.run(prompt, shell=True, capture_output=True, text=True)

    upload_path = inp.get("upload_path")
    uploaded = None
    if upload_path and os.path.exists(upload_path):
        key = inp.get("r2_key") or f"outputs/{uuid.uuid4().hex}-{os.path.basename(upload_path)}"
        uploaded = upload_to_r2(upload_path, key)

    return {
        "returncode": p.returncode,
        "stdout": p.stdout[-20000:],
        "stderr": p.stderr[-20000:],
        "uploaded": uploaded,
    }

runpod.serverless.start({"handler": handler})
