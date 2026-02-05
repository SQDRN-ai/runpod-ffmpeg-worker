import os
import subprocess
import uuid
import runpod
import boto3
import shlex

def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

def download_from_r2(key: str, local_path: str):
    bucket = os.environ["R2_BUCKET"]
    s3 = r2_client()
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    s3.download_file(bucket, key, local_path)
    return {"bucket": bucket, "key": key, "local_path": local_path}

def upload_to_r2(local_path: str, key: str):
    bucket = os.environ["R2_BUCKET"]
    s3 = r2_client()
    s3.upload_file(local_path, bucket, key)
    public_base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public_base}/{key}" if public_base else None
    return {"bucket": bucket, "key": key, "url": url}

def ensure_key(s: str) -> str:
    """
    Accepts either:
      - "posts/jobId/file.ext"  (recommended)
      - "r2://posts/jobId/file.ext"
    Returns the pure key.
    """
    if not s:
        return s
    return s.replace("r2://", "", 1) if s.startswith("r2://") else s

def run_ffmpeg(cmd: list[str]):
    # capture output but don’t explode memory
    p = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "returncode": p.returncode,
        "stdout": p.stdout[-20000:],
        "stderr": p.stderr[-20000:],
    }

def handler(event):
    inp = event.get("input", {})

    # --- MODE A: keep your old "prompt" mode (works as before) ---
    if "prompt" in inp and inp.get("prompt"):
        prompt = inp["prompt"]
        p = subprocess.run(prompt, shell=True, capture_output=True, text=True)

        upload_path = inp.get("upload_path")
        uploaded = None
        if upload_path and os.path.exists(upload_path):
            key = inp.get("r2_key") or f"outputs/{uuid.uuid4().hex}-{os.path.basename(upload_path)}"
            uploaded = upload_to_r2(upload_path, key)

        return {
            "mode": "prompt",
            "returncode": p.returncode,
            "stdout": p.stdout[-20000:],
            "stderr": p.stderr[-20000:],
            "uploaded": uploaded,
        }

    # --- MODE B: new "render" mode ---
    # Required inputs:
    #   video_key: "posts/<jobId>/input.mp4"
    #   ass_key:   "posts/<jobId>/subtitles.ass"
    #   out_key:   "posts/<jobId>/rendered_h265.mp4"
    mode = inp.get("mode", "render")
    if mode != "render":
        return {"error": f"Unknown mode: {mode}"}

    video_key = ensure_key(inp.get("video_key"))
    ass_key   = ensure_key(inp.get("ass_key"))
    out_key   = ensure_key(inp.get("out_key"))

    if not video_key or not ass_key or not out_key:
        return {"error": "Missing video_key, ass_key, or out_key"}

    # Download inputs
    download_from_r2(video_key, "/tmp/in.mp4")
    download_from_r2(ass_key, "/tmp/subs.ass")

    # FFmpeg render settings (defaults for your use-case)
    width  = int(inp.get("width", 3840))
    height = int(inp.get("height", 2160))
    crf    = int(inp.get("crf", 28))          # quality
    preset = inp.get("preset", "medium")      # speed vs quality

    # IMPORTANT: escaping the subtitles filter correctly
    # libass reads .ass and burns it in.
    subtitles_filter = f"subtitles=/tmp/subs.ass"

    cmd = [
    "ffmpeg", "-y",
    "-i", "/tmp/in.mp4",
    "-vf", subtitles_filter,
    "-c:v", "libx264",
    "-profile:v", "high",
    "-level", "4.2",
    "-preset", preset,      # medium is fine
    "-crf", str(crf),       # recommend crf=20
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    "-s", f"{width}x{height}",
    "-an",
    "/tmp/out.mp4"
]

    ff = run_ffmpeg(cmd)
    if ff["returncode"] != 0:
        return {"error": "ffmpeg failed", **ff}

    uploaded = upload_to_r2("/tmp/out.mp4", out_key)
    return {
        "mode": "render",
        **ff,
        "uploaded": uploaded,
        "inputs": {"video_key": video_key, "ass_key": ass_key, "out_key": out_key}
    }

runpod.serverless.start({"handler": handler})
