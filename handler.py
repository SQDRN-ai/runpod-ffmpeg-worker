import os
import subprocess
import uuid
import zipfile
import runpod
import re

TMP_IN = "/tmp/in.mp4"
TMP_ASS = "/tmp/subtitles.ass"
TMP_SHIFTED_ASS = "/tmp/subtitles_shifted.ass"

TMP_MUSIC = "/tmp/music.mp3"

TMP_INTRO_BGM = "/tmp/intro_bgm.mp3"
TMP_COUNTDOWN_VO = "/tmp/countdown_vo.mp3"
TMP_HB_VO = "/tmp/hb_voice.mp3"

TMP_NAME_ASS = "/tmp/name_overlay.ass"
TMP_HB_ASS = "/tmp/happy_birthday_overlay.ass"
TMP_AFTER_ASS = "/tmp/after_subtitles_overlay.ass"
TMP_BEFORE_ASS = "/tmp/before_subtitles_overlay.ass"
TMP_COUNTDOWN_ASS = "/tmp/countdown_overlay.ass"

TMP_FONTS_ZIP = "/tmp/fonts.zip"
TMP_OUT = "/tmp/final.mp4"

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
# Time helpers
# -----------------------------
def ass_time_to_seconds(t: str) -> float:
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


def shift_ass_dialogue_times(in_path: str, out_path: str, offset_s: float):
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
# Detect leading silence (ONLY used to shift subtitles correctly)
# -----------------------------
def detect_leading_silence_seconds(path: str, threshold_db: float, min_silence: float, max_trim: float) -> float:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"silencedetect=n={threshold_db}dB:d={min_silence}",
        "-f", "null", "-"
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    log = (p.stderr or "") + "\n" + (p.stdout or "")

    # Only trim if it truly starts at 0
    if not re.search(r"silence_start:\s*0(\.0+)?", log):
        return 0.0

    m_end = re.search(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", log)
    if not m_end:
        return 0.0

    try:
        t = float(m_end.group(1))
        if t < 0:
            return 0.0
        return min(t, float(max_trim))
    except Exception:
        return 0.0


# -----------------------------
# ASS overlay builders (same as CODE 1 but shortened here for clarity)
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

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

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
    labels = [str(x) for x in labels] if isinstance(labels, list) else ["3", "2", "1"]

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

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def dlg(layer: int, s0: float, s1: float, txt: str) -> str:
        st = seconds_to_ass_time(s0)
        et = seconds_to_ass_time(s1)
        tags = f"{{\\an{alignment}\\pos({x},{y})\\fad({fade_in_ms},{fade_out_ms})}}"
        return f"Dialogue: {layer},{st},{et},COUNT,,0000,0000,0000,,{tags}{txt}\n"

    layer = int(cfg.get("layer", 50))
    out = []
    for i, lab in enumerate(labels[:3]):
        s0 = start0 + i * step
        out.append(dlg(layer, s0, s0 + dur, lab))

    return header + styles + events_header + "".join(out)


def _get_canvas(render: dict):
    canvas = render.get("canvas", {}) or {}
    w = int(canvas.get("width", 3840))
    h = int(canvas.get("height", 2160))
    if w <= 0 or h <= 0:
        raise ValueError("render.canvas.width/height must be positive integers")
    return w, h


def handler(event):
    try:
        inp = (event or {}).get("input", {}) or {}
        mode = inp.get("mode", "render")

        if mode != "render":
            return {"error": "This trimmed-audio version is for mode='render' only."}

        job_id = inp.get("jobId")
        render = inp.get("render", {}) or {}

        subs_cfg = render.get("subtitles", {}) or {}
        after_cfg = render.get("after_subtitles_overlay", None)
        before_cfg = render.get("before_subtitles_overlay", None)
        countdown_cfg = render.get("countdown_overlay", {}) or {}

        intro_cfg = render.get("intro", {}) or {}
        intro_enabled = bool(intro_cfg.get("enabled", False))
        intro_len = float(intro_cfg.get("length_seconds", 5.0))
        normal_text_start = float(intro_cfg.get("normal_text_start_seconds", 2.0))

        video_key = inp.get("video_key")
        ass_key = inp.get("ass_key")
        music_key = inp.get("music_key")
        out_key = inp.get("out_key") or (f"posts/{job_id}/final.mp4" if job_id else f"outputs/{uuid.uuid4().hex}.mp4")

        if not video_key or not ass_key or not music_key:
            return {"error": "Missing required inputs.", "required": ["video_key", "ass_key", "music_key"]}

        play_w, play_h = _get_canvas(render)

        video_cfg = render.get("video", {}) or {}
        audio_cfg = render.get("audio", {}) or {}
        timing_cfg = render.get("timing", {}) or {}

        v_scale = video_cfg.get("scale", None)
        v_codec = video_cfg.get("codec", "libx264")
        v_preset = video_cfg.get("preset", "medium")
        v_crf = str(video_cfg.get("crf", 18))
        v_pix_fmt = video_cfg.get("pix_fmt", "yuv420p")
        v_profile = video_cfg.get("profile", "high")
        v_tune = video_cfg.get("tune", None)
        faststart = bool(video_cfg.get("movflags_faststart", True))

        a_codec = audio_cfg.get("codec", "aac")
        a_bitrate = audio_cfg.get("bitrate", "192k")
        a_volume = audio_cfg.get("volume", None)

        # ✅ Trim config
        trim_cfg = audio_cfg.get("trim_silence", {}) or {}
        trim_enabled = bool(trim_cfg.get("enabled", True))
        threshold_db = float(trim_cfg.get("threshold_db", -45.0))
        min_silence = float(trim_cfg.get("min_silence_seconds", 0.08))
        max_trim = float(trim_cfg.get("max_leading_trim_seconds", 2.0))
        # end trimming settings (still edge-only)
        end_silence = float(trim_cfg.get("end_silence_seconds", 0.20))

        loop_video = bool(timing_cfg.get("loop_video", False))

        # Download
        download_from_r2(video_key, TMP_IN)
        download_from_r2(ass_key, TMP_ASS)
        download_from_r2(music_key, TMP_MUSIC)

        if intro_enabled:
            missing = [k for k in ["bgm_key", "countdown_key", "hb_voice_key"] if not intro_cfg.get(k)]
            if missing:
                return {"error": "intro.enabled is true but missing intro keys", "missing": missing}
            download_from_r2(intro_cfg["bgm_key"], TMP_INTRO_BGM)
            download_from_r2(intro_cfg["countdown_key"], TMP_COUNTDOWN_VO)
            download_from_r2(intro_cfg["hb_voice_key"], TMP_HB_VO)

        # ✅ Detect ONLY leading silence (so we can shift subtitles)
        lead_trim = 0.0
        if trim_enabled:
            lead_trim = detect_leading_silence_seconds(
                TMP_MUSIC,
                threshold_db=threshold_db,
                min_silence=min_silence,
                max_trim=max_trim,
            )

        # ✅ Shift karaoke ASS by (intro_len - lead_trim)
        effective_ass_shift = (intro_len - lead_trim) if intro_enabled else (-lead_trim)
        ass_path_for_render = TMP_ASS
        if abs(effective_ass_shift) > 1e-6:
            shift_ass_dialogue_times(TMP_ASS, TMP_SHIFTED_ASS, effective_ass_shift)
            ass_path_for_render = TMP_SHIFTED_ASS

        # timings from the shifted ASS
        ass_end = get_ass_end_seconds(ass_path_for_render)
        ass_start = get_ass_start_seconds(ass_path_for_render)

        # duration cap (used for after overlay)
        audio_end = max(0.0, get_media_duration_seconds(TMP_MUSIC) - lead_trim)
        pad = float(render.get("end_pad_seconds", 0.3))
        duration_cap = max(ass_end, (audio_end + (intro_len if intro_enabled else 0.0))) + pad if (ass_end > 0 or audio_end > 0) else None

        # VIDEO filters (simplified)
        vf_filters = []
        if v_scale:
            vf_filters.append(f"scale={v_scale}")

        subs_filter = f"subtitles={ass_path_for_render}"
        force_style = subs_cfg.get("force_style", None)
        if isinstance(force_style, dict) and force_style:
            fs = _build_force_style(force_style)
            subs_filter += f":force_style='{_escape_for_subtitles_filter(fs)}'"
        vf_filters.append(subs_filter)

        # Countdown overlay
        if intro_enabled:
            countdown_ass = _make_countdown_overlay_ass(countdown_cfg, play_w, play_h)
            with open(TMP_COUNTDOWN_ASS, "w", encoding="utf-8") as f:
                f.write(countdown_ass)
            vf_filters.append(f"subtitles={TMP_COUNTDOWN_ASS}")

        # BEFORE overlay: start at normal_text_start, end at ass_start
        if isinstance(before_cfg, dict) and before_cfg.get("enabled", True):
            start_before = max(0.0, float(normal_text_start))
            end_before = max(start_before, float(ass_start))
            before_ass = _make_timed_static_overlay_ass(before_cfg, play_w, play_h, start_before, end_before)
            with open(TMP_BEFORE_ASS, "w", encoding="utf-8") as f:
                f.write(before_ass)
            vf_filters.append(f"subtitles={TMP_BEFORE_ASS}")

        # AFTER overlay
        if isinstance(after_cfg, dict) and after_cfg.get("enabled", True) and duration_cap is not None:
            start_after = max(0.0, float(ass_end))
            end_after = float(duration_cap)
            after_ass = _make_timed_static_overlay_ass(after_cfg, play_w, play_h, start_after, end_after)
            with open(TMP_AFTER_ASS, "w", encoding="utf-8") as f:
                f.write(after_ass)
            vf_filters.append(f"subtitles={TMP_AFTER_ASS}")

        vf = ",".join(vf_filters)

        # -----------------------------
        # FILTER_COMPLEX: trim only START+END silence on the SONG
        # This does NOT remove internal silent drops.
        # -----------------------------
        fc = []
        fc.append(f"[0:v]{vf}[vout]")

        def build_song_chain(in_label: str, out_label: str) -> str:
            filters = []
            if trim_enabled:
                # START+END trimming only:
                # start_periods=1 trims beginning
                # stop_periods=1 trims end
                filters.append(
                    "silenceremove="
                    f"start_periods=1:start_duration={min_silence}:start_threshold={threshold_db}dB:"
                    f"stop_periods=1:stop_duration={end_silence}:stop_threshold={threshold_db}dB"
                )
            if lead_trim > 0.0:
                # still trim exact leading amount we detected (keeps subtitles perfect)
                filters.append(f"atrim=start={lead_trim:.3f}")
                filters.append("asetpts=PTS-STARTPTS")
            if a_volume is not None:
                filters.append(f"volume={float(a_volume)}")
            filters.append("aresample=async=1")
            return f"{in_label}{','.join(filters)}{out_label}"

        if not intro_enabled:
            fc.append(build_song_chain("[1:a]", "[aout]"))
        else:
            intro_bgm_vol = float(intro_cfg.get("bgm_volume", 1.0))
            cd_vol = float(intro_cfg.get("countdown_volume", 1.0))
            hb_vol = float(intro_cfg.get("hb_voice_volume", 1.0))

            fc.append(f"[2:a]volume={intro_bgm_vol},atrim=0:{intro_len},asetpts=PTS-STARTPTS[introb]")
            fc.append(f"[3:a]volume={cd_vol},atrim=0:2.0,asetpts=PTS-STARTPTS[countvo]")
            fc.append(f"[4:a]volume={hb_vol},atrim=0:3.0,asetpts=PTS-STARTPTS,adelay=2000|2000[hbvo]")
            fc.append("[introb][countvo][hbvo]amix=inputs=3:normalize=0:duration=longest[intromix]")
            fc.append(f"[intromix]atrim=0:{intro_len},asetpts=PTS-STARTPTS[intro]")

            fc.append(build_song_chain("[1:a]", "[song]"))
            fc.append("[intro][song]concat=n=2:v=0:a=1[aout]")

        filter_complex = ";".join(fc)

        cmd = ["ffmpeg", "-y"]
        if loop_video:
            cmd += ["-stream_loop", "-1", "-i", TMP_IN]
        else:
            cmd += ["-i", TMP_IN]

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
            "mode": "render",
            "jobId": job_id,
            "uploaded": uploaded,
            "out_key": out_key,
            "trim_enabled": trim_enabled,
            "trimmed_leading_seconds": lead_trim,
            "effective_ass_shift_seconds": effective_ass_shift,
            "ffmpeg_cmd": cmd,
        }

    except Exception as e:
        return {"error": "handler exception", "details": str(e)}


runpod.serverless.start({"handler": handler})
