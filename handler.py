import os
import subprocess
import uuid
import math
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
    """
    Convert "#RRGGBB" or "#RRGGBBAA" into ASS "&HAABBGGRR&"
    """
    s = hex_rgba.strip()
    if not s.startswith("#") or len(s) not in (7, 9):
        raise ValueError("hex_rgba must be '#RRGGBB' or '#RRGGBBAA'")
    rr = s[1:3]
    gg = s[3:5]
    bb = s[5:7]
    aa = s[7:9] if len(s) == 9 else "00"
    return f"&H{aa}{bb}{gg}{rr}&"  # AABBGGRR


def _normalize_ass_color(v, fallback: str) -> str:
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


def _format_ass_time(ms: int) -> str:
    """
    ASS time format: H:MM:SS.cs where cs = centiseconds (1/100s)
    """
    if ms < 0:
        ms = 0
    cs = int(round(ms / 10.0))
    s = cs // 100
    cs_rem = cs % 100
    m = s // 60
    s_rem = s % 60
    h = m // 60
    m_rem = m % 60
    return f"{h}:{m_rem:02d}:{s_rem:02d}.{cs_rem:02d}"


def _make_style_line(
    name: str,
    font: str,
    size: int,
    primary: str,
    secondary: str,
    outline_col: str,
    back_col: str,
    spacing: float,
    outline: float,
    shadow: float,
    alignment: int,
    margin_l: int,
    margin_r: int,
    margin_v: int,
) -> str:
    return (
        "Style: {nm}, {font}, {size}, {pri}, {sec}, {olc}, {bac}, "
        "1,0,0,0, 100,100, {sp}, 0, 1, {ol}, {sh}, {an}, {ml}, {mr}, {mv}, 1\n"
    ).format(
        nm=name,
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


def _sparkle_glow_tags(
    fade_in_ms: int,
    fade_out_ms: int,
    outline_col: str,
    shimmer_outline: str | None,
    loop_ms: int,
    period_ms: int = 1200,
) -> str:
    """
    Build long-running sparkle tags by repeating pulses until loop_ms.
    """
    # Base: fade + a soft glow baseline
    tags = f"\\fad({fade_in_ms},{fade_out_ms})\\blur2\\be1\\fscx100\\fscy100"
    t = 0
    while t + period_ms <= loop_ms:
        # up then back
        tags += f"\\t({t},{t+period_ms//2},\\blur7\\be2\\fscx104\\fscy104)"
        tags += f"\\t({t+period_ms//2},{t+period_ms},\\blur2\\be1\\fscx100\\fscy100)"
        if shimmer_outline and shimmer_outline != outline_col:
            # IMPORTANT: emit "\3c" literally as "\\\\3c" in python string
            tags += f"\\t({t},{t+period_ms//2},\\\\3c{shimmer_outline})\\t({t+period_ms//2},{t+period_ms},\\\\3c{outline_col})"
        t += period_ms
    return tags


def _make_wave_letter_dialogues(
    text: str,
    style_name: str,
    play_w: int,
    play_h: int,
    size: int,
    wave_cfg: dict,
    layer: int = 10,
) -> str:
    """
    Create per-letter Dialogue lines with staggered pulse timing to simulate a wave.
    This is a font-width approximation (good enough for bold display names).
    """
    # Layout config (4K defaults)
    center_x = int(wave_cfg.get("center_x", play_w // 2))
    center_y = int(wave_cfg.get("center_y", play_h // 2))
    letter_spacing = float(wave_cfg.get("letter_spacing", 18))
    width_factor = float(wave_cfg.get("approx_char_width_factor", 0.62))

    amplitude = float(wave_cfg.get("amplitude_px", 40))
    scale_peak = float(wave_cfg.get("scale_peak", 112))  # percent

    step_ms = int(wave_cfg.get("step_ms", 120))          # delay between letters
    pulse_ms = int(wave_cfg.get("pulse_ms", 900))        # duration of each letter pulse
    loop_ms = int(wave_cfg.get("loop_ms", 240000))       # how long to keep waving

    # Approx char advance
    adv = size * width_factor + letter_spacing

    # Compute total width (approx) including spaces (treat space as 0.5 adv)
    advances = []
    for ch in text:
        if ch == " ":
            advances.append(adv * 0.5)
        else:
            advances.append(adv)
    total_w = sum(advances)

    start_x = center_x - total_w / 2.0

    # We will build repeated pulses per letter (offset by i*step_ms)
    # Use \pos for stable placement, and \t segments to simulate bob + scale.
    # Vertical bob: y +/- amplitude.
    dialogues = []
    running_x = start_x

    for i, ch in enumerate(text):
        # Skip empty?
        if ch == "":
            continue

        # Position for this glyph
        x = int(round(running_x + advances[i] / 2.0))
        y0 = int(round(center_y))
        y_up = int(round(center_y - amplitude))
        y_dn = int(round(center_y))

        # For spaces, we don't render, just advance
        if ch == " ":
            running_x += advances[i]
            continue

        # Build tags
        # We use \an5 center, \pos(x,y), then repeated \t segments.
        # For bobbing: we fake it by changing \pos with \t using \move is clunky,
        # so instead we animate \fscy + \fscx + a slight baseline shift with \t and \yshad? (not reliable).
        # The most reliable "movement" is to use \move with a short window.
        #
        # We'll do: move from y0 -> y_up -> y0 each pulse window.
        #
        # \move(x1,y1,x2,y2,t1,t2) runs over a time interval.
        # We chain two moves per pulse: up then down.
        #
        base = f"\\an5\\pos({x},{y0})"

        # pulses
        t0 = i * step_ms
        t = t0
        pulse_tags = ""
        while t + pulse_ms <= loop_ms:
            half = pulse_ms // 2
            # Move up first half, down second half
            pulse_tags += f"\\move({x},{y0},{x},{y_up},{t},{t+half})"
            pulse_tags += f"\\move({x},{y_up},{x},{y_dn},{t+half},{t+pulse_ms})"
            # Scale up then back
            pulse_tags += f"\\t({t},{t+half},\\fscx{scale_peak:.0f}\\fscy{scale_peak:.0f})"
            pulse_tags += f"\\t({t+half},{t+pulse_ms},\\fscx100\\fscy100)"
            t += pulse_ms

        tags = f"{{{base}{pulse_tags}}}"

        # Long event (whole duration); the time-based tags do the animation
        start = "0:00:00.00"
        end = "9:59:59.00"
        dialogues.append(
            f"Dialogue: {layer},{start},{end},{style_name},,0000,0000,0000,,{tags}{ch}\n"
        )

        running_x += advances[i]

    return "".join(dialogues)


def _make_name_overlay_ass(cfg: dict, play_w: int, play_h: int) -> str:
    """
    Generates an ASS file to overlay a big animated name.
    Supports:
      - sparkle_glow (single line, repeated pulses over loop_ms)
      - wave_letters (per-letter wave animation)
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

    fade_in_ms = int(cfg.get("fade_in_ms", 500))
    fade_out_ms = int(cfg.get("fade_out_ms", 900))
    rotate = float(cfg.get("rotate_deg", 0))  # keep straight by default
    rot_tag = f"\\frz{rotate}" if rotate else ""

    anim = str(cfg.get("animation", "sparkle_glow")).strip().lower()
    loop_ms = int(cfg.get("loop_ms", 240000))

    header = _ensure_ass_header(play_w, play_h)

    # One style for the name
    style_line = _make_style_line(
        name="NAME",
        font=font,
        size=size,
        primary=primary,
        secondary=secondary,
        outline_col=outline_col,
        back_col=back_col,
        spacing=spacing,
        outline=outline,
        shadow=shadow,
        alignment=alignment,
        margin_l=margin_l,
        margin_r=margin_r,
        margin_v=margin_v,
    )

    styles = style_line + "\n"

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    if anim == "wave_letters":
        wave_cfg = cfg.get("wave", {}) or {}

        # Optional position override for wave
        # If user provided x/y, interpret as center for the wave block
        x = cfg.get("x")
        y = cfg.get("y")
        if x is not None and y is not None:
            wave_cfg = dict(wave_cfg)
            wave_cfg["center_x"] = int(x)
            wave_cfg["center_y"] = int(y)

        dialogues = _make_wave_letter_dialogues(
            text=text,
            style_name="NAME",
            play_w=play_w,
            play_h=play_h,
            size=size,
            wave_cfg=wave_cfg,
            layer=10,
        )
        ass = header + styles + events_header + dialogues
        return ass

    # Default: sparkle_glow on a single Dialogue line
    shimmer_outline = cfg.get("shimmer_outline_color")
    shimmer_outline = _normalize_ass_color(shimmer_outline, outline_col) if shimmer_outline else None

    sparkle_tags = _sparkle_glow_tags(
        fade_in_ms=fade_in_ms,
        fade_out_ms=fade_out_ms,
        outline_col=outline_col,
        shimmer_outline=shimmer_outline,
        loop_ms=loop_ms,
        period_ms=int(cfg.get("sparkle_period_ms", 1200)),
    )

    # Optional explicit position (otherwise rely on alignment)
    x = cfg.get("x")
    y = cfg.get("y")
    pos_tag = f"\\pos({int(x)},{int(y)})" if x is not None and y is not None else ""

    tags = f"{{\\an{alignment}{pos_tag}{rot_tag}{sparkle_tags}}}"

    dialogue = (
        f"Dialogue: 10,0:00:00.00,9:59:59.00,NAME,,0000,0000,0000,,{tags}{text}\n"
    )

    ass = header + styles + events_header + dialogue
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

        # Download inputs
        download_from_r2(video_key, TMP_IN)
        download_from_r2(ass_key, TMP_ASS)
        download_from_r2(music_key, TMP_MUSIC)

        # Build video filtergraph
        filters = []
        if v_scale:
            filters.append(f"scale={v_scale}")

        # Burn karaoke subtitles + optional force_style
        force_style = subs_cfg.get("force_style", None)
        subs_filter = f"subtitles={TMP_ASS}"
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"
        filters.append(subs_filter)

        # Name overlay as second subtitles pass
        name_overlay_used = False
        if isinstance(name_cfg, dict) and str(name_cfg.get("text", "")).strip():
            ass_text = _make_name_overlay_ass(name_cfg, play_w, play_h)
            with open(TMP_NAME_ASS, "w", encoding="utf-8") as f:
                f.write(ass_text)
            filters.append(f"subtitles={TMP_NAME_ASS}")
            name_overlay_used = True

        vf = ",".join(filters)

        # Audio filter
        af = None
        if a_volume is not None:
            af = f"volume={float(a_volume)}"

        # ffmpeg command
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
