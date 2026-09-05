import os
import re
import math
import random
import shutil
import subprocess
import tempfile
import uuid
import zipfile
import runpod

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_SHIFTED_ASS = "/tmp/subtitles_shifted.ass"

TMP_MUSIC = "/tmp/music.mp3"

# Intro audio
TMP_INTRO_BGM = "/tmp/intro_bgm.mp3"
TMP_COUNTDOWN_VO = "/tmp/countdown_vo.mp3"
TMP_HB_VO = "/tmp/hb_voice.mp3"

# ASS overlays
TMP_NAME_ASS = "/tmp/name_overlay.ass"
TMP_HB_ASS = "/tmp/happy_birthday_overlay.ass"
TMP_AFTER_ASS = "/tmp/after_subtitles_overlay.ass"
TMP_BEFORE_ASS = "/tmp/before_subtitles_overlay.ass"
TMP_COUNTDOWN_ASS = "/tmp/countdown_overlay.ass"

TMP_FONTS_ZIP = "/tmp/fonts.zip"
TMP_OUT = "/tmp/final.mp4"

# Thumbnail
TMP_THUMB_BG = "/tmp/thumb_bg.png"
TMP_THUMB_OUT = "/tmp/thumb.jpg"

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
    try:
        s3.download_file(bucket, key, local_path)
    except Exception as e:
        raise RuntimeError(f"R2 download failed (bucket={bucket}, key={key}): {e}")


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
    """
    Returns a directory that contains .ttf/.otf files.
    Handles common zip layouts like:
      - /tmp/fonts/*.ttf
      - /tmp/fonts/Fonts/*.ttf
    """
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
# Thumbnail helpers
# -----------------------------
def _escape_drawtext_text(value: str) -> str:
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace("%", "\\%")
    return s


def _find_font_file(fontsdir: str, filename: str):
    if not fontsdir or not filename:
        return None
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename
    try:
        for root, _, files in os.walk(fontsdir):
            for f in files:
                if f.lower() == str(filename).lower():
                    return os.path.join(root, f)
    except Exception:
        pass
    return None


# -----------------------------
# Duration/time helpers
# -----------------------------
def ass_time_to_seconds(t: str) -> float:
    # ASS: H:MM:SS.cs
    h, m, rest = t.split(":")
    s, cs = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def seconds_to_ass_time(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    cs = int(round(sec * 100))
    h = cs // 360000
    m = (cs % 360000) // 6000
    s = (cs % 6000) // 100
    c = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


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


def get_ass_start_seconds(path: str) -> float:
    min_start = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 3)
            if len(parts) < 2:
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
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True,
        text=True,
    )
    try:
        return float(p.stdout.strip())
    except Exception:
        return 0.0


def validate_output_audio_after_intro(
    path: str,
    start_s: float,
    check_duration_s: float = 8.0,
    silence_threshold_db: float = -55.0,
):
    """
    Controleert of de MP4 output daadwerkelijk audio bevat én of er hoorbare audio zit
    in het stuk ná de intro, waar de muziek/song aanwezig moet zijn.

    Returns:
      (True, details)  als audio ok is
      (False, details) als audio ontbreekt of te stil is
    """

    # 1) Check: bestaat er überhaupt een audiostream?
    p = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type,duration",
            "-of", "default=nw=1",
            path,
        ],
        capture_output=True,
        text=True,
    )

    if p.returncode != 0 or "codec_type=audio" not in p.stdout:
        return False, {
            "reason": "no_audio_stream",
            "ffprobe_stdout": p.stdout,
            "ffprobe_stderr": p.stderr,
        }

    # 2) Check: meet volume in het segment ná de intro
    start_s = max(0.0, float(start_s))
    p = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-ss", f"{start_s:.3f}",
            "-t", f"{float(check_duration_s):.3f}",
            "-i", path,
            "-map", "0:a:0",
            "-af", "volumedetect",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )

    log = (p.stderr or "") + "\n" + (p.stdout or "")

    m = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", log)
    if not m:
        return False, {
            "reason": "could_not_measure_audio_volume",
            "ffmpeg_log_tail": log[-4000:],
        }

    max_volume = float(m.group(1))

    if max_volume < float(silence_threshold_db):
        return False, {
            "reason": "audio_after_intro_too_quiet_or_silent",
            "max_volume_db": max_volume,
            "threshold_db": float(silence_threshold_db),
            "checked_from_seconds": start_s,
            "checked_duration_seconds": float(check_duration_s),
            "ffmpeg_log_tail": log[-4000:],
        }

    return True, {
        "max_volume_db": max_volume,
        "checked_from_seconds": start_s,
        "checked_duration_seconds": float(check_duration_s),
    }


def shift_ass_dialogue_times(in_path: str, out_path: str, offset_s: float):
    """
    Shifts Dialogue start/end times by +offset_s seconds.
    """
    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out_lines = []
    for line in lines:
        if not line.startswith("Dialogue:"):
            out_lines.append(line)
            continue

        parts = line.split(",", 3)
        if len(parts) < 4:
            out_lines.append(line)
            continue

        head = parts[0]
        start_t = parts[1].strip()
        end_t = parts[2].strip()
        tail = parts[3]

        try:
            start_s = ass_time_to_seconds(start_t) + float(offset_s)
            end_s = ass_time_to_seconds(end_t) + float(offset_s)
            new_start = seconds_to_ass_time(start_s)
            new_end = seconds_to_ass_time(end_s)
            out_lines.append(f"{head},{new_start},{new_end},{tail}")
        except Exception:
            out_lines.append(line)

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)


