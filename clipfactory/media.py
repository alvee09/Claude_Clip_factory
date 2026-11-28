"""ffmpeg work: probing, finding highlight moments, and rendering clips.

Highlights are found without AI, which suits gameplay / sports / action footage:
  * loudness  - crowd noise, explosions, commentary hype
  * sudden rises in loudness - the moment something happens
  * scene-cut density - fast action, replays, camera switches
"""
import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np


def _find(name):
    for cand in (os.environ.get(name.upper()), shutil.which(name),
                 f"/usr/local/bin/{name}", f"/opt/homebrew/bin/{name}"):
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError(f"{name} not found. Install it with: brew install ffmpeg")


def ffmpeg():
    return _find("ffmpeg")


def ffprobe():
    return _find("ffprobe")


SAMPLE_RATE = 4000  # plenty for a loudness curve


def probe(path):
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"Can't read video: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(info.get("format", {}).get("duration")
                     or (video or {}).get("duration") or 0)
    return {
        "duration": duration,
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "has_video": video is not None,
        "has_audio": audio is not None,
        # PS5 / iPhone HDR: PQ or HLG brightness curve
        "hdr": (video or {}).get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "fps": _rate((video or {}).get("avg_frame_rate")) or _rate((video or {}).get("r_frame_rate")),
    }


def _rate(text):
    try:
        num, den = (text or "").split("/")
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


# Instagram rejects odd or variable frame rates, so clips go out at the nearest standard one.
STANDARD_FPS = [(24000 / 1001, "24000/1001"), (24, "24"), (25, "25"), (30000 / 1001, "30000/1001"),
                (30, "30"), (50, "50"), (60000 / 1001, "60000/1001"), (60, "60")]


def output_fps(fps):
    if not fps:
        return "30"
    return min(STANDARD_FPS, key=lambda r: abs(r[0] - min(fps, 60)))[1]


# HDR footage turned into normal (SDR, BT.709) video. Without this, clips look washed out and
# Instagram refuses them (an 8-bit file can't carry HDR).
TO_SDR = "scale=out_color_matrix=bt709:out_primaries=bt709:out_transfer=bt709:out_range=tv"


@functools.lru_cache(maxsize=64)
def sdr_prefix(path):
    """Filter to put first in a -vf chain for this source ('' when it's already SDR)."""
    try:
        return TO_SDR + "," if probe(path)["hdr"] else ""
    except RuntimeError:
        return ""


