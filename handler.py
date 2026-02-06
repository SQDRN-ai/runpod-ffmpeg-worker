import os
import subprocess
import uuid
import runpod

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_MUSIC = "/tmp/music.mp3"
TMP_NAME_ASS = "/tmp/name_overlay.ass"
TMP_FONTS_ZIP = "/tmp/fonts.zip"
TMP_OUT = "/tmp/final.mp4"

print("✅ handler.py loaded (startup ok)")


# -----------------------------
# R2 helpers (lazy-import boto3)
# -----------------------------
def r2_client():
    try:
        import boto3
    except Exception as e:
        raise RuntimeError(f"boto3 import failed (is it installed in the image?): {e}")

    required = ["R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")

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


# -----------------------------
# Utilities
# -----------------------------
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def unzip_to_dir(zip_path: str, out_dir: str):
    """
    Uses system unzip (most images have it). If not present, returns a clear error.
    """
    ensure_dir(out_dir)
    # -o overwrite, -q quiet
    p = subprocess.run(["unzip", "-o", "-q", zip_path, "-d", out_dir], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"unzip failed: {p.stderr[-2000:] or p.stdout[-2000:]}")


def _build_force_style(force_style: dict) -> str:
    parts = []
    for k, v in (force_style or {}).items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return ",".join(parts)


def _escape_for_subtitles_filter(value: str) -> str:
    # force_style is wrapped in single quotes
    return value.replace("\\", "\\\\").replace("'", r"\'")


# -----------------------------
# Name overlay ASS generator (unchanged from your working version)
# (Keep your current wave_letters/sparkle_glow code here.)
# For brevity, this example assumes you already have:
#   _make_name_overlay_ass(cfg, play_w, play_h)
#   _get_canvas(render)
# from your current working handler.
# -----------------------------

# ---- BEGIN: minimal name overlay implementation (sparkle_glow only) ----
# If you already have wave_letters + sparkle_glow in your handler, keep that code instead.
def _ensure_ass_header(play_w: int, play_h: int) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )


def _make_name_overlay_ass(cfg: dict, play_w: int, play_h: int) -> str:
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("name_overlay.text is required")

    # Simple static name overlay (your existing wave_letters code can stay instead)
    font = cfg.get("font", "Montserrat ExtraBold")
    size = int(cfg.get("size", 300))
    alignment = int(cfg.get("alignment", 5))

    style = (
        "Style: NAME, {font}, {size}, &H00FFFFFF&, &H00FFFFFF&, &H00000000&, &H00000000&, "
        "1,0,0,0, 100,100, 0, 0, 1, 20, 0, {an}, 0, 0, 0, 1\n"
    ).format(font=font, size=size, an=alignment)

    events = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 10,0:00:00.00,9:59:59.00,NAME,,0000,0000,0000,,{{\\an{alignment}}}{text}\n"
    )

    return _ensure_ass_header(play_w, play_h) + style + "\n" + events


def _get_canvas(render: dict):
    canvas = render.get("canvas", {}) or {}
    w = int(canvas.get("width", 3840))
    h = int(canvas.get("height", 2160))
    return w, h
# ---- END: minimal name overlay implementation ----


