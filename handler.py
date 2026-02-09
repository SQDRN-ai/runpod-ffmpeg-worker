import os
import subprocess
import uuid
import zipfile
import runpod

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_MUSIC = "/tmp/music.mp3"
TMP_NAME_ASS = "/tmp/name_overlay.ass"
TMP_HB_ASS = "/tmp/happy_birthday_overlay.ass"
TMP_AFTER_ASS = "/tmp/after_subtitles_overlay.ass"
TMP_BEFORE_ASS = "/tmp/before_subtitles_overlay.ass"  # ✅ NEW
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
    ensure_dir(out_dir)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(out_dir)


def find_fontsdir(root_dir: str) -> str:
    try:
        for name in os.listdir(root_dir):
            low = name.lower()
            if low.endswith(".ttf") or low.endswith(".otf"):
                return root_dir
    except FileNotFoundError:
        return root_dir

    try:
        for name in os.listdir(root_dir):
            sub = os.path.join(root_dir, name)
            if os.path.isdir(sub):
                for fn in os.listdir(sub):
                    low = fn.lower()
                    if low.endswith(".ttf") or low.endswith(".otf"):
                        return sub
    except Exception:
        pass

    return root_dir


def _build_force_style(force_style: dict) -> str:
    parts = []
    for k, v in (force_style or {}).items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return ",".join(parts)


def _escape_for_subtitles_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", r"\'")


# -----------------------------
# Duration helpers
# -----------------------------
def ass_time_to_seconds(t: str) -> float:
    h, m, rest = t.split(":")
    s, cs = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def get_ass_end_seconds(path: str) -> float:
    max_end = 0.0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 3)
            if len(parts) < 3:
                continue
            end_t = parts[2].strip()
            try:
                end_s = ass_time_to_seconds(end_t)
                if end_s > max_end:
                    max_end = end_s
            except Exception:
                pass
    return max_end


def get_ass_start_seconds(path: str) -> float:  # ✅ NEW
    min_start = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            # Dialogue: Layer, Start, End, ...
            parts = line.split(",", 3)
            if len(parts) < 3:
                continue
            start_t = parts[1].strip()
            try:
                start_s = ass_time_to_seconds(start_t)
                if min_start is None or start_s < min_start:
                    min_start = start_s
            except Exception:
                pass
    return float(min_start) if min_start is not None else 0.0


def get_media_duration_seconds(path: str) -> float:
    p = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            path
        ],
        capture_output=True,
        text=True
    )
    try:
        return float(p.stdout.strip())
    except Exception:
        return 0.0


