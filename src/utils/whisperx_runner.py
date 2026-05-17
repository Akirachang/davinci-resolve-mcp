"""WhisperX CLI wrapper for accurate, word-aligned subtitle generation.

Resolve's built-in `CreateSubtitlesFromAudio` produces subtitles with loose
word/phrase timing. WhisperX (Whisper + wav2vec2 forced alignment) is the
canonical tool for tight, word-level subtitle timestamps. This module is a thin
subprocess wrapper that mirrors the shape of `frame_extraction.py`: a
`check_whisperx()` probe + `run_whisperx()` invocation + an SRT-path resolver.

No Python dependency on whisperx is added; the user installs the CLI separately
(`pip install whisperx`) and we shell out so missing-binary failures return a
clear install hint rather than an import error.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple


_INSTALL_HINT = (
    "whisperx not found on PATH. Install via `pip install whisperx` "
    "(see https://github.com/m-bain/whisperX) and ensure the `whisperx` "
    "console script is on PATH for the Python invoking this MCP."
)


def check_whisperx() -> Dict[str, Any]:
    """Return whisperx availability and version, or a clear install hint."""
    binary = shutil.which("whisperx")
    if not binary:
        return {"available": False, "error": _INSTALL_HINT}
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "path": binary, "error": f"whisperx failed to run: {exc}"}
    out = (result.stdout or "") + (result.stderr or "")
    first_line = out.splitlines()[0] if out else ""
    match = re.search(r"(?:whisperx[,\s]+version|version)\s+(\S+)", first_line, re.IGNORECASE)
    version = match.group(1) if match else (first_line.strip() or None)
    return {"available": True, "path": binary, "version": version}


def run_whisperx(
    audio_path: str,
    output_dir: str,
    *,
    model: str = "small",
    language: str = "auto",
    compute_type: str = "int8",
    extra_args: Optional[List[str]] = None,
    timeout_seconds: float = 900.0,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Invoke the whisperx CLI on `audio_path`, writing outputs to `output_dir`.

    Returns (ok, error_message, info). `info` includes the resolved command and
    the last stderr line on failure for debugging. WhisperX writes multiple
    files (.srt, .vtt, .json, .tsv) per input; we don't parse any of them here,
    callers use `find_output_srt()` afterwards.
    """
    binary = shutil.which("whisperx")
    if not binary:
        return False, _INSTALL_HINT, {}

    if not os.path.isfile(audio_path):
        return False, f"audio file does not exist: {audio_path}", {}

    os.makedirs(output_dir, exist_ok=True)

    cmd: List[str] = [
        binary,
        audio_path,
        "--model", str(model),
        "--output_dir", output_dir,
        "--output_format", "all",
        "--compute_type", str(compute_type),
        "--print_progress", "False",
    ]
    lang = (language or "auto").strip()
    if lang and lang.lower() != "auto":
        cmd.extend(["--language", lang])
    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    info: Dict[str, Any] = {"command": cmd}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"whisperx timed out after {timeout_seconds:.0f}s", info
    except OSError as exc:
        return False, f"whisperx failed to launch: {exc}", info

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        last = stderr[-1] if stderr else f"whisperx exited {result.returncode}"
        info["stderr_tail"] = last
        return False, last, info

    return True, None, info


def find_output_srt(output_dir: str, audio_path: str) -> Optional[str]:
    """Locate the SRT whisperx wrote for `audio_path` inside `output_dir`.

    WhisperX writes `<audio_stem>.srt` next to the other formats. We prefer the
    exact basename match; otherwise return the first .srt found, or None.
    """
    if not os.path.isdir(output_dir):
        return None
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    preferred = os.path.join(output_dir, stem + ".srt")
    if os.path.isfile(preferred):
        return preferred
    for entry in sorted(os.listdir(output_dir)):
        if entry.lower().endswith(".srt"):
            return os.path.join(output_dir, entry)
    return None


def find_output_json(output_dir: str, audio_path: str) -> Optional[str]:
    """Locate the JSON whisperx wrote, parallel to `find_output_srt`."""
    if not os.path.isdir(output_dir):
        return None
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    preferred = os.path.join(output_dir, stem + ".json")
    if os.path.isfile(preferred):
        return preferred
    for entry in sorted(os.listdir(output_dir)):
        if entry.lower().endswith(".json"):
            return os.path.join(output_dir, entry)
    return None


def downmix_to_whisper_wav(
    src_audio: str,
    dst_audio: str,
    *,
    timeout_seconds: float = 120.0,
) -> Tuple[bool, Optional[str]]:
    """Convert any audio file to mono 16kHz PCM (whisper's canonical input).

    Uses ffmpeg if available on PATH; returns (False, hint) if not. Whisperx
    accepts other rates/channels, but mono-16k cuts memory + speeds inference.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found on PATH; cannot downmix to mono 16kHz"
    if not os.path.isfile(src_audio):
        return False, f"source audio does not exist: {src_audio}"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", src_audio,
        "-ac", "1", "-ar", "16000",
        "-y", dst_audio,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg downmix timed out after {timeout_seconds:.0f}s"
    except OSError as exc:
        return False, f"ffmpeg downmix failed: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return False, tail[-1] if tail else "ffmpeg downmix returned nonzero exit"
    if not os.path.isfile(dst_audio) or os.path.getsize(dst_audio) == 0:
        return False, "ffmpeg downmix reported success but produced no output"
    return True, None


def count_srt_cues(srt_path: str) -> int:
    """Best-effort count of SRT cue blocks (lines containing `-->`)."""
    try:
        with open(srt_path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if "-->" in line)
    except OSError:
        return 0
