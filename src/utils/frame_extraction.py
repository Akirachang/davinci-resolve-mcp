"""Source-media frame extraction via ffmpeg.

This module wraps ffmpeg for fast extraction of small thumbnails from a clip's
SOURCE media (the original file on disk, ungraded). Used by the `frames`
compound tool to give vision-capable models a way to actually see the footage
they are editing — not just clip names and durations.

For graded timeline output (color page, render-resolution), use Resolve's
`Project.ExportCurrentFrameAsStill` path instead — handled in src/server.py
via the `frames(action="extract_from_timeline")` action.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple


def check_ffmpeg() -> Dict[str, Any]:
    """Return ffmpeg availability and version, or a clear install hint."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {
            "available": False,
            "error": (
                "ffmpeg not found on PATH. Install via Homebrew (`brew install ffmpeg`), "
                "apt (`sudo apt install ffmpeg`), or https://ffmpeg.org/download.html."
            ),
        }
    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "path": ffmpeg, "error": f"ffmpeg failed to run: {exc}"}
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
    match = re.search(r"ffmpeg version (\S+)", first_line)
    return {
        "available": True,
        "path": ffmpeg,
        "version": match.group(1) if match else first_line,
    }


def _scale_filter(max_dimension: Optional[int]) -> Optional[str]:
    """Return an ffmpeg -vf scale expression that fits within max_dimension on the
    longest edge while preserving aspect ratio and even dimensions, or None."""
    if not max_dimension or max_dimension <= 0:
        return None
    md = int(max_dimension)
    return (
        f"scale='if(gt(iw,ih),min({md},iw),-2)':'if(gt(iw,ih),-2,min({md},ih))'"
    )


def _format_to_extension(fmt: str) -> str:
    fmt = (fmt or "jpg").lower().lstrip(".")
    if fmt in ("jpg", "jpeg"):
        return "jpg"
    if fmt == "png":
        return "png"
    if fmt == "webp":
        return "webp"
    raise ValueError(f"Unsupported format '{fmt}'. Use jpg, png, or webp.")


def extract_frame(
    source_path: str,
    timestamp_seconds: float,
    output_path: str,
    max_dimension: Optional[int] = 512,
    quality: int = 3,
    timeout_seconds: float = 30.0,
) -> Tuple[bool, Optional[str]]:
    """Extract a single frame at `timestamp_seconds` from `source_path`.

    Returns (ok, error_message). On success, `output_path` will exist.
    `quality` is ffmpeg's -q:v (1=best, 31=worst); ignored for png.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found on PATH"

    cmd: List[str] = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0.0, float(timestamp_seconds)):.3f}",
        "-i", source_path,
        "-frames:v", "1",
    ]
    scale = _scale_filter(max_dimension)
    if scale:
        cmd.extend(["-vf", scale])
    if not output_path.lower().endswith(".png"):
        cmd.extend(["-q:v", str(int(quality))])
    cmd.extend(["-y", output_path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out extracting frame at {timestamp_seconds:.3f}s"
    except OSError as exc:
        return False, f"ffmpeg failed: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else "ffmpeg returned nonzero exit"
        return False, last
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return False, "ffmpeg reported success but no output file was written"
    return True, None


def evenly_spaced_timestamps(duration_seconds: float, count: int) -> List[float]:
    """Return `count` timestamps evenly spread across the clip, biased away from
    the absolute start/end where black frames are common.
    """
    if count <= 0 or duration_seconds <= 0:
        return []
    if count == 1:
        return [duration_seconds / 2.0]
    step = duration_seconds / (count + 1)
    return [round(step * (i + 1), 3) for i in range(count)]


def normalize_frame_selection(
    duration_seconds: float,
    fps: Optional[float],
    *,
    count: Optional[int] = None,
    timestamps_seconds: Optional[List[float]] = None,
    frame_numbers: Optional[List[int]] = None,
    interval_seconds: Optional[float] = None,
    max_count: int = 32,
) -> Tuple[List[float], Optional[str]]:
    """Resolve any of the four frame-selection modes into a list of timestamps in
    seconds. Returns (timestamps, error_message). Caps result at `max_count`."""
    selected = sum(
        1 for v in (count, timestamps_seconds, frame_numbers, interval_seconds) if v
    )
    if selected > 1:
        return [], (
            "Provide only one of: count, timestamps_seconds, frame_numbers, interval_seconds"
        )

    timestamps: List[float] = []
    if timestamps_seconds is not None:
        try:
            timestamps = [float(t) for t in timestamps_seconds]
        except (TypeError, ValueError):
            return [], "timestamps_seconds must be a list of numbers"
    elif frame_numbers is not None:
        if not fps or fps <= 0:
            return [], "frame_numbers requires a positive fps"
        try:
            timestamps = [float(int(f)) / fps for f in frame_numbers]
        except (TypeError, ValueError):
            return [], "frame_numbers must be a list of integers"
    elif interval_seconds is not None:
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError):
            return [], "interval_seconds must be a number"
        if interval <= 0:
            return [], "interval_seconds must be positive"
        t = interval / 2.0
        while t < duration_seconds and len(timestamps) < max_count:
            timestamps.append(round(t, 3))
            t += interval
    else:
        n = int(count) if count else 8
        timestamps = evenly_spaced_timestamps(duration_seconds, n)

    if not timestamps:
        return [], "Frame selection produced zero frames"
    if len(timestamps) > max_count:
        timestamps = timestamps[:max_count]
    timestamps = [max(0.0, min(t, max(0.0, duration_seconds - 0.001))) for t in timestamps]
    return timestamps, None


def parse_resolve_fps(value: Any) -> Optional[float]:
    """Best-effort parse of Resolve's `FPS` clip property — accepts numbers and
    strings like "23.976" or "29.97 DF"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        v = float(match.group(0))
    except ValueError:
        return None
    return v if v > 0 else None


def parse_resolve_duration_seconds(
    duration: Any, fps: Optional[float], frames: Any = None
) -> Optional[float]:
    """Convert Resolve's `Duration` (TC string) or `Frames` property to seconds.

    Resolve returns Duration as `HH:MM:SS:FF` or `HH:MM:SS;FF`, and Frames as an
    integer count. Prefer Frames + fps; fall back to TC parsing.
    """
    if frames is not None and fps and fps > 0:
        try:
            return float(int(str(frames).strip())) / fps
        except (TypeError, ValueError):
            pass
    if duration is None or not fps or fps <= 0:
        return None
    s = str(duration).strip().replace(";", ":").replace(".", ":")
    parts = s.split(":")
    if len(parts) != 4:
        return None
    try:
        hh, mm, ss, ff = (int(p) for p in parts)
    except ValueError:
        return None
    return hh * 3600 + mm * 60 + ss + ff / fps