# -----------------------------
# Main handler
# -----------------------------
def handler(event):
    try:
        inp = (event or {}).get("input", {}) or {}
        mode = inp.get("mode", "render")
        if mode != "render":
            return {"error": f"Unknown mode: {mode}. Use mode='render'."}

        job_id = inp.get("jobId")
        video_key = inp.get("video_key")
        ass_key = inp.get("ass_key")
        music_key = inp.get("music_key")
        out_key = inp.get("out_key") or (
            f"posts/{job_id}/final.mp4" if job_id else f"outputs/{uuid.uuid4().hex}.mp4"
        )

        if not video_key or not ass_key or not music_key:
            return {"error": "Missing required inputs.", "required": ["video_key", "ass_key", "music_key"]}

        render = inp.get("render", {}) or {}
        subs_cfg = render.get("subtitles", {}) or {}
        name_cfg = render.get("name_overlay", None)
        timing_cfg = render.get("timing", {}) or {}
        fonts_cfg = render.get("fonts", {}) or {}

        play_w, play_h = _get_canvas(render)
        loop_video = bool(timing_cfg.get("loop_video", False))

        video_cfg = render.get("video", {}) or {}
        audio_cfg = render.get("audio", {}) or {}

        v_codec = video_cfg.get("codec", "libx264")
        v_preset = video_cfg.get("preset", "medium")
        v_crf = str(video_cfg.get("crf", 18))
        v_pix_fmt = video_cfg.get("pix_fmt", "yuv420p")
        v_profile = video_cfg.get("profile", "high")
        v_tune = video_cfg.get("tune", None)
        v_scale = video_cfg.get("scale", None)
        faststart = bool(video_cfg.get("movflags_faststart", True))

        a_codec = audio_cfg.get("codec", "aac")
        a_bitrate = audio_cfg.get("bitrate", "192k")
        a_volume = audio_cfg.get("volume", None)

        # 1) Download inputs
        download_from_r2(video_key, TMP_IN)
        download_from_r2(ass_key, TMP_ASS)
        download_from_r2(music_key, TMP_MUSIC)

        # 2) Optional fonts.zip -> /tmp/fonts
        fontsdir = None
        zip_key = fonts_cfg.get("zip_key")
        local_dir = fonts_cfg.get("local_dir", "/tmp/fonts")
        if zip_key:
            download_from_r2(zip_key, TMP_FONTS_ZIP)
            unzip_to_dir(TMP_FONTS_ZIP, local_dir)
            fontsdir = local_dir

        # 3) Build video filtergraph
        filters = []
        if v_scale:
            filters.append(f"scale={v_scale}")

        # Karaoke subtitles
        force_style = subs_cfg.get("force_style", None)
        subs_filter = f"subtitles={TMP_ASS}"
        if fontsdir:
            subs_filter += f":fontsdir={fontsdir}"
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"
        filters.append(subs_filter)

        # Name overlay subtitles (generated ASS)
        name_overlay_used = False
        if isinstance(name_cfg, dict) and str(name_cfg.get("text", "")).strip():
            ass_text = _make_name_overlay_ass(name_cfg, play_w, play_h)
            with open(TMP_NAME_ASS, "w", encoding="utf-8") as f:
                f.write(ass_text)

            name_filter = f"subtitles={TMP_NAME_ASS}"
            if fontsdir:
                name_filter += f":fontsdir={fontsdir}"
            filters.append(name_filter)
            name_overlay_used = True

        vf = ",".join(filters)

        # 4) Audio filter
        af = None
        if a_volume is not None:
            af = f"volume={float(a_volume)}"

        # 5) ffmpeg command
        cmd = ["ffmpeg", "-y"]
        if loop_video:
            cmd += ["-stream_loop", "-1", "-i", TMP_IN]
        else:
            cmd += ["-i", TMP_IN]
        cmd += ["-i", TMP_MUSIC]

        cmd += [
            "-vf", vf,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", str(v_codec),
            "-preset", str(v_preset),
            "-crf", str(v_crf),
            "-pix_fmt", str(v_pix_fmt),
            "-c:a", str(a_codec),
            "-b:a", str(a_bitrate),
            "-shortest",
        ]

        if v_profile:
            cmd += ["-profile:v", str(v_profile)]
        if v_tune:
            cmd += ["-tune", str(v_tune)]
        if faststart:
            cmd += ["-movflags", "+faststart"]
        if af:
            cmd += ["-af", af]

        cmd.append(TMP_OUT)

        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return {
                "error": "ffmpeg failed",
                "returncode": p.returncode,
                "stderr": p.stderr[-20000:],
                "stdout": p.stdout[-20000:],
                "cmd": cmd,
            }

        uploaded = upload_to_r2(TMP_OUT, out_key)

        return {
            "status": "ok",
            "jobId": job_id,
            "out_key": out_key,
            "uploaded": uploaded,
            "fontsdir_used": fontsdir,
            "name_overlay_used": name_overlay_used,
            "ffmpeg_cmd": cmd,
            "ffmpeg_stderr_tail": p.stderr[-20000:],
        }

    except Exception as e:
        return {"error": "handler exception", "details": str(e)}


runpod.serverless.start({"handler": handler})