def seconds_to_ass_time(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    cs = int(round(sec * 100))
    h = cs // 360000
    m = (cs % 360000) // 6000
    s = (cs % 6000) // 100
    c = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


# -----------------------------
# Name overlay (wave letters)
# -----------------------------
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


def _make_wave_letter_dialogues(
    text: str,
    style_name: str,
    play_w: int,
    play_h: int,
    size: int,
    wave_cfg: dict,
    layer: int = 10,
) -> str:
    center_x = int(wave_cfg.get("center_x", play_w // 2))
    center_y = int(wave_cfg.get("center_y", play_h // 2))
    letter_spacing = float(wave_cfg.get("letter_spacing", 18))
    width_factor = float(wave_cfg.get("approx_char_width_factor", 0.62))

    amplitude = float(wave_cfg.get("amplitude_px", 40))
    scale_peak = float(wave_cfg.get("scale_peak", 112))  # percent

    step_ms = int(wave_cfg.get("step_ms", 120))
    pulse_ms = int(wave_cfg.get("pulse_ms", 900))
    loop_ms = int(wave_cfg.get("loop_ms", 240000))

    adv = size * width_factor + letter_spacing

    advances = []
    for ch in text:
        advances.append(adv * 0.5 if ch == " " else adv)

    total_w = sum(advances)
    start_x = center_x - total_w / 2.0

    dialogues = []
    running_x = start_x

    for i, ch in enumerate(text):
        x = int(round(running_x + advances[i] / 2.0))
        y0 = int(round(center_y))
        y_up = int(round(center_y - amplitude))
        y_dn = int(round(center_y))

        if ch == " ":
            running_x += advances[i]
            continue

        base = f"\\an5\\pos({x},{y0})"

        t0 = i * step_ms
        t = t0
        pulse_tags = ""
        while t + pulse_ms <= loop_ms:
            half = pulse_ms // 2
            pulse_tags += f"\\move({x},{y0},{x},{y_up},{t},{t+half})"
            pulse_tags += f"\\move({x},{y_up},{x},{y_dn},{t+half},{t+pulse_ms})"
            pulse_tags += f"\\t({t},{t+half},\\fscx{scale_peak:.0f}\\fscy{scale_peak:.0f})"
            pulse_tags += f"\\t({t+half},{t+pulse_ms},\\fscx100\\fscy100)"
            t += pulse_ms

        tags = f"{{{base}{pulse_tags}}}"
        dialogues.append(
            f"Dialogue: {layer},0:00:00.00,9:59:59.00,{style_name},,0000,0000,0000,,{tags}{ch}\n"
        )

        running_x += advances[i]

    return "".join(dialogues)


def _make_name_overlay_ass(cfg: dict, play_w: int, play_h: int) -> str:
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("name_overlay.text is required")

    anim = str(cfg.get("animation", "wave_letters")).strip().lower()

    font = cfg.get("font", "Montserrat ExtraBold")
    size = int(cfg.get("size", 300))
    outline = float(cfg.get("outline", 20))
    shadow = float(cfg.get("shadow", 0))
    spacing = float(cfg.get("spacing", 3))
    alignment = int(cfg.get("alignment", 5))

    margin_v = int(cfg.get("margin_v", 0))
    margin_l = int(cfg.get("margin_l", 0))
    margin_r = int(cfg.get("margin_r", 0))

    primary = str(cfg.get("color", "&H00FFFFFF&"))
    secondary = str(cfg.get("secondary_color", "&H0033CCFF&"))
    outline_col = str(cfg.get("outline_color", "&H00000000&"))
    back_col = str(cfg.get("back_color", "&H00000000&"))

    header = _ensure_ass_header(play_w, play_h)
    styles = _make_style_line(
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
    ) + "\n"

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    if anim == "wave_letters":
        wave_cfg = cfg.get("wave", {}) or {}
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
        return header + styles + events_header + dialogues

    x = cfg.get("x")
    y = cfg.get("y")
    pos_tag = f"\\pos({int(x)},{int(y)})" if (x is not None and y is not None) else ""
    tags = f"{{\\an{alignment}{pos_tag}}}"
    dialogue = f"Dialogue: 10,0:00:00.00,9:59:59.00,NAME,,0000,0000,0000,,{tags}{text}\n"
    return header + styles + events_header + dialogue


def _get_canvas(render: dict):
    canvas = render.get("canvas", {}) or {}
    w = int(canvas.get("width", 3840))
    h = int(canvas.get("height", 2160))
    if w <= 0 or h <= 0:
        raise ValueError("render.canvas.width/height must be positive integers")
    return w, h


def _make_timed_static_overlay_ass(cfg: dict, play_w: int, play_h: int, start_s: float, end_s: float) -> str:
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("after_subtitles_overlay.text is required")

    font = cfg.get("font", "Montserrat SemiBold")
    size = int(cfg.get("size", 180))
    outline = float(cfg.get("outline", 10))
    shadow = float(cfg.get("shadow", 4))
    spacing = float(cfg.get("spacing", 0))
    alignment = int(cfg.get("alignment", 5))

    primary = str(cfg.get("color", "&H00FFFFFF&"))
    outline_col = str(cfg.get("outline_color", "&H00000000&"))
    back_col = str(cfg.get("back_color", "&H00000000&"))

    x = cfg.get("x", play_w // 2)
    y = cfg.get("y", int(play_h * 0.80))

    fade_in_ms = int(cfg.get("fade_in_ms", 200))
    fade_out_ms = int(cfg.get("fade_out_ms", 200))

    header = _ensure_ass_header(play_w, play_h)
    styles = _make_style_line(
        name="AFTER",
        font=font,
        size=size,
        primary=primary,
        secondary=primary,
        outline_col=outline_col,
        back_col=back_col,
        spacing=spacing,
        outline=outline,
        shadow=shadow,
        alignment=alignment,
        margin_l=0,
        margin_r=0,
        margin_v=0,
    ) + "\n"

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    start_t = seconds_to_ass_time(start_s)
    end_t = seconds_to_ass_time(end_s)

    tags = f"{{\\an{alignment}\\pos({int(x)},{int(y)})\\fad({fade_in_ms},{fade_out_ms})}}"
    dialogue = f"Dialogue: 12,{start_t},{end_t},AFTER,,0000,0000,0000,,{tags}{text}\n"

    return header + styles + events_header + dialogue


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
            }

        render = inp.get("render", {}) or {}
        subs_cfg = render.get("subtitles", {}) or {}
        name_cfg = render.get("name_overlay", None)
        hb_cfg = render.get("happy_birthday_overlay", None)
        after_cfg = render.get("after_subtitles_overlay", None)
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

        # duration cap (existing)
        ass_end = get_ass_end_seconds(TMP_ASS)
        ass_start = get_ass_start_seconds(TMP_ASS)  # ✅ NEW
        audio_end = get_media_duration_seconds(TMP_MUSIC)
        pad = float(render.get("end_pad_seconds", 0.3))
        duration_cap = max(ass_end, audio_end) + pad if (ass_end > 0 or audio_end > 0) else None

        # 2) Optional fonts.zip -> /tmp/fonts (pure python unzip)
        fontsdir = None
        zip_key = fonts_cfg.get("zip_key")
        local_dir = fonts_cfg.get("local_dir", "/tmp/fonts")
        if zip_key:
            download_from_r2(zip_key, TMP_FONTS_ZIP)
            unzip_to_dir(TMP_FONTS_ZIP, local_dir)
            fontsdir = find_fontsdir(local_dir)

        # 3) Build video filtergraph
        filters = []
        if v_scale:
            filters.append(f"scale={v_scale}")

        subs_filter = f"subtitles={TMP_ASS}"
        if fontsdir:
            subs_filter += f":fontsdir={fontsdir}"

        force_style = subs_cfg.get("force_style", None)
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"

        filters.append(subs_filter)

        # HAPPY BIRTHDAY overlay (unchanged)
        happy_birthday_used = False
        if hb_cfg is True:
            hb_cfg = {}
        if isinstance(hb_cfg, dict):
            hb_text = str(hb_cfg.get("text", "HAPPY BIRTHDAY")).strip()
            if hb_text:
                name_y = None
                if isinstance(name_cfg, dict):
                    try:
                        name_y = int(name_cfg.get("y")) if name_cfg.get("y") is not None else None
                    except Exception:
                        name_y = None

                default_y = max(0, (name_y - 380)) if name_y is not None else int(play_h * 0.38)

                hb_full_cfg = dict(hb_cfg)
                hb_full_cfg.setdefault("text", hb_text)
                hb_full_cfg.setdefault("animation", "static")
                hb_full_cfg.setdefault("font", "Montserrat SemiBold")
                hb_full_cfg.setdefault("size", 220)
                hb_full_cfg.setdefault("x", play_w // 2)
                hb_full_cfg.setdefault("y", default_y)
                hb_full_cfg.setdefault("alignment", 5)
                hb_full_cfg.setdefault("color", "&H00FFFFFF&")
                hb_full_cfg.setdefault("outline", 10)
                hb_full_cfg.setdefault("outline_color", "&H00000000&")
                hb_full_cfg.setdefault("shadow", 0)

                hb_ass = _make_name_overlay_ass(hb_full_cfg, play_w, play_h)
                with open(TMP_HB_ASS, "w", encoding="utf-8") as f:
                    f.write(hb_ass)

                hb_filter = f"subtitles={TMP_HB_ASS}"
                if fontsdir:
                    hb_filter += f":fontsdir={fontsdir}"
                filters.append(hb_filter)
                happy_birthday_used = True

        # ✅ UPDATED: show the SAME overlay both BEFORE first subtitle and AFTER last subtitle
        before_subtitles_used = False  # ✅ NEW
        after_subtitles_used = False
        before_window = None  # ✅ NEW
        after_window = None

        if after_cfg is True:
            after_cfg = {}
        if isinstance(after_cfg, dict) and duration_cap is not None:
            enabled = bool(after_cfg.get("enabled", True))
            min_seconds = float(after_cfg.get("min_seconds", 2.0))

            if enabled:
                # BEFORE: 0 -> ass_start
                start_before = 0.0
                end_before = max(0.0, float(ass_start))
                if (end_before - start_before) >= min_seconds:
                    before_ass = _make_timed_static_overlay_ass(after_cfg, play_w, play_h, start_before, end_before)
                    with open(TMP_BEFORE_ASS, "w", encoding="utf-8") as f:
                        f.write(before_ass)

                    before_filter = f"subtitles={TMP_BEFORE_ASS}"
                    if fontsdir:
                        before_filter += f":fontsdir={fontsdir}"
                    filters.append(before_filter)
                    before_subtitles_used = True
                    before_window = {"start": start_before, "end": end_before, "min_seconds": min_seconds}

                # AFTER: ass_end -> duration_cap
                start_after = max(0.0, float(ass_end))
                end_after = float(duration_cap)
                if (end_after - start_after) >= min_seconds:
                    after_ass = _make_timed_static_overlay_ass(after_cfg, play_w, play_h, start_after, end_after)
                    with open(TMP_AFTER_ASS, "w", encoding="utf-8") as f:
                        f.write(after_ass)

                    after_filter = f"subtitles={TMP_AFTER_ASS}"
                    if fontsdir:
                        after_filter += f":fontsdir={fontsdir}"
                    filters.append(after_filter)
                    after_subtitles_used = True
                    after_window = {"start": start_after, "end": end_after, "min_seconds": min_seconds}

        # Name overlay ASS (unchanged)
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

        # 4) Audio filter (unchanged)
        af_parts = []
        if a_volume is not None:
            af_parts.append(f"volume={float(a_volume)}")
        af_parts.append("aresample=async=1")
        af = ",".join(af_parts) if af_parts else None

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

        if duration_cap is not None:
            cmd += ["-t", f"{duration_cap:.3f}"]

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
            "happy_birthday_used": happy_birthday_used,
            "name_overlay_used": name_overlay_used,
            "ffmpeg_cmd": cmd,
            "ffmpeg_stderr_tail": p.stderr[-20000:],
            "canvas": {"width": play_w, "height": play_h},
            "timing": {"loop_video": loop_video},
            "duration_cap_seconds": duration_cap,
            "ass_start_seconds": ass_start,  # ✅ NEW
            "ass_end_seconds": ass_end,
            "audio_end_seconds": audio_end,
            "before_subtitles_used": before_subtitles_used,          # ✅ NEW
            "after_subtitles_used": after_subtitles_used,
            "before_subtitles_window": before_window,               # ✅ NEW
            "after_subtitles_window": after_window,
        }

    except Exception as e:
        return {"error": "handler exception", "details": str(e)}


runpod.serverless.start({"handler": handler})
