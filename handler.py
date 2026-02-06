import os
import subprocess
import uuid
import runpod

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_MUSIC = "/tmp/music.mp3"
TMP_NAME_ASS = "/tmp/name_overlay.ass"
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
# ASS helpers
# -----------------------------
def _ass_color_from_hex_rgba(hex_rgba: str) -> str:
    s = hex_rgba.strip()
    if not s.startswith("#") or len(s) not in (7, 9):
        raise ValueError("hex_rgba must be '#RRGGBB' or '#RRGGBBAA'")
    rr = s[1:3]
    gg = s[3:5]
    bb = s[5:7]
    aa = s[7:9] if len(s) == 9 else "00"
    return f"&H{aa}{bb}{gg}{rr}&"  # AABBGGRR


def _normalize_ass_color(v, fallback: str) -> str:
    if v is None:
        return fallback
    s = str(v).strip()
    if not s:
        return fallback
    if s.startswith("&H") and s.endswith("&"):
        return s
    if s.startswith("#"):
        return _ass_color_from_hex_rgba(s)
    return s


def _build_force_style(force_style: dict) -> str:
    parts = []
    for k, v in (force_style or {}).items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return ",".join(parts)


def _escape_for_subtitles_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", r"\'")


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

    font = cfg.get("font", "Montserrat ExtraBold")
    size = int(cfg.get("size", 300))
    outline = float(cfg.get("outline", 20))
    shadow = float(cfg.get("shadow", 0))
    spacing = float(cfg.get("spacing", 3))
    alignment = int(cfg.get("alignment", 5))

    margin_v = int(cfg.get("margin_v", 0))
    margin_l = int(cfg.get("margin_l", 0))
    margin_r = int(cfg.get("margin_r", 0))

    primary = _normalize_ass_color(cfg.get("color"), "&H00FFFFFF&")
    secondary = _normalize_ass_color(cfg.get("secondary_color"), "&H0033CCFF&")
    outline_col = _normalize_ass_color(cfg.get("outline_color"), "&H00000000&")
    back_col = _normalize_ass_color(cfg.get("back_color"), "&H00000000&")

    x = cfg.get("x")
    y = cfg.get("y")
    pos_tag = f"\\pos({int(x)},{int(y)})" if x is not None and y is not None else ""

    rotate = float(cfg.get("rotate_deg", 0))
    rot_tag = f"\\frz{rotate}" if rotate else ""

    fade_in_ms = int(cfg.get("fade_in_ms", 500))
    fade_out_ms = int(cfg.get("fade_out_ms", 900))
    anim = str(cfg.get("animation", "sparkle_glow")).strip().lower()

    style_line = (
        "Style: NAME, {font}, {size}, {pri}, {sec}, {olc}, {bac}, "
        "1,0,0,0, 100,100, {sp}, 0, 1, {ol}, {sh}, {an}, {ml}, {mr}, {mv}, 1\n"
    ).format(
        font=font,
        size=size,
        pri=primary,
        sec=secondary,
        olc=outline_col,
        bac=back_col,
        sp=spacing,
        ol=outline,
        sh=shadow,
        an=alignment,
        ml=margin_l,
        mr=margin_r,
        mv=margin_v,
    )

    if anim == "sparkle_glow":
        shimmer_outline = cfg.get("shimmer_outline_color")
        shimmer_outline = _normalize_ass_color(shimmer_outline, outline_col) if shimmer_outline else None

        pulse = (
            f"\\fad({fade_in_ms},{fade_out_ms})"
            "\\blur2\\be1"
            "\\fscx100\\fscy100"
            "\\t(0,600,\\blur7\\be2\\fscx104\\fscy104)"
            "\\t(600,1200,\\blur2\\be1\\fscx100\\fscy100)"
            "\\t(1200,1800,\\blur7\\be2\\fscx104\\fscy104)"
            "\\t(1800,2400,\\blur2\\be1\\fscx100\\fscy100)"
            "\\t(2400,3000,\\blur7\\be2\\fscx104\\fscy104)"
            "\\t(3000,3600,\\blur2\\be1\\fscx100\\fscy100)"
        )

        # IMPORTANT: use "\\\\3c" to literally emit "\3c" in the ASS file
        if shimmer_outline and shimmer_outline != outline_col:
            pulse += f"\\t(0,600,\\\\3c{shimmer_outline})\\t(600,1200,\\\\3c{outline_col})"
            pulse += f"\\t(1200,1800,\\\\3c{shimmer_outline})\\t(1800,2400,\\\\3c{outline_col})"
            pulse += f"\\t(2400,3000,\\\\3c{shimmer_outline})\\t(3000,3600,\\\\3c{outline_col})"

        tags = f"{{\\an{alignment}{pos_tag}{rot_tag}{pulse}}}"
    else:
        # ✅ fixed: no extra brace at the end
        tags = f"{{\\an{alignment}{pos_tag}{rot_tag}\\fad({fade_in_ms},{fade_out_ms})}}"

    dialogue = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 10,0:00:00.00,9:59:59.00,NAME,,0000,0000,0000,,{tags}{text}\n"
    )

    ass = _ensure_ass_header(play_w, play_h)
    ass += style_line
    ass += "\n"
    ass += dialogue
    return ass


def _get_canvas(render: dict):
    canvas = render.get("canvas", {}) or {}
    w = int(canvas.get("width", 3840))
    h = int(canvas.get("height", 2160))
    if w <= 0 or h <= 0:
        raise ValueError("render.canvas.width/height must be positive integers")
    return w, h


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
            return {
                "error": "Missing required inputs.",
                "required": ["video_key", "ass_key", "music_key"],
                "got": {"video_key": bool(video_key), "ass_key": bool(ass_key), "music_key": bool(music_key)},
            }

        render = inp.get("render", {}) or {}
        subs_cfg = render.get("subtitles", {}) or {}
        name_cfg = render.get("name_overlay", None)
        timing_cfg = render.get("timing", {}) or {}

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

        download_from_r2(video_key, TMP_IN)
        download_from_r2(ass_key, TMP_ASS)
        download_from_r2(music_key, TMP_MUSIC)

        filters = []
        if v_scale:
            filters.append(f"scale={v_scale}")

        force_style = subs_cfg.get("force_style", None)
        subs_filter = f"subtitles={TMP_ASS}"
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"
        filters.append(subs_filter)

        name_overlay_used = False
        if isinstance(name_cfg, dict) and str(name_cfg.get("text", "")).strip():
            ass_text = _make_name_overlay_ass(name_cfg, play_w, play_h)
            with open(TMP_NAME_ASS, "w", encoding="utf-8") as f:
                f.write(ass_text)
            filters.append(f"subtitles={TMP_NAME_ASS}")
            name_overlay_used = True

        vf = ",".join(filters)

        af = None
        if a_volume is not None:
            af = f"volume={float(a_volume)}"

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
            "name_overlay_used": name_overlay_used,
            "ffmpeg_cmd": cmd,
            "ffmpeg_stderr_tail": p.stderr[-20000:],
            "canvas": {"width": play_w, "height": play_h},
            "timing": {"loop_video": loop_video},
        }

    except Exception as e:
        return {"error": "handler exception", "details": str(e)}


runpod.serverless.start({"handler": handler})