def _run_with_progress(cmd, duration, on_progress):
    """Run ffmpeg with -progress and report 0..1 through on_progress."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                done = int(line.split("=", 1)[1]) / 1e6
                on_progress(min(done / duration, 1.0))
            except ValueError:
                pass
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(err.strip()[-500:] or "ffmpeg failed")


def _robust_z(x):
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    if mad < 1e-6:
        mad = x.std() + 1e-6
    return (x - med) / mad


def _smooth(x, w):
    if w <= 1 or len(x) < w:
        return x
    return np.convolve(x, np.ones(w) / w, mode="same")


def intensity_curve(path, info, on_progress=lambda p: None, fast=True):
    """One decode pass -> per-second 'how exciting is this' score.

    Fast mode decodes only the video's keyframes for the scene part; the sound is still
    read in full. Keyframe-to-keyframe change is a good stand-in for fast action, and on
    a 13-minute 1080p60 test it picked the same highlights ~80x faster (9 s vs 694 s).
    """
    seconds = int(info["duration"])
    if seconds < 1:
        return np.zeros(0)
    tmp = Path(tempfile.mkdtemp(prefix="clipfactory-"))
    audio_raw, scenes_txt = tmp / "audio.raw", tmp / "scenes.txt"
    try:
        cmd = [ffmpeg(), "-v", "error", "-nostdin", "-hwaccel", "auto",
               *(["-skip_frame:v", "nokey"] if fast else []), "-i", str(path)]
        if info["has_audio"]:
            cmd += ["-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
                    "-f", "s16le", "-y", str(audio_raw)]
        if info["has_video"]:
            # scdet (unlike select) passes every frame on, so ffmpeg keeps reporting progress
            cmd += ["-map", "0:v:0", "-an", "-vf",
                    f"fps=4,scale=160:-2,scdet=threshold=15,"
                    f"metadata=print:key=lavfi.scd.time:file={scenes_txt}",
                    "-f", "null", "-"]
        _run_with_progress(cmd, info["duration"], on_progress)

        score = np.zeros(seconds)

        if audio_raw.exists() and audio_raw.stat().st_size >= SAMPLE_RATE * 2:
            samples = np.memmap(audio_raw, dtype="<i2", mode="r")
            n = min(len(samples) // SAMPLE_RATE, seconds)
            rms = np.empty(n)
            for i in range(0, n, 600):  # chunked to keep memory flat on long videos
                block = samples[i * SAMPLE_RATE:min(i + 600, n) * SAMPLE_RATE]
                block = block.astype(np.float32).reshape(-1, SAMPLE_RATE) / 32768.0
                rms[i:i + len(block)] = np.sqrt((block ** 2).mean(axis=1))
            del samples
            db = 20 * np.log10(rms + 1e-5)
            # how loud vs the rest of this video
            loud = np.clip(_robust_z(db), -3, 6)
            # how much louder than the previous 5 seconds
            csum = np.concatenate([[0], np.cumsum(db)])
            idx = np.arange(n)
            lo = np.maximum(idx - 5, 0)
            prev_mean = np.where(idx > 0, (csum[idx] - csum[lo]) / np.maximum(idx - lo, 1), db)
            rise = np.clip(_robust_z(db - prev_mean), -3, 6)
            score[:n] += _smooth(0.6 * loud + 0.4 * rise, 3)

        if scenes_txt.exists():
            cuts = np.zeros(seconds)
            for line in scenes_txt.read_text(errors="ignore").splitlines():
                if "pts_time:" in line:
                    try:
                        t = int(float(line.split("pts_time:")[1].split()[0]))
                        if 0 <= t < seconds:
                            cuts[t] += 1
                    except (ValueError, IndexError):
                        pass
            if cuts.any():
                density = _smooth(cuts, 6)
                scene_z = np.clip((density - density.mean()) / (density.std() + 1e-6), -2, 5)
                score += (0.3 if info["has_audio"] else 1.0) * scene_z
        return score
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick_moments(score, duration, settings):
    """Return [(start, length, score)] for the best non-overlapping moments."""
    length = float(settings["clip_length"])
    if duration <= length + 2:
        return [(0.0, max(duration - 0.1, 1.0), 0.0)] if duration >= 5 else []
    max_n = max(1, min(int(settings["max_clips_per_video"]),
                       int(duration // (60 * float(settings["minutes_per_clip"]))) or 1))
    picks = []
    if len(score):
        for t in np.argsort(score)[::-1]:
            if len(picks) >= max_n or (picks and score[t] < settings["min_score"]):
                break
            if all(abs(int(t) - p) >= length for p in picks):
                picks.append(int(t))
    if not picks:  # flat / silent footage: take the middle
        picks = [int(duration / 2)]
    moments = []
    for t in sorted(picks):
        # the peak lands ~2/3 into the clip: build-up, then the payoff
        start = min(max(0.0, t - 0.65 * length), duration - length)
        moments.append((round(start, 2), length, float(score[t]) if len(score) else 0.0))
    return moments


# Shapes for the social version of a clip, as output width x height.
ASPECTS = {"9:16": (1080, 1920),   # Reels, Shorts, TikTok
           "4:5": (1080, 1350),    # Instagram feed post
           "1:1": (1080, 1080)}    # square


def render(src, start, length, out, kind, settings, vertical_mode=None, crop_x=0.5,
           aspect=None, on_progress=lambda p: None):
    """kind: 'vertical' (the social version, shaped by aspect: 9:16, 4:5, 1:1 or 'original') or 'original'.

    vertical_mode: 'blur' (whole frame on a blurred background) or 'crop' (zoom to
    fill the screen; crop_x picks which part stays: 0 = left, 0.5 = center, 1 = right).
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "vertical" and (aspect or settings.get("aspect")) == "original":
        vf = "scale='min(1920,iw)':-2,setsar=1"  # the social version keeps the video's own shape
    elif kind == "vertical":
        w, h = ASPECTS.get(aspect or settings.get("aspect"), ASPECTS["9:16"])
        if (vertical_mode or settings["vertical_mode"]) == "crop":
            x = min(max(float(crop_x if crop_x is not None else 0.5), 0.0), 1.0)
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                  f"crop={w}:{h}:(iw-ow)*{x:.3f}:(ih-oh)/2,setsar=1")
        else:
            vf = ("split[a][b];"
                  f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
                  f"boxblur=10:2,scale={w}:{h}[bg];"
                  f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                  "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    else:
        vf = "scale='min(1920,iw)':-2,setsar=1"
    info = probe(src)
    vf = (TO_SDR + "," if info["hdr"] else "") + vf + f",scale=out_range=tv,format=yuv420p,fps={output_fps(info['fps'])}"
    tmp_out = out.with_suffix(".part.mp4")
    cmd = [ffmpeg(), "-v", "error", "-nostdin", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, "-fps_mode", "cfr",
           "-c:v", "libx264", "-preset", settings["preset"], "-crf", str(settings["crf"]),
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-color_range", "tv",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(tmp_out)]
    _run_with_progress(cmd, length, on_progress)
    tmp_out.replace(out)
    return out


def thumbnail(clip_path, at, out):
    subprocess.run([ffmpeg(), "-v", "error", "-nostdin", "-y", "-ss", f"{at:.2f}",
                    "-i", str(clip_path), "-frames:v", "1", "-vf", "scale=360:-2",
                    "-q:v", "4", str(out)], check=True, capture_output=True)
    return out
