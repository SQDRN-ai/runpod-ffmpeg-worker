import os
import subprocess
import uuid
import runpod
import boto3
from typing import Any, Dict, Tuple

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_MUSIC = "/tmp/music.mp3"
TMP_NAME_ASS = "/tmp/name_overlay.ass"
TMP_OUT = "/tmp/final.mp4"


# -----------------------------
# R2 helpers
# -----------------------------
def r2_client():
    # Requires: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
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
# Filter/ASS helpers
# -----------------------------
def _ass_color_from_hex_rgba(hex_rgba: str) -> str:
    """
    Convert "#RRGGBB" or "#RRGGBBAA" into ASS "&HAABBGGRR&"
    """
    s = hex_rgba.strip()
    if not s.startswith("#") or len(s) not in (7, 9):
        raise ValueError("hex_rgba must be '#RRGGBB' or '#RRGGBBAA'")

    rr = s[1:3]
    gg = s[3:5]
    bb = s[5:7]
    aa = s[7:9] if len(s) == 9 else "00"  # 00 = fully opaque in ASS alpha
    return f"&H{aa}{bb}{gg}{rr}&"  # AABBGGRR


def _normalize_ass_color(v: Any, fallback: str) -> str:
    """
    Accepts:
      - ASS colors like "&H00FFFFFF&"
      - hex like "#RRGGBB" or "#RRGGBBAA"
    """
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


def _build_force_style(force_style: Dict[str, Any]) -> str:
    """
    Build libass force_style string "Key=Value,Key=Value"
    """
    parts = []
    for k, v in force_style.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return ",".join(parts)


def _escape_for_subtitles_filter(value: str) -> str:
    """
    force_style is wrapped in single quotes; escape backslashes and single quotes.
    """
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


def _make_name_overlay_ass(cfg: Dict[str, Any], play_w: int, play_h: int) -> str:
    """
    Generates an ASS file to overlay a big animated name across the whole video.
    Supports animation: "sparkle_glow" (default).
    """
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("name_overlay.text is required")

    font = cfg.get("font", "Montserrat ExtraBold")
    size = int(cfg.get("size", 300))
    outline = float(cfg.get("outline", 20))
    shadow = float(cfg.get("shadow", 0))
    spacing = float(cfg.get("spacing", 3))
    alignment = int(cfg.get("alignment", 5))  # 5 = center-middle
    margin_v = int(cfg.get("margin_v", 0))
    margin_l = int(cfg.get("margin_l", 0))
    margin_r = int(cfg.get("margin_r", 0))

    primary = _normalize_ass_color(cfg.get("color"), "&H00FFFFFF&")
    secondary = _normalize_ass_color(cfg.get("secondary_color"), "&H0033CCFF&")
    outline_col = _normalize_ass_color(cfg.get("outline_color"), "&H00000000&")
    back_col = _normalize_ass_color(cfg.get("back_color"), "&H00000000&")

    x = cfg.get("x")
    y = cfg.get("y")
    pos_tag = rf"\pos({int(x)},{int(y)})" if x is not None and y is not None else ""

    rotate = float(cfg.get("rotate_deg", 0))
    rot_tag = rf"\frz{rotate}" if rotate else ""

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
            rf"\fad({fade_in_ms},{fade_out_ms})"
            rf"\blur2\be1"
            rf"\fscx100\fscy100"
            rf"\t(0,600,\blur7\be2\fscx104\fscy104)"
            rf"\t(600,1200,\blur2\be1\fscx100\fscy100)"
            rf"\t(1200,1800,\blur7\be2\fscx104\fscy104)"
            rf"\t(1800,2400,\blur2\be1\fscx100\fscy100)"
            rf"\t(2400,3000,\blur7\be2\fscx104\fscy104)"
            rf"\t(3000,3600,\blur2\be1\fscx100\fscy100)"
        )

        if shimmer_outline and shimmer_outline != outline_col:
            pulse += rf"\t(0,600,\3c{shimmer_outline})\t(600,1200,\3c{outline_col})"
            pulse += rf"\t(1200,1800,\3c{shimmer_outline})\t(1800,2400,\3c{outline_col})"
            pulse += rf"\t(2400,3000,\3c{shimmer_outline})\t(3000,3600,\3c{outline_col})"

        tags = rf"{{\an{alignment}{pos_tag}{rot_tag}{pulse}}}"
    else:
        tags = rf"{{\an{alignment}{pos_tag}{rot_tag}\fad({fade_in_ms},{fade_out_ms})}}}"

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


def _get_canvas(render: Dict[str, Any]) -> Tuple[int, int]:
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
    inp = event.get("input", {}) or {}
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
            "got": {
                "video_key": bool(video_key),
                "ass_key": bool(ass_key),
                "music_key": bool(music_key),
            },
        }

    render = inp.get("render", {}) or {}
    subs_cfg = render.get("subtitles", {}) or {}
    name_cfg = render.get("name_overlay", None)
    timing_cfg = render.get("timing", {}) or {}

    # Canvas (ASS PlayRes) — default 4K for predictable sizing
    try:
        play_w, play_h = _get_canvas(render)
    except Exception as e:
        return {"error": "Invalid render.canvas", "details": str(e)}

    # Timing controls
    loop_video = bool(timing_cfg.get("loop_video", False))

    # Video/audio encoding knobs (optional)
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

    # 2) Build video filtergraph
    filters = []

    # Optional scale first (only if you want to force output resolution)
    if v_scale:
        filters.append(f"scale={v_scale}")

    # Burn karaoke subtitles, optionally overriding style
    force_style = subs_cfg.get("force_style", None)
    subs_filter = f"subtitles={TMP_ASS}"
    if isinstance(force_style, dict) and force_style:
        fs = _build_force_style(force_style)
        fs_esc = _escape_for_subtitles_filter(fs)
        subs_filter += f":force_style='{fs_esc}'"
    filters.append(subs_filter)

    # Name overlay as a SECOND ASS pass (4K-aware PlayRes)
    name_overlay_used = False
    if isinstance(name_cfg, dict) and str(name_cfg.get("text", "")).strip():
        try:
            ass_text = _make_name_overlay_ass(name_cfg, play_w, play_h)
            with open(TMP_NAME_ASS, "w", encoding="utf-8") as f:
                f.write(ass_text)
            filters.append(f"subtitles={TMP_NAME_ASS}")
            name_overlay_used = True
        except Exception as e:
            return {"error": "Failed to generate name overlay ASS", "details": str(e)}

    vf = ",".join(filters)

    # 3) Build audio filter (optional)
    af = None
    if a_volume is not None:
        try:
            af = f"volume={float(a_volume)}"
        except Exception:
            return {"error": "audio.volume must be a number (e.g. 1.08)"}

    # 4) ffmpeg command
    cmd = ["ffmpeg", "-y"]

    # Loop the VIDEO input if requested, so audio determines final duration
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

    # 5) Upload output
    uploaded = upload_to_r2(TMP_OUT, out_key)

    return {
        "status": "ok",
        "jobId": job_id,
        "video_key": video_key,
        "ass_key": ass_key,
        "music_key": music_key,
        "out_key": out_key,
        "uploaded": uploaded,
        "name_overlay_used": name_overlay_used,
        "ffmpeg_cmd": cmd,
        "ffmpeg_stderr_tail": p.stderr[-20000:],
        "canvas": {"width": play_w, "height": play_h},
        "timing": {"loop_video": loop_video},
    }


runpod.serverless.start({"handler": handler})
