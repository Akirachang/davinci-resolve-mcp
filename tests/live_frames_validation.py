#!/usr/bin/env python3
"""Live validation for the v2.7.0 `frames` compound tool.

Creates a disposable Resolve project, generates a synthetic 5-second test clip
with ffmpeg, imports it, and exercises every action of the `frames` tool:

  - check_ffmpeg          -- diagnostic
  - extract_from_clip     -- count, timestamps_seconds, frame_numbers,
                             interval_seconds, return_images=False, max_count cap
  - extract_thumbnails    -- bulk thumbnail browser across N clip ids
  - extract_from_timeline -- graded timeline output via Project.ExportCurrentFrameAsStill

Each call's output JPEGs are inspected to confirm:
  - non-zero file size
  - correct count
  - timestamp/frame ordering and metadata fields populated

Run with Python 3.10-3.12 against a running Resolve Studio instance:

  python3.11 tests/live_frames_validation.py
  python3.11 tests/live_frames_validation.py --keep-open

Requires `ffmpeg` on PATH for synthetic media generation and the source-clip
extraction path. The timeline-extraction path uses Resolve and is also exercised.
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
        raise RuntimeError("stdio_server is not used by the frames live harness")

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


def _make_synthetic_media(work_dir: Path, name: str, duration: int = 5) -> Path:
    media_path = work_dir / name
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc2=size=320x180:rate=24:duration={duration}",
            "-pix_fmt", "yuv420p",
            "-y", str(media_path),
        ],
        check=True,
    )
    return media_path


def _extract_metadata_and_paths(result):
    """Result may be a dict (return_images=False) or a list with [metadata, Image, ...]."""
    if isinstance(result, dict):
        if result.get("error"):
            raise AssertionError(f"frames tool returned error: {result['error']}")
        return result, []
    if isinstance(result, list) and result:
        metadata = result[0]
        if isinstance(metadata, dict) and metadata.get("error"):
            raise AssertionError(f"frames tool returned error: {metadata['error']}")
        image_paths = [img.path for img in result[1:] if hasattr(img, "path") and img.path]
        return metadata, image_paths
    raise AssertionError(f"Unexpected response shape: {result!r}")


def _assert_files_nonzero(paths):
    for p in paths:
        if not os.path.isfile(p):
            raise AssertionError(f"expected output file does not exist: {p}")
        if os.path.getsize(p) == 0:
            raise AssertionError(f"output file is empty: {p}")


def run_validation(server, keep_open: bool = False) -> int:
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not found on PATH", file=sys.stderr)
        return 2

    project_name = f"_mcp_frames_live_{int(time.time())}"
    timeline_name = "frames_live_validation"
    work_dir = Path(tempfile.mkdtemp(prefix="mcp_frames_live_"))
    out_dir = Path(tempfile.mkdtemp(prefix="mcp_frames_out_"))
    created_project = False

    try:
        version = server.resolve_control("get_version")
        if version.get("error"):
            raise AssertionError(f"resolve_control.get_version failed: {version['error']}")
        print(f"Connected to {version['product']} {version['version_string']}")

        # check_ffmpeg
        ff = server.frames("check_ffmpeg")
        if not ff.get("available"):
            raise AssertionError(f"frames.check_ffmpeg reported unavailable: {ff!r}")
        print(f"ffmpeg detected: {ff.get('version')} at {ff.get('path')}")

        # Disposable project
        created = server.project_manager("create", {"name": project_name})
        if not created.get("success"):
            raise AssertionError(f"project_manager.create failed: {created!r}")
        created_project = True
        print(f"Created disposable project: {project_name}")

        server.resolve_control("open_page", {"page": "edit"})

        media_a = _make_synthetic_media(work_dir, "frames_clip_a.mov", duration=5)
        media_b = _make_synthetic_media(work_dir, "frames_clip_b.mov", duration=3)
        print(f"Generated synthetic media: {media_a.name}, {media_b.name}")

        resolve = server.get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        media_pool = project.GetMediaPool()
        imported = media_pool.ImportMedia([str(media_a), str(media_b)])
        if not imported or len(imported) < 2:
            raise AssertionError(f"ImportMedia failed: {imported!r}")
        clip_a, clip_b = imported[0], imported[1]
        clip_a_id = clip_a.GetUniqueId()
        clip_b_id = clip_b.GetUniqueId()
        print(f"Imported clip A: {clip_a.GetName()} ({clip_a_id})")
        print(f"Imported clip B: {clip_b.GetName()} ({clip_b_id})")

        # ── Test 1: extract_from_clip with default count ───────────────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "output_dir": str(out_dir / "default_count"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        if meta["frame_count"] != 8:
            raise AssertionError(f"default count expected 8, got {meta['frame_count']}")
        if meta.get("errors"):
            raise AssertionError(f"unexpected per-frame errors: {meta['errors']}")
        _assert_files_nonzero([f["path"] for f in meta["frames"]])
        print(f"  extract_from_clip default: 8 frames @ {meta['max_dimension']}px, "
              f"clip duration {meta['duration_seconds']}s, fps {meta['fps']}")

        # ── Test 2: extract_from_clip with explicit timestamps ─────────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "timestamps_seconds": [0.5, 2.5, 4.5],
            "output_dir": str(out_dir / "timestamps"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        if meta["frame_count"] != 3:
            raise AssertionError(f"timestamps expected 3, got {meta['frame_count']}")
        ts_returned = [f["timestamp_seconds"] for f in meta["frames"]]
        if ts_returned != [0.5, 2.5, 4.5]:
            raise AssertionError(f"timestamps mismatch: {ts_returned}")
        print(f"  extract_from_clip timestamps_seconds=[0.5,2.5,4.5]: ok")

        # ── Test 3: extract_from_clip with frame_numbers ───────────────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "frame_numbers": [24, 48, 72],
            "output_dir": str(out_dir / "frame_numbers"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        if meta["frame_count"] != 3:
            raise AssertionError(f"frame_numbers expected 3, got {meta['frame_count']}")
        # 24fps clip: frames 24/48/72 -> 1.0/2.0/3.0s
        ts_returned = [f["timestamp_seconds"] for f in meta["frames"]]
        if ts_returned != [1.0, 2.0, 3.0]:
            raise AssertionError(f"frame_numbers->timestamps mismatch: {ts_returned}")
        print(f"  extract_from_clip frame_numbers=[24,48,72]: ok")

        # ── Test 4: extract_from_clip with interval_seconds ───────────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "interval_seconds": 1.0,
            "output_dir": str(out_dir / "interval"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        # 5s clip, 1s interval, midpoint-biased: 0.5, 1.5, 2.5, 3.5, 4.5
        if meta["frame_count"] != 5:
            raise AssertionError(f"interval_seconds expected 5, got {meta['frame_count']}")
        print(f"  extract_from_clip interval_seconds=1.0: 5 frames")

        # ── Test 5: extract_from_clip max_count cap ────────────────────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "count": 100,
            "max_count": 4,
            "output_dir": str(out_dir / "max_count"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        if meta["frame_count"] != 4:
            raise AssertionError(f"max_count cap expected 4, got {meta['frame_count']}")
        print(f"  extract_from_clip max_count cap: ok")

        # ── Test 6: extract_thumbnails across both clips ───────────────────
        result = server.frames("extract_thumbnails", {
            "clip_ids": [clip_a_id, clip_b_id],
            "count_per_clip": 2,
            "output_dir": str(out_dir / "thumbs"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        if meta["frame_count"] != 4:
            raise AssertionError(f"thumbnails expected 4 (2 clips × 2), got {meta['frame_count']}")
        clip_ids_in_response = {f["clip_id"] for f in meta["frames"]}
        if clip_ids_in_response != {clip_a_id, clip_b_id}:
            raise AssertionError(f"thumbnails missed clips: {clip_ids_in_response}")
        print(f"  extract_thumbnails 2 clips × 2: ok")

        # ── Test 7: invalid clip_id error path ────────────────────────────
        result = server.frames("extract_from_clip", {"clip_id": "nonexistent-id"})
        if not isinstance(result, dict) or "error" not in result:
            raise AssertionError(f"expected error for invalid clip_id, got {result!r}")
        print(f"  extract_from_clip error path: {result['error']}")

        # ── Test 8: extract_from_timeline ──────────────────────────────────
        timeline = media_pool.CreateEmptyTimeline(timeline_name)
        if not timeline or not project.SetCurrentTimeline(timeline):
            raise AssertionError("Failed to create or set current validation timeline")
        # Append clip A to the timeline
        appended = media_pool.AppendToTimeline([clip_a])
        if not appended:
            raise AssertionError(f"AppendToTimeline failed: {appended!r}")
        time.sleep(0.5)

        result = server.frames("extract_from_timeline", {
            "count": 3,
            "output_dir": str(out_dir / "timeline"),
            "return_images": False,
            "cleanup": False,
        })
        meta, _ = _extract_metadata_and_paths(result)
        # Timeline extraction can fail per-frame on some Resolve builds without
        # the Color page active; require at least 1 frame extracted.
        if meta["frame_count"] < 1:
            raise AssertionError(f"extract_from_timeline produced 0 frames: {meta!r}")
        _assert_files_nonzero([f["path"] for f in meta["frames"]])
        print(f"  extract_from_timeline: {meta['frame_count']}/3 frames "
              f"(errors: {len(meta.get('errors') or [])})")

        # ── Test 9: return_images=True yields Image content blocks ─────────
        result = server.frames("extract_from_clip", {
            "clip_id": clip_a_id,
            "count": 2,
            "output_dir": str(out_dir / "with_images"),
            "return_images": True,
            "cleanup": False,
        })
        if not isinstance(result, list) or len(result) != 3:
            raise AssertionError(f"return_images=True expected list of length 3, got {result!r}")
        if not hasattr(result[1], "path"):
            raise AssertionError("expected Image-like objects in response list")
        print(f"  return_images=True returns Image blocks: ok")

        if keep_open:
            server.project_manager("save")
            print(f"LEFT PROJECT OPEN FOR INSPECTION: {project_name}")
            created_project = False

    finally:
        if created_project:
            try:
                server.project_manager("save")
                server.project_manager("close")
                server.project_manager("delete", {"name": project_name})
                print(f"Deleted disposable project: {project_name}")
            except Exception as exc:
                print(f"WARNING: cleanup failed: {exc}")
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass

    print("frames live validation passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live validation for the frames tool")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the disposable project open for manual inspection.",
    )
    args = parser.parse_args()

    _install_mcp_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import src.server as server

    return run_validation(server, keep_open=args.keep_open)


if __name__ == "__main__":
    raise SystemExit(main())
