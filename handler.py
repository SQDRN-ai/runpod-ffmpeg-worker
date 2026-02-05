import os
import subprocess
import uuid
import runpod
import boto3

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_MUSIC = "/tmp/music.mp3"
TMP_OUT = "/tmp/final.mp4"

def r2_client():
    # You must set R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
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
    s3.download_file(bucket, key, local_path)

def upload_to_r2(local_path: str, key: str):
    bucket = os.environ["R2_BUCKET"]
    s3 = r2_client()
    s3.upload_file(local_path, bucket, key)

    public_base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public_base}/{key}" if public_base else None
    return {"bucket": bucket, "key": key, "url": url}

def handler(event):
    inp = event.get("input", {})
    mode = inp.get("mode", "render")

    if mode != "render":
        return {"error": f"Unknown mode: {mode}. Use mode='render'."}

    job_id = inp.get("jobId")
    video_key = inp.get("video_key")
    ass_key = inp.get("ass_key")
    music_key = inp.get("music_key")
    out_key = inp.get("out_key") or (f"posts/{job_id}/final.mp4" if job_id else f"outputs/{uuid.uuid4().hex}.mp4")

    if not video_key or not ass_key or not music_key:
        return {
            "error": "Missing required inputs.",
            "required": ["video_key", "ass_key", "music_key"],
            "got": {"video_key": bool(video_key), "ass_key": bool(ass_key), "music_key": bool(music_key)}
        }

    # 1) Download inputs
    download_from_r2(video_key, TMP_IN)
    download_from_r2(ass_key, TMP_ASS)
    download_from_r2(music_key, TMP_MUSIC)

    # 2) Render: burn subtitles + REPLACE audio with mp3
    cmd = [
        "ffmpeg", "-y",
        "-i", TMP_IN,
        "-i", TMP_MUSIC,
        "-vf", f"subtitles={TMP_ASS}",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        TMP_OUT
    ]

    p = subprocess.run(cmd, capture_output=True, text=True)

    if p.returncode != 0:
        return {
            "error": "ffmpeg failed",
            "returncode": p.returncode,
            "stderr": p.stderr[-20000:],
            "stdout": p.stdout[-20000:],
        }

    # 3) Upload output
    uploaded = upload_to_r2(TMP_OUT, out_key)

    return {
        "status": "ok",
        "jobId": job_id,
        "video_key": video_key,
        "ass_key": ass_key,
        "music_key": music_key,
        "out_key": out_key,
        "uploaded": uploaded,
        "ffmpeg_stderr_tail": p.stderr[-20000:],  # useful for debugging
    }

runpod.serverless.start({"handler": handler})
