#!/usr/bin/env python3
"""Live validation for the v2.8.0 `subtitles` compound tool.

Creates a disposable Resolve project, generates a synthetic speech-ish WAV
(macOS `say` if available, otherwise an ffmpeg-generated sine + skip the
align-quality assertions), imports it onto a fresh timeline, and exercises
every action of the `subtitles` tool:

  - check_engine     -- diagnostic for whisperx
  - render_audio     -- timeline-audio export to WAV via the standard render queue
  - align            -- whisperx invocation on the rendered audio
  - import_srt       -- best-effort MediaPool.ImportMedia probe
  - generate         -- end-to-end pipeline

Each call's outputs are inspected for non-empty results and (where possible)
sane envelope keys. NEVER touches user media; only synthetic audio created
inside the temp dir under this run.

Run:
  python3.11 tests/live_whisperx_validation.py
  python3.11 tests/live_whisperx_validation.py --keep-open

Requires `whisperx` on PATH; the harness will SKIP cleanly without it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path


def _install_mcp_stubs() -> None:
    """Allow importing src.server when MCP deps are absent from the host Python."""

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def decorate(func):
                return func
            return decorate

        def resource(self, *args, **kwargs):
            def decorate(func):
                return func
            return decorate

    class _Image:
        def __init__(self, path=None, data=None, format=None):
            self.path = path
            self.data = data
            self.format = format

    def stdio_server(*args, **kwargs):
        raise RuntimeError("stdio_server is not used by this harness")

    anyio = types.ModuleType("anyio")
    anyio.run = lambda func: func()

    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    stdio = types.ModuleType("mcp.server.stdio")

    fastmcp.FastMCP = FastMCP
    fastmcp.Image = _Image
    stdio.stdio_server = stdio_server

    sys.modules.setdefault("anyio", anyio)
    sys.modules.setdefault("mcp", mcp)
    sys.modules.setdefault("mcp.server", server)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp)
    sys.modules.setdefault("mcp.server.stdio", stdio)


def _make_synthetic_speech_wav(work_dir: Path, name: str = "speech.wav") -> Path:
    """Produce a short speech-like WAV. Prefer macOS `say` (real TTS) so whisperx
    has actual phonemes to align; fall back to an ffmpeg sine tone (whisperx will
    likely transcribe nothing meaningful but the pipeline still runs)."""
    out = work_dir / name
    if shutil.which("say"):
        # `say` writes AIFF natively; convert with ffmpeg if available.
        aiff = work_dir / "speech.aiff"
        subprocess.run(
            ["say", "-o", str(aiff), "--data-format=LEI16@22050",
             "The quick brown fox jumps over the lazy dog."],
            check=True,
        )
        if shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-i", str(aiff), "-ac", "1", "-ar", "16000", "-y", str(out)],
                check=True,
            )
            aiff.unlink(missing_ok=True)
            return out
        return aiff
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi",
             "-i", "sine=frequency=220:duration=3",
             "-ac", "1", "-ar", "16000",
             "-y", str(out)],
            check=True,
        )
        return out
    raise RuntimeError("Need either macOS `say` or ffmpeg to generate synthetic audio.")


def run_validation(server, keep_open: bool = False) -> int:
    engine = server.subtitles("check_engine")
    if not engine.get("available"):
        print(f"SKIP: whisperx not available: {engine.get('error')}", file=sys.stderr)
        return 2
    print(f"whisperx detected: {engine.get('version')} at {engine.get('path')}")

    project_name = f"_mcp_subs_live_{int(time.time())}"
    timeline_name = "subs_live_validation"
    work_dir = Path(tempfile.mkdtemp(prefix="mcp_subs_live_"))
    created_project = False

    try:
        version = server.resolve_control("get_version")
        if version.get("error"):
            raise AssertionError(f"resolve_control.get_version failed: {version['error']}")
        print(f"Connected to {version['product']} {version['version_string']}")

        created = server.project_manager("create", {"name": project_name})
        if not created.get("success"):
            raise AssertionError(f"project_manager.create failed: {created!r}")
        created_project = True
        print(f"Created disposable project: {project_name}")

        server.resolve_control("open_page", {"page": "edit"})

        speech_path = _make_synthetic_speech_wav(work_dir, "speech.wav")
        print(f"Generated synthetic speech: {speech_path.name}")

        resolve = server.get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        media_pool = project.GetMediaPool()
        imported = media_pool.ImportMedia([str(speech_path)])
        if not imported:
            raise AssertionError(f"ImportMedia failed for synthetic audio: {imported!r}")
        audio_clip = imported[0]
        media_pool.CreateTimelineFromClips(timeline_name, [audio_clip])
        print(f"Imported synthetic audio + created timeline: {timeline_name}")

        # ── render_audio ──────────────────────────────────────────────────
        rendered = server.subtitles("render_audio", {})
        if "error" in rendered:
            raise AssertionError(f"render_audio failed: {rendered['error']}")
        audio_path = rendered["audio_path"]
        if not os.path.isfile(audio_path) or os.path.getsize(audio_path) == 0:
            raise AssertionError(f"render_audio produced no usable WAV: {audio_path}")
        print(f"  render_audio: {audio_path} ({os.path.getsize(audio_path)} bytes)")

        # ── align ─────────────────────────────────────────────────────────
        aligned = server.subtitles("align", {
            "audio_path": audio_path,
            "model": "tiny",  # smallest model for speed in CI / disposable harness
            "language": "en",
        })
        if "error" in aligned:
            raise AssertionError(f"align failed: {aligned['error']}")
        srt_path = aligned["srt_path"]
        if not os.path.isfile(srt_path):
            raise AssertionError(f"align reported srt_path {srt_path} but file is missing")
        cues = aligned.get("cue_count", 0)
        if cues < 1:
            print(f"  WARNING: align produced 0 cues (synthetic audio may not be intelligible)")
        else:
            print(f"  align: {cues} cues in {srt_path}")

        # ── import_srt (best-effort probe) ────────────────────────────────
        imported_srt = server.subtitles("import_srt", {"srt_path": srt_path})
        if "error" in imported_srt:
            raise AssertionError(f"import_srt failed unexpectedly: {imported_srt['error']}")
        if imported_srt.get("manual_import_required"):
            print(f"  import_srt: manual fallback ({imported_srt.get('instructions')})")
        else:
            print(f"  import_srt: imported={imported_srt.get('imported')} "
                  f"appended={imported_srt.get('appended_to_timeline')}")

        # ── generate (end-to-end) ─────────────────────────────────────────
        gen = server.subtitles("generate", {
            "model": "tiny", "language": "en",
            "keep_audio": True, "keep_intermediates": False,
            "auto_import": True,
        })
        if "error" in gen:
            raise AssertionError(f"generate failed: {gen['error']} (stage={gen.get('stage')})")
        if not gen.get("success"):
            raise AssertionError(f"generate did not report success: {gen!r}")
        print(f"  generate: stage={gen.get('stage')} cues={gen.get('cue_count')} "
              f"imported={gen.get('imported')} manual={gen.get('manual_import_required')}")

        # ── unknown action ────────────────────────────────────────────────
        unknown = server.subtitles("nope", {})
        if "error" not in unknown:
            raise AssertionError(f"unknown action should error: {unknown!r}")
        print(f"  unknown-action error path: ok")

        print("ALL SUBS LIVE TESTS PASSED")
        return 0
    finally:
        if not keep_open and created_project:
            try:
                server.project_manager("delete", {"name": project_name})
                print(f"Deleted disposable project: {project_name}")
            except Exception as exc:
                print(f"Cleanup warning: could not delete project: {exc}", file=sys.stderr)
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave the disposable project open in Resolve for inspection.")
    args = parser.parse_args()

    _install_mcp_stubs()
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(PROJECT_ROOT))
    from src import server

    return run_validation(server, keep_open=args.keep_open)


if __name__ == "__main__":
    sys.exit(main())