# -----------------------------
# Detect leading silence (only for subtitle alignment)
# -----------------------------
def detect_leading_silence_seconds(path: str, threshold_db: float, min_silence: float, max_trim: float) -> float:
    """
    Detects leading silence that starts at 0 and returns silence_end seconds.
    Caps to max_trim.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"silencedetect=n={threshold_db}dB:d={min_silence}",
        "-f", "null", "-"
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    log = (p.stderr or "") + "\n" + (p.stdout or "")

    if not re.search(r"silence_start:\s*0(\.0+)?", log):
        return 0.0

    m_end = re.search(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", log)
    if not m_end:
        return 0.0

    try:
        t = float(m_end.group(1))
        if t <= 0:
            return 0.0
        return min(t, float(max_trim))
    except Exception:
        return 0.0


# -----------------------------
# ASS overlay builders
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


def _make_wave_letter_dialogues(text: str, style_name: str, play_w: int, play_h: int, size: int, wave_cfg: dict, layer: int = 10) -> str:
    center_x = int(wave_cfg.get("center_x", play_w // 2))
    center_y = int(wave_cfg.get("center_y", play_h // 2))
    letter_spacing = float(wave_cfg.get("letter_spacing", 18))
    width_factor = float(wave_cfg.get("approx_char_width_factor", 0.62))

    amplitude = float(wave_cfg.get("amplitude_px", 40))
    scale_peak = float(wave_cfg.get("scale_peak", 112))

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
        dialogues.append(f"Dialogue: {layer},0:00:00.00,9:59:59.00,{style_name},,0000,0000,0000,,{tags}{ch}\n")
        running_x += advances[i]

    return "".join(dialogues)


def _make_name_overlay_ass(cfg: dict, play_w: int, play_h: int) -> str:
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("overlay.text is required")

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

    events_header = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

    if anim == "wave_letters":
        wave_cfg = cfg.get("wave", {}) or {}
        x = cfg.get("x")
        y = cfg.get("y")
        if x is not None and y is not None:
            wave_cfg = dict(wave_cfg)
            wave_cfg["center_x"] = int(x)
            wave_cfg["center_y"] = int(y)

        dialogues = _make_wave_letter_dialogues(text=text, style_name="NAME", play_w=play_w, play_h=play_h, size=size, wave_cfg=wave_cfg, layer=10)
        return header + styles + events_header + dialogues

    x = cfg.get("x")
    y = cfg.get("y")
    pos_tag = f"\\pos({int(x)},{int(y)})" if (x is not None and y is not None) else ""
    tags = f"{{\\an{alignment}{pos_tag}}}"
    dialogue = f"Dialogue: 10,0:00:00.00,9:59:59.00,NAME,,0000,0000,0000,,{tags}{text}\n"
    return header + styles + events_header + dialogue


def _make_timed_static_overlay_ass(cfg: dict, play_w: int, play_h: int, start_s: float, end_s: float) -> str:
    text = str(cfg.get("text", "")).strip()
    if not text:
        raise ValueError("overlay.text is required")

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
        name="OVER",
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

    events_header = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    start_t = seconds_to_ass_time(start_s)
    end_t = seconds_to_ass_time(end_s)
    tags = f"{{\\an{alignment}\\pos({int(x)},{int(y)})\\fad({fade_in_ms},{fade_out_ms})}}"
    dialogue = f"Dialogue: 12,{start_t},{end_t},OVER,,0000,0000,0000,,{tags}{text}\n"
    return header + styles + events_header + dialogue


def _make_countdown_overlay_ass(cfg: dict, play_w: int, play_h: int) -> str:
    cfg = cfg or {}

    font = str(cfg.get("font", "Boogaloo"))
    size = int(cfg.get("size", 900))
    color = str(cfg.get("color", "&H00FFFFFF&"))
    outline = float(cfg.get("outline", 18))
    outline_color = str(cfg.get("outline_color", "&H00000000&"))
    shadow = float(cfg.get("shadow", 6))
    back_color = str(cfg.get("back_color", "&H00000000&"))
    spacing = float(cfg.get("spacing", 0))
    alignment = int(cfg.get("alignment", 5))

    x = int(cfg.get("x", play_w // 2))
    y = int(cfg.get("y", play_h // 2))

    fade_in_ms = int(cfg.get("fade_in_ms", 60))
    fade_out_ms = int(cfg.get("fade_out_ms", 120))

    timing = cfg.get("timing", {}) or {}
    start0 = float(timing.get("start_seconds", 0.0))
    step = float(timing.get("step_seconds", 0.6))
    dur = float(timing.get("duration_seconds", 0.6))
    labels = timing.get("labels", ["3", "2", "1"])
    labels = [str(v) for v in labels] if isinstance(labels, list) else ["3", "2", "1"]

    header = _ensure_ass_header(play_w, play_h)
    styles = _make_style_line(
        name="COUNT",
        font=font,
        size=size,
        primary=color,
        secondary=color,
        outline_col=outline_color,
        back_col=back_color,
        spacing=spacing,
        outline=outline,
        shadow=shadow,
        alignment=alignment,
        margin_l=int(cfg.get("margin_l", 0)),
        margin_r=int(cfg.get("margin_r", 0)),
        margin_v=int(cfg.get("margin_v", 0)),
    ) + "\n"

    events_header = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

    layer = int(cfg.get("layer", 50))

    def dlg(s0: float, s1: float, txt: str) -> str:
        st = seconds_to_ass_time(s0)
        et = seconds_to_ass_time(s1)
        tags = f"{{\\an{alignment}\\pos({x},{y})\\fad({fade_in_ms},{fade_out_ms})}}"
        return f"Dialogue: {layer},{st},{et},COUNT,,0000,0000,0000,,{tags}{txt}\n"

    out = []
    for i, lab in enumerate(labels[:3]):
        s0 = start0 + i * step
        out.append(dlg(s0, s0 + dur, lab))

    return header + styles + events_header + "".join(out)


def _get_canvas(render: dict):
    canvas = render.get("canvas", {}) or {}
    w = int(canvas.get("width", 3840))
    h = int(canvas.get("height", 2160))
    if w <= 0 or h <= 0:
        raise ValueError("render.canvas.width/height must be positive integers")
    return w, h


# -----------------------------
# Theme slideshow renderer
# -----------------------------
_SLIDESHOW_ANIMATIONS = {
    "zoom_in", "zoom_out", "pan_left", "pan_right",
    "pan_up", "pan_down", "static",
}

_SLIDESHOW_TRANSITIONS = {
    "fade", "fadeblack", "fadewhite", "smoothleft", "smoothright",
    "smoothup", "smoothdown", "wipeleft", "wiperight", "wipeup",
    "wipedown", "circleopen", "circleclose", "dissolve",
}


def _safe_job_component(value) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "job"))
    return value.strip("-.")[:80] or "job"


def _escape_ass_text(value: str) -> str:
    """Escape user text while retaining explicit newlines as ASS line breaks."""
    value = str(value).replace("\\", r"\\")
    value = value.replace("{", r"\{").replace("}", r"\}")
    return value.replace("\r\n", r"\N").replace("\n", r"\N").replace("\r", r"\N")


def _make_text_events_ass(events: list, play_w: int, play_h: int) -> str:
    """Build one ASS document containing independently timed theme text events."""
    if not isinstance(events, list):
        raise ValueError("render.text_events must be an array")

    header = _ensure_ass_header(play_w, play_h)
    styles = []
    dialogues = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise ValueError(f"render.text_events[{index}] must be an object")
        text = str(raw.get("text", "")).strip()
        if not text:
            continue

        start_s = max(0.0, float(raw.get("start_seconds", 0.0)))
        end_s = max(start_s + 0.05, float(raw.get("end_seconds", start_s + 2.0)))
        style_name = f"EVENT{index}"
        font = str(raw.get("font", "Montserrat ExtraBold"))
        size = int(raw.get("size", 240))
        alignment = int(raw.get("alignment", 5))
        x = int(raw.get("x", play_w // 2))
        y = int(raw.get("y", play_h // 2))
        fade_in_ms = max(0, int(raw.get("fade_in_ms", 250)))
        fade_out_ms = max(0, int(raw.get("fade_out_ms", 250)))
        animation = str(raw.get("animation", "fade")).strip().lower()

        styles.append(_make_style_line(
            name=style_name,
            font=font,
            size=size,
            primary=str(raw.get("color", "&H00FFFFFF&")),
            secondary=str(raw.get("secondary_color", "&H0033CCFF&")),
            outline_col=str(raw.get("outline_color", "&H00000000&")),
            back_col=str(raw.get("back_color", "&H00000000&")),
            spacing=float(raw.get("spacing", 1)),
            outline=float(raw.get("outline", 12)),
            shadow=float(raw.get("shadow", 4)),
            alignment=alignment,
            margin_l=0,
            margin_r=0,
            margin_v=0,
        ))

        duration_ms = max(50, int(round((end_s - start_s) * 1000)))
        tags = [f"\\an{alignment}", f"\\pos({x},{y})", f"\\fad({fade_in_ms},{fade_out_ms})"]
        if animation == "pop":
            pop_ms = min(450, max(100, duration_ms // 4))
            tags.extend([
                "\\fscx70\\fscy70",
                f"\\t(0,{pop_ms},\\fscx110\\fscy110)",
                f"\\t({pop_ms},{min(duration_ms, pop_ms * 2)},\\fscx100\\fscy100)",
            ])
        elif animation == "pulse":
            midpoint = max(100, duration_ms // 2)
            tags.extend([
                f"\\t(0,{midpoint},\\fscx108\\fscy108)",
                f"\\t({midpoint},{duration_ms},\\fscx100\\fscy100)",
            ])

        dialogues.append(
            "Dialogue: 20,{start},{end},{style},,0000,0000,0000,,{{{tags}}}{text}\n".format(
                start=seconds_to_ass_time(start_s),
                end=seconds_to_ass_time(end_s),
                style=style_name,
                tags="".join(tags),
                text=_escape_ass_text(text),
            )
        )

    if not dialogues:
        return ""
    events_header = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    return header + "".join(styles) + "\n" + events_header + "".join(dialogues)


def _slideshow_zoompan(animation: str, frames: int, zoom_speed: float) -> tuple[str, str, str]:
    animation = animation if animation in _SLIDESHOW_ANIMATIONS else "zoom_in"
    frames = max(1, int(frames))
    speed = max(0.00005, min(float(zoom_speed), 0.01))
    progress = f"min(1,on/{frames})"

    if animation == "zoom_out":
        return (f"if(eq(on,0),1.12,max(1.001,zoom-{speed:.6f}))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
    if animation == "pan_left":
        return ("1.10", f"(iw-iw/zoom)*(1-{progress})", "(ih-ih/zoom)/2")
    if animation == "pan_right":
        return ("1.10", f"(iw-iw/zoom)*({progress})", "(ih-ih/zoom)/2")
    if animation == "pan_up":
        return ("1.10", "(iw-iw/zoom)/2", f"(ih-ih/zoom)*(1-{progress})")
    if animation == "pan_down":
        return ("1.10", "(iw-iw/zoom)/2", f"(ih-ih/zoom)*({progress})")
    if animation == "static":
        return ("1.001", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2")
    return (f"min(zoom+{speed:.6f},1.12)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")


def _render_slideshow(*, inp: dict, render: dict, job_id, fontsdir: str, play_w: int, play_h: int):
    image_keys = inp.get("image_keys")
    music_key = inp.get("music_key")
    if not isinstance(image_keys, list) or not image_keys or not all(isinstance(k, str) and k.strip() for k in image_keys):
        return {"error": "Missing required input image_keys (non-empty string array)."}
    if not music_key:
        return {"error": "Missing required input music_key."}

    slideshow_cfg = render.get("slideshow", {}) or {}
    video_cfg = render.get("video", {}) or {}
    audio_cfg = render.get("audio", {}) or {}
    intro_cfg = render.get("intro", {}) or {}
    thumb_cfg = render.get("thumbnail", None)

    fps = max(1, min(int(slideshow_cfg.get("fps", 30)), 60))
    # Keep multi-input transitions within the Theme worker's memory limit. The
    # final zoompan stage still emits the requested 4K canvas, but xfade no
    # longer has to retain several 4K frames for every source image.
    work_scale = min(1.0, 1920.0 / play_w, 1080.0 / play_h)
    work_w = max(2, (int(play_w * work_scale) // 2) * 2)
    work_h = max(2, (int(play_h * work_scale) // 2) * 2)
    preferred_hold = max(1.0, float(slideshow_cfg.get("image_duration_seconds", 5.0)))
    transition_seconds = max(0.0, float(slideshow_cfg.get("transition_seconds", 0.6)))
    zoom_speed = float(slideshow_cfg.get("zoom_speed", 0.00035))
    seed = int(slideshow_cfg.get("seed", 0) or 0)
    randomizer = random.Random(seed)
    requested_animations = slideshow_cfg.get("animations", []) or []
    requested_transitions = slideshow_cfg.get("transitions", []) or []

    intro_enabled = bool(intro_cfg.get("enabled", False))
    intro_seconds = max(0.0, float(intro_cfg.get("length_seconds", 0.0))) if intro_enabled else 0.0
    end_pad_seconds = max(0.0, float(render.get("end_pad_seconds", 0.3)))

    job_dir = tempfile.mkdtemp(prefix=f"birthday-theme-{_safe_job_component(job_id)}-", dir="/tmp")
    music_path = os.path.join(job_dir, "music.mp3")
    out_path = os.path.join(job_dir, "final.mp4")
    text_ass_path = os.path.join(job_dir, "text-events.ass")
    supplied_ass_path = os.path.join(job_dir, "overlay.ass")
    intro_bgm_path = os.path.join(job_dir, "intro-bgm.mp3")
    image_paths = []

    try:
        for index, key in enumerate(image_keys):
            ext = os.path.splitext(key)[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
                ext = ".img"
            local_path = os.path.join(job_dir, f"image-{index:03d}{ext}")
            download_from_r2(key, local_path)
            image_paths.append(local_path)
        download_from_r2(music_key, music_path)

        measured_music_duration = get_media_duration_seconds(music_path)
        supplied_music_duration = float(inp.get("audio_duration_seconds", 0.0) or 0.0)
        music_duration = measured_music_duration if measured_music_duration > 0 else supplied_music_duration
        if music_duration <= 0:
            return {"error": "Could not determine music duration."}

        total_duration = intro_seconds + music_duration + end_pad_seconds
        image_count = len(image_paths)
        transition_seconds = min(transition_seconds, preferred_hold / 2.0)
        if image_count == 1:
            transition_seconds = 0.0
        segment_duration = (total_duration + (image_count - 1) * transition_seconds) / image_count
        segment_frames = max(1, int(math.ceil(segment_duration * fps)))

        animations = []
        for index in range(image_count):
            candidate = requested_animations[index] if index < len(requested_animations) else randomizer.choice(sorted(_SLIDESHOW_ANIMATIONS - {"static"}))
            animations.append(candidate if candidate in _SLIDESHOW_ANIMATIONS else "zoom_in")

        transitions = []
        for index in range(max(0, image_count - 1)):
            candidate = requested_transitions[index] if index < len(requested_transitions) else randomizer.choice(["fade", "dissolve", "smoothleft", "smoothright"])
            transitions.append(candidate if candidate in _SLIDESHOW_TRANSITIONS else "fade")

        cmd = ["ffmpeg", "-y"]
        for image_path in image_paths:
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{segment_duration:.6f}", "-i", image_path]
        music_input_index = image_count
        cmd += ["-i", music_path]

        intro_bgm_input_index = None
        intro_bgm_key = intro_cfg.get("bgm_key") if intro_enabled else None
        if intro_bgm_key:
            download_from_r2(intro_bgm_key, intro_bgm_path)
            intro_bgm_input_index = music_input_index + 1
            cmd += ["-i", intro_bgm_path]

        fc = []
        video_labels = []
        for index, animation in enumerate(animations):
            label = f"slide{index}"
            fc.append(
                f"[{index}:v]scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
                f"crop={work_w}:{work_h},setsar=1,trim=duration={segment_duration:.6f},"
                f"fps={fps},format=yuv420p[{label}]"
            )
            video_labels.append(f"[{label}]")

        current_video = video_labels[0]
        if image_count > 1:
            for index in range(1, image_count):
                out_label = f"xfade{index}"
                offset = index * (segment_duration - transition_seconds)
                fc.append(
                    f"{current_video}{video_labels[index]}xfade=transition={transitions[index - 1]}:"
                    f"duration={transition_seconds:.6f}:offset={offset:.6f}[{out_label}]"
                )
                current_video = f"[{out_label}]"

        # Applying zoompan after xfade avoids FFmpeg's undefined-frame-rate output
        # from zoompan being rejected by xfade, while every still remains in motion.
        motion_period = max(1, segment_frames)
        global_zoom = f"1.001+0.079*abs(sin(on/{motion_period}*PI))"
        video_filter_tail = [
            f"zoompan=z='{global_zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={play_w}x{play_h}:fps={fps}",
            f"trim=duration={total_duration:.6f}",
            "setpts=PTS-STARTPTS",
        ]

        supplied_ass_key = inp.get("overlay_ass_key")
        if supplied_ass_key:
            download_from_r2(supplied_ass_key, supplied_ass_path)
            supplied_filter = f"subtitles={supplied_ass_path}"
            if fontsdir:
                supplied_filter += f":fontsdir={fontsdir}"
            video_filter_tail.append(supplied_filter)

        generated_ass = _make_text_events_ass(render.get("text_events", []) or [], play_w, play_h)
        if generated_ass:
            with open(text_ass_path, "w", encoding="utf-8") as handle:
                handle.write(generated_ass)
            generated_filter = f"subtitles={text_ass_path}"
            if fontsdir:
                generated_filter += f":fontsdir={fontsdir}"
            video_filter_tail.append(generated_filter)

        fc.append(f"{current_video}{','.join(video_filter_tail)}[vout]")

        song_chain = f"[{music_input_index}:a]asetpts=PTS-STARTPTS"
        if audio_cfg.get("volume") is not None:
            song_chain += f",volume={float(audio_cfg['volume'])}"
        song_chain += "[songa]"
        fc.append(song_chain)

        if intro_seconds > 0:
            if intro_bgm_input_index is not None:
                fade_duration = min(0.4, intro_seconds)
                fc.append(
                    f"[{intro_bgm_input_index}:a]atrim=0:{intro_seconds:.6f},asetpts=PTS-STARTPTS,"
                    f"afade=t=out:st={max(0.0, intro_seconds - fade_duration):.6f}:d={fade_duration:.6f}[introa]"
                )
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{intro_seconds:.6f}[introa]")
            fc.append("[introa][songa]concat=n=2:v=0:a=1[basea]")
        else:
            fc.append("[songa]anull[basea]")

        if end_pad_seconds > 0:
            fc.append(f"[basea]apad=pad_dur={end_pad_seconds:.6f}[aout]")
        else:
            fc.append("[basea]anull[aout]")

        filter_complex = ";".join(fc)
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", str(video_cfg.get("codec", "libx264")),
            "-preset", str(video_cfg.get("preset", "medium")),
            "-crf", str(video_cfg.get("crf", 18)),
            "-pix_fmt", str(video_cfg.get("pix_fmt", "yuv420p")),
            "-r", str(fps),
            "-c:a", str(audio_cfg.get("codec", "aac")),
            "-b:a", str(audio_cfg.get("bitrate", "192k")),
            "-movflags", "+faststart",
            "-t", f"{total_duration:.6f}", out_path,
        ]

        process = subprocess.run(cmd, capture_output=True, text=True)
        if process.returncode != 0:
            return {"error": "slideshow ffmpeg failed", "returncode": process.returncode, "stderr": process.stderr[-20000:], "cmd": cmd, "filter_complex": filter_complex}

        check_start = intro_seconds + min(2.0, max(0.0, music_duration / 4.0))
        check_duration = min(8.0, max(1.0, music_duration - (check_start - intro_seconds)))
        audio_ok, audio_check = validate_output_audio_after_intro(
            out_path, start_s=check_start, check_duration_s=check_duration,
            silence_threshold_db=float(audio_cfg.get("post_audio_silence_threshold_db", -55.0)),
        )
        if not audio_ok:
            return {"error": "slideshow output audio validation failed", "audio_check": audio_check, "ffmpeg_stderr_tail": process.stderr[-20000:]}

        out_key = inp.get("out_key") or (f"jobs/{job_id}/final.mp4" if job_id else f"outputs/{uuid.uuid4().hex}.mp4")
        uploaded = upload_to_r2(out_path, out_key)

        thumbnail_result = None
        thumbnail_cmd = None
        if isinstance(thumb_cfg, dict) and bool(thumb_cfg.get("enabled", False)):
            effective_thumb_cfg = dict(thumb_cfg)
            effective_thumb_cfg.setdefault("background_key", image_keys[0])
            thumbnail_result, thumbnail_cmd = _render_thumbnail(
                thumb_cfg=effective_thumb_cfg, job_id=job_id,
                name_cfg=render.get("name_overlay", None), fontsdir=fontsdir,
            )

        return {
            "status": "ok", "mode": "render_slideshow", "jobId": job_id,
            "out_key": out_key, "uploaded": uploaded,
            "thumbnail_uploaded": thumbnail_result, "thumbnail_ffmpeg_cmd": thumbnail_cmd,
            "canvas": {"width": play_w, "height": play_h}, "fps": fps,
            "work_canvas": {"width": work_w, "height": work_h},
            "image_count": image_count, "image_keys": image_keys,
            "animations": animations, "transitions": transitions, "seed": seed,
            "preferred_image_duration_seconds": preferred_hold,
            "actual_segment_duration_seconds": segment_duration,
            "transition_seconds": transition_seconds,
            "supplied_audio_duration_seconds": supplied_music_duration,
            "measured_audio_duration_seconds": measured_music_duration,
            "intro_seconds": intro_seconds, "total_duration_seconds": total_duration,
            "overlay_ass_used": bool(supplied_ass_key),
            "generated_text_events_used": bool(generated_ass),
            "audio_validation": audio_check, "ffmpeg_cmd": cmd,
            "filter_complex": filter_complex, "ffmpeg_stderr_tail": process.stderr[-20000:],
        }
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# -----------------------------
# Thumbnail renderer
# -----------------------------
def _render_thumbnail(*, thumb_cfg: dict, job_id, name_cfg, fontsdir: str):
    thumb_result = None
    thumb_cmd = None

    if thumb_cfg is True:
        thumb_cfg = {}
    if isinstance(thumb_cfg, dict) and bool(thumb_cfg.get("enabled", False)):
        bg_key = thumb_cfg.get("background_key")
        if not bg_key:
            raise ValueError("thumbnail.enabled is true but thumbnail.background_key is missing")

        thumb_out_key = thumb_cfg.get("out_key") or (
            f"jobs/{job_id}/thumb.jpg" if job_id else f"outputs/{uuid.uuid4().hex}.jpg"
        )

        size_cfg = thumb_cfg.get("size", {}) or {}
        # Render 16:9 thumbnails at 4K by default. YouTube's upload endpoint
        # imposes a 2 MB file limit, so the encoder below validates the final
        # JPEG and gracefully steps down in quality/resolution when necessary.
        tw = int(size_cfg.get("width", 3840))
        th = int(size_cfg.get("height", 2160))
        if tw < 2 or th < 2:
            raise ValueError("thumbnail.size.width and thumbnail.size.height must be at least 2")

        name_text_cfg = thumb_cfg.get("name_text", {}) or {}
        thumb_text = str(
            name_text_cfg.get("text")
            or (name_cfg.get("text") if isinstance(name_cfg, dict) else "")
        ).strip()
        if not thumb_text:
            raise ValueError("thumbnail.enabled is true but thumbnail.name_text.text is missing (and name_overlay.text not available)")

        download_from_r2(bg_key, TMP_THUMB_BG)

        fontfile_name = name_text_cfg.get("fontfile_name")
        fontfile = name_text_cfg.get("fontfile")
        resolved_fontfile = None
        if fontfile:
            resolved_fontfile = _find_font_file(fontsdir, fontfile) if fontsdir else (fontfile if os.path.exists(fontfile) else None)
        if not resolved_fontfile and fontfile_name and fontsdir:
            resolved_fontfile = _find_font_file(fontsdir, fontfile_name)

        fontsize = int(name_text_cfg.get("fontsize", 220))
        fontcolor = str(name_text_cfg.get("color", "#FFFFFF"))
        x_expr = str(name_text_cfg.get("x", "(w-text_w)/2"))
        y_expr = str(name_text_cfg.get("y", "(h-text_h)/2"))

        borderw = int(name_text_cfg.get("borderw", 0))
        bordercolor = str(name_text_cfg.get("bordercolor", "white"))
        shadowx = int(name_text_cfg.get("shadowx", 0))
        shadowy = int(name_text_cfg.get("shadowy", 0))
        shadowcolor = str(name_text_cfg.get("shadowcolor", "black@0.0"))

        safe_text = _escape_drawtext_text(thumb_text)

        drawtext_parts = []
        if resolved_fontfile:
            drawtext_parts.append(f"fontfile='{resolved_fontfile}'")
        else:
            fontname = str(name_text_cfg.get("font", "Roboto Black"))
            drawtext_parts.append(f"font='{fontname}'")

        drawtext_parts += [
            f"text='{safe_text}'",
            f"fontsize={fontsize}",
            f"fontcolor={fontcolor}",
            f"x={x_expr}",
            f"y={y_expr}",
            f"borderw={borderw}",
            f"bordercolor={bordercolor}",
            f"shadowx={shadowx}",
            f"shadowy={shadowy}",
            f"shadowcolor={shadowcolor}",
        ]

        # Keep a small margin below YouTube's 2,000,000-byte limit. First
        # preserve 4K and trade a little JPEG quality; only then fall back to
        # still-high resolutions. Callers can override every value per theme.
        max_bytes = int(thumb_cfg.get("max_bytes", 1_950_000))
        if max_bytes < 1:
            raise ValueError("thumbnail.max_bytes must be positive")
        initial_quality = max(2, min(31, int(thumb_cfg.get("jpg_quality", 2))))
        quality_candidates = []
        for quality in (initial_quality, initial_quality + 1, initial_quality + 2,
                        initial_quality + 4, initial_quality + 6, initial_quality + 8,
                        initial_quality + 10):
            quality = min(31, quality)
            if quality not in quality_candidates:
                quality_candidates.append(quality)

        requested_widths = [tw, 3200, 2560, 2048, 1920]
        width_candidates = []
        for width in requested_widths:
            if 2 <= width <= tw and width not in width_candidates:
                width_candidates.append(width)

        selected = None
        attempts = []
        for width in width_candidates:
            height = max(2, int(round(th * (width / tw))) // 2 * 2)
            scale_crop = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
            vf_thumb = f"{scale_crop},drawtext=" + ":".join(drawtext_parts)
            for quality in quality_candidates:
                thumb_cmd = [
                    "ffmpeg", "-y",
                    "-i", TMP_THUMB_BG,
                    "-vf", vf_thumb,
                    "-frames:v", "1",
                    "-c:v", "mjpeg",
                    "-q:v", str(quality),
                    TMP_THUMB_OUT,
                ]
                tp = subprocess.run(thumb_cmd, capture_output=True, text=True)
                if tp.returncode != 0:
                    raise RuntimeError(
                        "thumbnail ffmpeg failed\n"
                        f"returncode={tp.returncode}\n"
                        f"stderr_tail={tp.stderr[-20000:]}\n"
                        f"stdout_tail={tp.stdout[-20000:]}\n"
                        f"cmd={thumb_cmd}"
                    )
                size_bytes = os.path.getsize(TMP_THUMB_OUT)
                attempts.append({"width": width, "height": height, "jpg_quality": quality, "size_bytes": size_bytes})
                if size_bytes <= max_bytes:
                    selected = attempts[-1]
                    break
            if selected:
                break

        if not selected:
            last_attempt = attempts[-1] if attempts else None
            raise RuntimeError(
                "thumbnail could not be encoded below the configured max_bytes "
                f"({max_bytes}); last_attempt={last_attempt}"
            )

        uploaded = upload_to_r2(TMP_THUMB_OUT, thumb_out_key)
        thumb_result = {
            **uploaded,
            "width": selected["width"],
            "height": selected["height"],
            "size_bytes": selected["size_bytes"],
            "jpg_quality": selected["jpg_quality"],
            "max_bytes": max_bytes,
            "attempts": attempts,
        }

    return thumb_result, thumb_cmd


# -----------------------------
# Main handler
# -----------------------------
def handler(event):
    try:
        inp = (event or {}).get("input", {}) or {}
        mode = inp.get("mode", "render")
        if mode not in ("render", "render_slideshow", "thumbnail"):
            return {"error": f"Unknown mode: {mode}. Use mode='render', mode='render_slideshow', or mode='thumbnail'."}

        job_id = inp.get("jobId")
        render = inp.get("render", {}) or {}

        subs_cfg = render.get("subtitles", {}) or {}
        name_cfg = render.get("name_overlay", None)
        hb_cfg = render.get("happy_birthday_overlay", None)
        after_cfg = render.get("after_subtitles_overlay", None)
        before_cfg = render.get("before_subtitles_overlay", None)
        countdown_cfg = render.get("countdown_overlay", {}) or {}

        thumb_cfg = render.get("thumbnail", None)
        timing_cfg = render.get("timing", {}) or {}
        fonts_cfg = render.get("fonts", {}) or {}

        intro_cfg = render.get("intro", {}) or {}
        intro_enabled = bool(intro_cfg.get("enabled", False))
        intro_len_min = float(intro_cfg.get("length_seconds", 5.0))  # minimum intro
        normal_text_start = float(intro_cfg.get("normal_text_start_seconds", 2.0))

        # Current intro behavior
        countdown_seconds = float(intro_cfg.get("countdown_seconds", 2.0))
        hb_voice_delay_seconds = float(intro_cfg.get("hb_voice_delay_seconds", 2.0))

        # NEW: re-use same HB voice at end of song
        add_hb_voice_to_song_end = bool(intro_cfg.get("add_hb_voice_to_song_end", True))
        song_end_hb_voice_delay_seconds = float(intro_cfg.get("song_end_hb_voice_delay_seconds", 0.0))

        play_w, play_h = _get_canvas(render)

        # Fonts
        fontsdir = None
        zip_key = fonts_cfg.get("zip_key")
        local_dir = fonts_cfg.get("local_dir", "/tmp/fonts")
        if zip_key:
            download_from_r2(zip_key, TMP_FONTS_ZIP)
            unzip_to_dir(TMP_FONTS_ZIP, local_dir)
            fontsdir = find_fontsdir(local_dir)

        # Thumbnail-only mode
        if mode == "thumbnail":
            thumb_result, thumb_cmd = _render_thumbnail(
                thumb_cfg=thumb_cfg,
                job_id=job_id,
                name_cfg=name_cfg,
                fontsdir=fontsdir,
            )
            return {
                "status": "ok",
                "mode": "thumbnail",
                "jobId": job_id,
                "thumbnail_uploaded": thumb_result,
                "thumbnail_ffmpeg_cmd": thumb_cmd,
                "fontsdir_used": fontsdir,
                "canvas": {"width": play_w, "height": play_h},
            }

        if mode == "render_slideshow":
            return _render_slideshow(
                inp=inp,
                render=render,
                job_id=job_id,
                fontsdir=fontsdir,
                play_w=play_w,
                play_h=play_h,
            )

        # Render mode inputs
        video_key = inp.get("video_key")
        ass_key = inp.get("ass_key")
        music_key = inp.get("music_key")
        out_key = inp.get("out_key") or (f"posts/{job_id}/final.mp4" if job_id else f"outputs/{uuid.uuid4().hex}.mp4")

        if not video_key or not ass_key or not music_key:
            return {"error": "Missing required inputs.", "required": ["video_key", "ass_key", "music_key"]}

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

        # Download base assets
        download_from_r2(video_key, TMP_IN)
        download_from_r2(ass_key, TMP_ASS)
        download_from_r2(music_key, TMP_MUSIC)

        hb_voice_dur = 0.0
        intro_len = 0.0
        hb_voice_available = False

        # Intro assets (and compute dynamic intro length)
        if intro_enabled:
            missing = [k for k in ["bgm_key", "countdown_key", "hb_voice_key"] if not intro_cfg.get(k)]
            if missing:
                return {"error": "intro.enabled is true but missing intro keys", "missing": missing}

            download_from_r2(intro_cfg["bgm_key"], TMP_INTRO_BGM)
            download_from_r2(intro_cfg["countdown_key"], TMP_COUNTDOWN_VO)
            download_from_r2(intro_cfg["hb_voice_key"], TMP_HB_VO)

            hb_voice_dur = max(0.0, get_media_duration_seconds(TMP_HB_VO))
            hb_voice_available = hb_voice_dur > 0.0

            # Dynamic intro length: minimum vs spoken audio length
            intro_len = max(
                intro_len_min,
                countdown_seconds,
                hb_voice_delay_seconds + hb_voice_dur
            )
        else:
            intro_len = 0.0

        # End HB voice rules
        end_hb_voice_enabled = bool(intro_enabled and hb_voice_available and add_hb_voice_to_song_end)
        hb_vol_intro = float(intro_cfg.get("hb_voice_volume", 1.0))
        hb_vol_song_end = float(intro_cfg.get("song_end_hb_voice_volume", hb_vol_intro))

        # Trim config (optional)
        trim_cfg = audio_cfg.get("trim_silence", {}) or {}
        trim_enabled = bool(trim_cfg.get("enabled", False))
        threshold_db = float(trim_cfg.get("threshold_db", -45))
        min_silence = float(trim_cfg.get("min_silence_seconds", 0.08))
        max_leading_trim = float(trim_cfg.get("max_leading_trim_seconds", 2.0))
        end_silence = float(trim_cfg.get("end_silence_seconds", 0.20))
        subtitle_nudge = float(trim_cfg.get("subtitle_nudge_seconds", 0.0))  # optional fine-tune

        lead_trim = 0.0
        if trim_enabled:
            lead_trim = detect_leading_silence_seconds(
                TMP_MUSIC,
                threshold_db=threshold_db,
                min_silence=min_silence,
                max_trim=max_leading_trim,
            )

        # Karaoke ASS shift:
        # - intro adds intro_len delay
        # - trimming removes lead_trim delay
        effective_ass_shift = ((intro_len - lead_trim) if intro_enabled else (-lead_trim)) + subtitle_nudge

        ass_path_for_render = TMP_ASS
        if abs(effective_ass_shift) > 1e-6:
            shift_ass_dialogue_times(TMP_ASS, TMP_SHIFTED_ASS, effective_ass_shift)
            ass_path_for_render = TMP_SHIFTED_ASS

        # Timings based on final ASS
        # ass_end = absolute "last sung word ended" in the final output timeline
        ass_end = get_ass_end_seconds(ass_path_for_render)
        ass_start = get_ass_start_seconds(ass_path_for_render)

        music_dur = get_media_duration_seconds(TMP_MUSIC)
        effective_song_dur = max(0.0, music_dur - lead_trim)

        # Base audio ends here, before any added end HB voice
        base_audio_end_est = (intro_len if intro_enabled else 0.0) + effective_song_dur

        # NEW: end HB voice starts exactly when singing ends (ASS last end),
        # optionally plus a tiny user-defined delay.
        song_end_hb_start = max(0.0, ass_end + song_end_hb_voice_delay_seconds) if end_hb_voice_enabled else None
        song_end_hb_end = (song_end_hb_start + hb_voice_dur) if (end_hb_voice_enabled and song_end_hb_start is not None) else None

        pad = float(render.get("end_pad_seconds", 0.3))
        total_audio_len_est = max(
            base_audio_end_est,
            (song_end_hb_end or 0.0)
        )
        duration_cap = max(ass_end, total_audio_len_est) + pad if (ass_end > 0 or total_audio_len_est > 0) else None

        # -----------------------------
        # Build video filter chain (ALL with fontsdir)
        # -----------------------------
        vf_filters = []
        if v_scale:
            vf_filters.append(f"scale={v_scale}")

        # Main karaoke subtitles
        subs_filter = f"subtitles={ass_path_for_render}"
        if fontsdir:
            subs_filter += f":fontsdir={fontsdir}"
        force_style = subs_cfg.get("force_style", None)
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"
        vf_filters.append(subs_filter)

        # Countdown overlay during intro
        if intro_enabled:
            countdown_ass = _make_countdown_overlay_ass(countdown_cfg, play_w, play_h)
            with open(TMP_COUNTDOWN_ASS, "w", encoding="utf-8") as f:
                f.write(countdown_ass)

            cd_filter = f"subtitles={TMP_COUNTDOWN_ASS}"
            if fontsdir:
                cd_filter += f":fontsdir={fontsdir}"
            vf_filters.append(cd_filter)

        # HAPPY BIRTHDAY overlay (shift to normal_text_start)
        happy_birthday_used = False
        if hb_cfg is True:
            hb_cfg = {}
        if isinstance(hb_cfg, dict):
            hb_text = str(hb_cfg.get("text", "")).strip()
            if hb_text:
                hb_full_cfg = dict(hb_cfg)
                hb_full_cfg.setdefault("text", hb_text)
                hb_full_cfg.setdefault("animation", "static")
                hb_full_cfg.setdefault("font", "Montserrat SemiBold")
                hb_full_cfg.setdefault("size", 220)
                hb_full_cfg.setdefault("x", play_w // 2)
                hb_full_cfg.setdefault("y", int(play_h * 0.38))
                hb_full_cfg.setdefault("alignment", 5)
                hb_full_cfg.setdefault("color", "&H00FFFFFF&")
                hb_full_cfg.setdefault("outline", 10)
                hb_full_cfg.setdefault("outline_color", "&H00000000&")
                hb_full_cfg.setdefault("shadow", 0)

                hb_ass = _make_name_overlay_ass(hb_full_cfg, play_w, play_h)
                with open(TMP_HB_ASS, "w", encoding="utf-8") as f:
                    f.write(hb_ass)

                if normal_text_start > 0:
                    shift_ass_dialogue_times(TMP_HB_ASS, TMP_HB_ASS, normal_text_start)

                hb_filter = f"subtitles={TMP_HB_ASS}"
                if fontsdir:
                    hb_filter += f":fontsdir={fontsdir}"
                vf_filters.append(hb_filter)
                happy_birthday_used = True

        # Name overlay (shift to normal_text_start)
        name_overlay_used = False
        if isinstance(name_cfg, dict) and str(name_cfg.get("text", "")).strip():
            name_ass = _make_name_overlay_ass(name_cfg, play_w, play_h)
            with open(TMP_NAME_ASS, "w", encoding="utf-8") as f:
                f.write(name_ass)

            if normal_text_start > 0:
                shift_ass_dialogue_times(TMP_NAME_ASS, TMP_NAME_ASS, normal_text_start)

            name_filter = f"subtitles={TMP_NAME_ASS}"
            if fontsdir:
                name_filter += f":fontsdir={fontsdir}"
            vf_filters.append(name_filter)
            name_overlay_used = True

        # BEFORE overlay: start at normal_text_start, end at ass_start
        before_subtitles_used = False
        before_subtitles_window = None
        if before_cfg is True:
            before_cfg = {}
        if isinstance(before_cfg, dict) and bool(before_cfg.get("enabled", True)):
            min_seconds = float(before_cfg.get("min_seconds", 0.1))
            start_before = max(0.0, float(normal_text_start))
            end_before = max(start_before, float(ass_start))
            if (end_before - start_before) >= min_seconds:
                before_ass = _make_timed_static_overlay_ass(before_cfg, play_w, play_h, start_before, end_before)
                with open(TMP_BEFORE_ASS, "w", encoding="utf-8") as f:
                    f.write(before_ass)

                bf_filter = f"subtitles={TMP_BEFORE_ASS}"
                if fontsdir:
                    bf_filter += f":fontsdir={fontsdir}"
                vf_filters.append(bf_filter)

                before_subtitles_used = True
                before_subtitles_window = {"start": start_before, "end": end_before, "min_seconds": min_seconds}

        # AFTER overlay: start at ass_end, end at duration_cap
        after_subtitles_used = False
        after_subtitles_window = None
        if after_cfg is True:
            after_cfg = {}
        if isinstance(after_cfg, dict) and bool(after_cfg.get("enabled", True)) and duration_cap is not None:
            min_seconds = float(after_cfg.get("min_seconds", 2.0))
            start_after = max(0.0, float(ass_end))
            end_after = float(duration_cap)
            if (end_after - start_after) >= min_seconds:
                after_ass = _make_timed_static_overlay_ass(after_cfg, play_w, play_h, start_after, end_after)
                with open(TMP_AFTER_ASS, "w", encoding="utf-8") as f:
                    f.write(after_ass)

                af_filter = f"subtitles={TMP_AFTER_ASS}"
                if fontsdir:
                    af_filter += f":fontsdir={fontsdir}"
                vf_filters.append(af_filter)

                after_subtitles_used = True
                after_subtitles_window = {"start": start_after, "end": end_after, "min_seconds": min_seconds}

        vf = ",".join(vf_filters)

        # -----------------------------
        # FILTER_COMPLEX: video + audio
        # -----------------------------
        fc = []
        fc.append(f"[0:v]{vf}[vout]")

        # IMPORTANT: To keep subtitles perfect, we ONLY trim start with atrim(lead_trim),
        # and only trim end with silenceremove stop_periods.
        def build_song_chain(in_label: str, out_label: str) -> str:
            parts = []

            # END-only silence trim (does NOT touch the beginning)
            if trim_enabled:
                parts.append(
                    "silenceremove="
                    f"stop_periods=1:stop_duration={end_silence}:stop_threshold={threshold_db}dB"
                )

            # START trim exactly once, using detected lead_trim
            if trim_enabled and lead_trim > 0.0:
                parts.append(f"atrim=start={lead_trim:.3f}")
                parts.append("asetpts=PTS-STARTPTS")

            if a_volume is not None:
                parts.append(f"volume={float(a_volume)}")

            parts.append("aresample=async=1")
            return f"{in_label}{','.join(parts)}{out_label}"

        if not intro_enabled:
            # No intro, just song
            fc.append(build_song_chain("[1:a]", "[basea]"))
            current_audio_label = "[basea]"
        else:
            intro_bgm_vol = float(intro_cfg.get("bgm_volume", 1.0))
            cd_vol = float(intro_cfg.get("countdown_volume", 1.0))

            if end_hb_voice_enabled:
                fc.append("[4:a]asplit=2[hbvo_src_intro][hbvo_src_end]")
                hb_intro_src = "[hbvo_src_intro]"
                hb_end_src = "[hbvo_src_end]"
            else:
                hb_intro_src = "[4:a]"
                hb_end_src = None

            # Intro components
            fc.append(f"[2:a]volume={intro_bgm_vol},atrim=0:{intro_len:.3f},asetpts=PTS-STARTPTS[introb]")
            fc.append(f"[3:a]volume={cd_vol},atrim=0:{countdown_seconds:.3f},asetpts=PTS-STARTPTS[countvo]")

            delay_ms = int(round(hb_voice_delay_seconds * 1000))
            fc.append(
                f"{hb_intro_src}volume={hb_vol_intro},atrim=0:{hb_voice_dur:.3f},"
                f"asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}[hbvo_intro]"
            )

            fc.append("[introb][countvo][hbvo_intro]amix=inputs=3:normalize=0:duration=longest[intromix]")
            fc.append(f"[intromix]atrim=0:{intro_len:.3f},asetpts=PTS-STARTPTS[intro]")

            # Song
            fc.append(build_song_chain("[1:a]", "[song]"))

            # Base audio = intro + song
            fc.append("[intro][song]concat=n=2:v=0:a=1[basea]")
            current_audio_label = "[basea]"

        # NEW: add same HB voice at song end
        # Starts exactly at the last sung word end time (ass_end), even if music continues or not.
        song_end_hb_added = False
        if end_hb_voice_enabled and song_end_hb_start is not None and hb_voice_dur > 0.0:
            end_delay_ms = int(round(song_end_hb_start * 1000))

            if intro_enabled and hb_end_src is not None:
                fc.append(
                    f"{hb_end_src}volume={hb_vol_song_end},atrim=0:{hb_voice_dur:.3f},"
                    f"asetpts=PTS-STARTPTS,adelay={end_delay_ms}|{end_delay_ms}[hbvo_end]"
                )
            else:
                # Fallback path; should normally not happen because end_hb_voice_enabled requires intro_enabled
                fc.append(
                    f"[4:a]volume={hb_vol_song_end},atrim=0:{hb_voice_dur:.3f},"
                    f"asetpts=PTS-STARTPTS,adelay={end_delay_ms}|{end_delay_ms}[hbvo_end]"
                )

            fc.append(f"{current_audio_label}[hbvo_end]amix=inputs=2:normalize=0:duration=longest[aout]")
            song_end_hb_added = True
        else:
            fc.append(f"{current_audio_label}anull[aout]")

        filter_complex = ";".join(fc)

        # -----------------------------
        # ffmpeg command
        # -----------------------------
        cmd = ["ffmpeg", "-y"]

        # video (loop starts immediately)
        if loop_video:
            cmd += ["-stream_loop", "-1", "-i", TMP_IN]
        else:
            cmd += ["-i", TMP_IN]

        # audio inputs
        cmd += ["-i", TMP_MUSIC]
        if intro_enabled:
            cmd += ["-i", TMP_INTRO_BGM, "-i", TMP_COUNTDOWN_VO, "-i", TMP_HB_VO]

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
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

        if duration_cap is not None:
            cmd += ["-t", f"{duration_cap:.3f}"]

        cmd.append(TMP_OUT)

        last_render_error = None
        last_audio_check = None
        p = None

        for render_attempt in range(2):
            p = subprocess.run(cmd, capture_output=True, text=True)

            if p.returncode != 0:
                last_render_error = {
                    "error": "ffmpeg failed",
                    "returncode": p.returncode,
                    "stderr": p.stderr[-20000:],
                    "stdout": p.stdout[-20000:],
                    "cmd": cmd,
                    "filter_complex": filter_complex,
                    "render_attempt": render_attempt + 1,
                }
                continue

            audio_windows = [
                (2.0, 8.0),
                (16.0, 22.0),
                (26.0, 32.0),
            ]

            audio_ok = True
            audio_checks = []

            for start_offset, end_offset in audio_windows:
                start_s = (intro_len + start_offset) if intro_enabled else start_offset
                duration = end_offset - start_offset

                ok, audio_check = validate_output_audio_after_intro(
                    TMP_OUT,
                    start_s=start_s,
                    check_duration_s=duration,
                    silence_threshold_db=float(audio_cfg.get("post_audio_silence_threshold_db", -55.0)),
                )

                audio_checks.append({
                    "window_relative_to_song": [start_offset, end_offset],
                    "checked_from_seconds": start_s,
                    "checked_duration_seconds": duration,
                    "ok": ok,
                    "result": audio_check,
                })

                if not ok:
                    audio_ok = False

            last_audio_check = {
                "all_windows_ok": audio_ok,
                "windows": audio_checks,
            }

            if audio_ok:
                break

            last_render_error = {
                "error": "output audio validation failed",
                "audio_check": last_audio_check,
                "cmd": cmd,
                "filter_complex": filter_complex,
                "ffmpeg_stderr_tail": p.stderr[-20000:],
                "render_attempt": render_attempt + 1,
            }
        else:
            return {
                **(last_render_error or {"error": "render failed"}),
                "retried": True,
                "max_attempts": 2,
            }

        uploaded = upload_to_r2(TMP_OUT, out_key)

        # Thumbnail (optional)
        thumb_result = None
        thumb_cmd = None
        try:
            thumb_result, thumb_cmd = _render_thumbnail(
                thumb_cfg=thumb_cfg,
                job_id=job_id,
                name_cfg=name_cfg,
                fontsdir=fontsdir,
            )
        except ValueError as ve:
            if isinstance(thumb_cfg, dict) and bool(thumb_cfg.get("enabled", False)):
                return {"error": str(ve)}
        except RuntimeError as re_err:
            if isinstance(thumb_cfg, dict) and bool(thumb_cfg.get("enabled", False)):
                return {"error": "thumbnail ffmpeg failed", "details": str(re_err)}

        return {
            "status": "ok",
            "mode": "render",
            "jobId": job_id,
            "out_key": out_key,
            "uploaded": uploaded,
            "thumbnail_uploaded": thumb_result,
            "thumbnail_ffmpeg_cmd": thumb_cmd,
            "fontsdir_used": fontsdir,
            "intro_enabled": intro_enabled,
            "intro_seconds_min": intro_len_min if intro_enabled else 0.0,
            "intro_seconds_effective": intro_len if intro_enabled else 0.0,
            "hb_voice_seconds": hb_voice_dur if intro_enabled else 0.0,
            "hb_voice_delay_seconds": hb_voice_delay_seconds if intro_enabled else 0.0,
            "countdown_seconds": countdown_seconds if intro_enabled else 0.0,
            "normal_text_start_seconds": normal_text_start,
            "trim_enabled": trim_enabled,
            "trimmed_leading_seconds": lead_trim,
            "subtitle_nudge_seconds": subtitle_nudge,
            "effective_ass_shift_seconds": effective_ass_shift,
            "happy_birthday_used": happy_birthday_used,
            "name_overlay_used": name_overlay_used,
            "before_subtitles_used": before_subtitles_used,
            "after_subtitles_used": after_subtitles_used,
            "before_subtitles_window": before_subtitles_window,
            "after_subtitles_window": after_subtitles_window,
            "song_end_hb_voice_enabled": end_hb_voice_enabled,
            "song_end_hb_voice_added": song_end_hb_added,
            "song_end_hb_voice_start_seconds": song_end_hb_start,
            "song_end_hb_voice_end_seconds": song_end_hb_end,
            "base_audio_end_est_seconds": base_audio_end_est,
            "duration_cap_seconds": duration_cap,
            "audio_validation": last_audio_check,
            "render_attempts_used": render_attempt + 1,
            "audio_validation_retried": render_attempt > 0,
            "ffmpeg_cmd": cmd,
            "filter_complex": filter_complex,
            "ffmpeg_stderr_tail": p.stderr[-20000:],
        }

    except Exception as e:
        return {"error": "handler exception", "details": str(e)}


runpod.serverless.start({"handler": handler})
