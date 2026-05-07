#!/usr/bin/env python3
"""Live validation for timeline.duplicate_clips.

Creates a disposable Resolve project, imports synthetic media, places a trimmed
video-only timeline item, duplicates it to another track via the compound
timeline action, verifies the copied timing/source trim, and deletes the
project unless --keep-open is provided.

Run with Python 3.10-3.12 against a running Resolve Studio instance:

  python3.11 tests/live_duplicate_clips_validation.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path


def _install_mcp_stubs() -> None:
    """Allow importing src.server when MCP deps are absent from Python 3.11."""

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

    def stdio_server(*args, **kwargs):
        raise RuntimeError("stdio_server is not used by the live duplicate harness")

    anyio = types.ModuleType("anyio")
    anyio.run = lambda func: func()

    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    stdio = types.ModuleType("mcp.server.stdio")

    fastmcp.FastMCP = FastMCP
    stdio.stdio_server = stdio_server

    sys.modules.setdefault("anyio", anyio)
    sys.modules.setdefault("mcp", mcp)
    sys.modules.setdefault("mcp.server", server)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp)
    sys.modules.setdefault("mcp.server.stdio", stdio)


def _require_success(label, result):
    if not isinstance(result, dict):
        raise AssertionError(f"{label}: expected dict, got {result!r}")
    if result.get("error"):
        raise AssertionError(f"{label}: {result['error']}")
    if "success" in result and result["success"] is not True:
        raise AssertionError(f"{label}: expected success=True, got {result!r}")
    return result


def _frame_int(value):
    return int(round(float(value)))


def _source_start(item):
    if hasattr(item, "GetSourceStartFrame"):
        try:
            value = item.GetSourceStartFrame()
            if value is not None:
                return _frame_int(value)
        except Exception:
            pass
    return _frame_int(item.GetLeftOffset())


def _make_synthetic_media(work_dir: Path) -> Path:
    media_path = work_dir / "duplicate_clips_source.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=5",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(media_path),
        ],
        check=True,
    )
    return media_path


def run_validation(server, keep_open: bool = False) -> int:
    project_name = f"_mcp_duplicate_clips_live_{int(time.time())}"
    timeline_name = "duplicate_clips_live_validation"
    work_dir = Path(tempfile.mkdtemp(prefix="mcp_duplicate_clips_live_"))
    created_project = False
    delete_result = None

    try:
        version = _require_success("resolve_control.get_version", server.resolve_control("get_version"))
        print(f"Connected to {version['product']} {version['version_string']}")

        _require_success("project_manager.create", server.project_manager("create", {"name": project_name}))
        created_project = True
        print(f"Created disposable project: {project_name}")

        _require_success("resolve_control.open_page", server.resolve_control("open_page", {"page": "edit"}))
        media_path = _make_synthetic_media(work_dir)
        print(f"Generated synthetic media: {media_path}")

        resolve = server.get_resolve()
        project = resolve.GetProjectManager().GetCurrentProject()
        media_pool = project.GetMediaPool()
        imported = media_pool.ImportMedia([str(media_path)])
        if not imported:
            raise AssertionError(f"Failed to import synthetic media: {media_path}")
        media_pool_item = imported[0]
        media_pool_item_id = media_pool_item.GetUniqueId()
        print(f"Imported media pool item: {media_pool_item.GetName()}")

        timeline = media_pool.CreateEmptyTimeline(timeline_name)
        if not timeline or not project.SetCurrentTimeline(timeline):
            raise AssertionError("Failed to create or set current validation timeline")
        print(f"Created timeline: {timeline_name}")

        if int(timeline.GetTrackCount("video") or 0) < 1:
            _require_success("timeline.add_track V1", server.timeline("add_track", {"track_type": "video"}))
        if int(timeline.GetTrackCount("video") or 0) < 2:
            _require_success("timeline.add_track V2", server.timeline("add_track", {"track_type": "video"}))

        append = _require_success(
            "media_pool.append_to_timeline source trim",
            server.media_pool(
                "append_to_timeline",
                {
                    "clip_infos": [
                        {
                            "media_pool_item_id": media_pool_item_id,
                            "start_frame": 24,
                            "end_frame": 71,
                            "record_frame": 100,
                            "track_index": 1,
                            "media_type": 1,
                        }
                    ]
                },
            ),
        )
        source_id = append["items"][0]["timeline_item_id"]
        if not source_id:
            raise AssertionError(f"Append returned no source timeline item id: {append!r}")
        source_item = server._find_timeline_item_by_id(timeline, source_id)
        if not source_item:
            raise AssertionError(f"Could not find source timeline item: {source_id}")
        source_duration = _frame_int(source_item.GetDuration())
        source_start = _source_start(source_item)
        print(
            "Placed source item: "
            f"id={source_id}, start={source_item.GetStart()}, duration={source_duration}, source_start={source_start}"
        )

        duplicate = _require_success(
            "timeline.duplicate_clips",
            server.timeline(
                "duplicate_clips",
                {
                    "clip_ids": [source_id],
                    "target_track_index": 2,
                    "record_frame_offset": 200,
                },
            ),
        )
        result = duplicate["results"][0]
        if result.get("success") is not True:
            raise AssertionError(f"duplicate_clips reported failure: {duplicate!r}")
        duplicate_id = result.get("timeline_item_id")
        if not duplicate_id:
            raise AssertionError(f"duplicate_clips did not return a recoverable timeline item id: {duplicate!r}")
        duplicate_item = server._find_timeline_item_by_id(timeline, duplicate_id)
        if not duplicate_item:
            raise AssertionError(f"Could not find duplicate timeline item: {duplicate_id}")

        expected_start = _frame_int(source_item.GetStart()) + 200
        actual_start = _frame_int(duplicate_item.GetStart())
        actual_duration = _frame_int(duplicate_item.GetDuration())
        actual_source_start = _source_start(duplicate_item)
        duplicate_media_id = duplicate_item.GetMediaPoolItem().GetUniqueId()

        if actual_start != expected_start:
            raise AssertionError(f"duplicate start mismatch: expected {expected_start}, got {actual_start}")
        if actual_duration != source_duration:
            raise AssertionError(f"duplicate duration mismatch: expected {source_duration}, got {actual_duration}")
        if actual_source_start != source_start:
            raise AssertionError(
                f"duplicate source trim mismatch: expected {source_start}, got {actual_source_start}"
            )
        if duplicate_media_id != media_pool_item_id:
            raise AssertionError(f"duplicate media mismatch: expected {media_pool_item_id}, got {duplicate_media_id}")
        print(
            "Verified duplicate: "
            f"id={duplicate_id}, start={actual_start}, duration={actual_duration}, source_start={actual_source_start}"
        )

        invalid_track = server.timeline(
            "duplicate_clips",
            {"clip_ids": [source_id], "target_track_index": 99, "record_frame_offset": 10},
        )
        if "does not exist" not in invalid_track["results"][0].get("error", ""):
            raise AssertionError(f"Expected invalid track error, got {invalid_track!r}")
        print("Verified invalid target track error path")

        if keep_open:
            _require_success("project_manager.save", server.project_manager("save"))
            print(f"LEFT PROJECT OPEN FOR INSPECTION: {project_name}")
            created_project = False

    finally:
        if created_project:
            server.project_manager("save")
            server.project_manager("close")
            delete_result = server.project_manager("delete", {"name": project_name})
            print(f"Deleted disposable project: {delete_result}")

    if delete_result and delete_result.get("success") is not True:
        raise AssertionError(f"Cleanup failed for {project_name}: {delete_result!r}")
    print("duplicate_clips live validation passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live duplicate_clips validation harness")
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
