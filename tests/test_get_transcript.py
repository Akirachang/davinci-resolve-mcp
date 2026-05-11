"""Hermetic tests for the timeline.get_transcript action.

Live coverage against a real Resolve project lives in tests/live_v280_validation.py.
"""

import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mcp_stubs():
    """Allow importing src.server when MCP deps are absent (same pattern as
    tests/live_frames_validation.py)."""
    class FastMCP:
        def __init__(self, *a, **kw): pass
        def tool(self, *a, **kw):
            def d(f): return f
            return d
        def resource(self, *a, **kw):
            def d(f): return f
            return d

    class _Image:
        def __init__(self, path=None, data=None, format=None):
            self.path, self.data, self.format = path, data, format

    def stdio_server(*a, **kw):
        raise RuntimeError("stub")

    anyio = types.ModuleType("anyio")
    anyio.run = lambda f: f()
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    stdio_mod = types.ModuleType("mcp.server.stdio")
    fastmcp_mod.FastMCP = FastMCP
    fastmcp_mod.Image = _Image
    stdio_mod.stdio_server = stdio_server
    sys.modules.setdefault("anyio", anyio)
    sys.modules.setdefault("mcp", mcp_mod)
    sys.modules.setdefault("mcp.server", server_mod)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp_mod)
    sys.modules.setdefault("mcp.server.stdio", stdio_mod)


_install_mcp_stubs()

import src.server as compound  # noqa: E402


class SubtitleItemStub:
    def __init__(self, start, end, name):
        self._start = start
        self._end = end
        self._name = name

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._end

    def GetName(self):
        return self._name


class TimelineStub:
    def __init__(
        self,
        fps="30",
        start_frame=108000,
        subtitle_tracks=1,
        items_by_track=None,
        create_returns=True,
    ):
        self.fps = fps
        self.start_frame = start_frame
        self._sub_tracks = subtitle_tracks
        self._items = items_by_track or {1: []}
        self.create_calls = []
        self._create_returns = create_returns

    def GetTrackCount(self, track_type):
        return self._sub_tracks if track_type == "subtitle" else 0

    def GetStartFrame(self):
        return self.start_frame

    def GetItemListInTrack(self, track_type, index):
        if track_type != "subtitle":
            return []
        return list(self._items.get(index, []))

    def CreateSubtitlesFromAudio(self, settings):
        self.create_calls.append(settings)
        if self._create_returns:
            # Simulate Resolve adding a subtitle track + items
            self._sub_tracks = 1
            self._items.setdefault(
                1,
                [
                    SubtitleItemStub(108019, 108076, "hello world"),
                    SubtitleItemStub(108076, 108129, "second line"),
                ],
            )
        return self._create_returns


class ProjectStub:
    def __init__(self, timeline, fps="30"):
        self._timeline = timeline
        self._fps = fps

    def GetCurrentTimeline(self):
        return self._timeline

    def GetSetting(self, name):
        if name == "timelineFrameRate":
            return self._fps
        return None


class GetTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.original_check = compound._check
        items = [
            SubtitleItemStub(108197, 108234, "third"),
            SubtitleItemStub(108019, 108076, "first"),
            SubtitleItemStub(108076, 108129, "second"),
        ]
        self.timeline = TimelineStub(
            fps="30",
            start_frame=108000,
            subtitle_tracks=1,
            items_by_track={1: items},
        )
        self.project = ProjectStub(self.timeline, fps="30")
        compound._check = lambda: (None, self.project, None)

    def tearDown(self):
        compound._check = self.original_check

    def test_happy_path_segments_sorted_with_seconds(self):
        out = compound.timeline("get_transcript")
        self.assertNotIn("error", out)
        self.assertTrue(out["success"])
        self.assertEqual(out["item_count"], 3)
        self.assertEqual(out["track_index"], 1)
        self.assertEqual(out["timeline_start_frame"], 108000)
        self.assertEqual(out["frame_rate"], 30.0)
        # Sorted by start_frame
        self.assertEqual([s["text"] for s in out["segments"]], ["first", "second", "third"])
        # Seconds: (108019 - 108000) / 30 = 0.6333...
        self.assertAlmostEqual(out["segments"][0]["start_seconds"], 0.6333333, places=4)
        self.assertAlmostEqual(out["segments"][0]["end_seconds"], 2.5333333, places=4)

    def test_merge_returns_full_text(self):
        out = compound.timeline("get_transcript", {"merge": True})
        self.assertEqual(out["full_text"], "first second third")

    def test_no_subtitle_track_auto_create_off_returns_error(self):
        self.timeline._sub_tracks = 0
        out = compound.timeline("get_transcript", {"auto_create": False})
        self.assertIn("error", out)
        self.assertIn("No subtitle track", out["error"])
        self.assertEqual(self.timeline.create_calls, [])

    def test_no_subtitle_track_auto_create_invokes_resolve(self):
        self.timeline._sub_tracks = 0
        self.timeline._items = {}
        out = compound.timeline("get_transcript", {"settings": {"foo": "bar"}})
        self.assertEqual(self.timeline.create_calls, [{"foo": "bar"}])
        self.assertEqual(out["item_count"], 2)
        self.assertEqual(out["segments"][0]["text"], "hello world")

    def test_create_subtitles_returns_false_is_surfaced(self):
        self.timeline._sub_tracks = 0
        self.timeline._create_returns = False
        out = compound.timeline("get_transcript")
        self.assertIn("error", out)
        self.assertIn("CreateSubtitlesFromAudio returned False", out["error"])

    def test_bad_track_index_rejected(self):
        out = compound.timeline("get_transcript", {"track_index": 7})
        self.assertIn("error", out)
        self.assertIn("out of range", out["error"])

    def test_zero_or_negative_track_index_rejected(self):
        out = compound.timeline("get_transcript", {"track_index": 0})
        self.assertIn("error", out)
        out2 = compound.timeline("get_transcript", {"track_index": -1})
        self.assertIn("error", out2)

    def test_invalid_frame_rate_setting_returns_error(self):
        self.project._fps = "not-a-number"
        out = compound.timeline("get_transcript")
        self.assertIn("error", out)
        self.assertIn("timelineFrameRate", out["error"])

    def test_zero_frame_rate_rejected(self):
        self.project._fps = "0"
        out = compound.timeline("get_transcript")
        self.assertIn("error", out)
        self.assertIn("Invalid timelineFrameRate", out["error"])

    def test_empty_subtitle_track_returns_empty_segments(self):
        self.timeline._items = {1: []}
        out = compound.timeline("get_transcript")
        self.assertEqual(out["item_count"], 0)
        self.assertEqual(out["segments"], [])

    def test_text_pulls_from_get_name(self):
        self.timeline._items = {1: [SubtitleItemStub(108019, 108076, "exact caption text")]}
        out = compound.timeline("get_transcript")
        self.assertEqual(out["segments"][0]["text"], "exact caption text")

    def test_none_name_becomes_empty_string(self):
        self.timeline._items = {1: [SubtitleItemStub(108019, 108076, None)]}
        out = compound.timeline("get_transcript")
        self.assertEqual(out["segments"][0]["text"], "")

    def test_get_transcript_in_unknown_action_list(self):
        out = compound.timeline("totally_made_up_action")
        self.assertIn("error", out)
        self.assertIn("get_transcript", out["error"])


if __name__ == "__main__":
    unittest.main()
