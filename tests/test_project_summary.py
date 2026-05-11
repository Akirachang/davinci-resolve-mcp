"""Hermetic tests for the resolve_control.project_summary action.

Live coverage against a real Resolve project lives in tests/live_v280_validation.py.
"""

import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mcp_stubs():
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


class ClipStub:
    def __init__(self, name, uid):
        self._name, self._uid = name, uid

    def GetName(self):
        return self._name

    def GetUniqueId(self):
        return self._uid


class FolderStub:
    def __init__(self, name, uid, clips=(), subfolders=()):
        self._name, self._uid = name, uid
        self._clips = list(clips)
        self._subs = list(subfolders)

    def GetName(self):
        return self._name

    def GetUniqueId(self):
        return self._uid

    def GetClipList(self):
        return list(self._clips)

    def GetSubFolderList(self):
        return list(self._subs)


class TimelineStub:
    def __init__(self, name, uid, start=108000, end=115000, tracks=None):
        self._name, self._uid = name, uid
        self._start, self._end = start, end
        self._tracks = tracks or {"video": 1, "audio": 1, "subtitle": 0}

    def GetName(self):
        return self._name

    def GetUniqueId(self):
        return self._uid

    def GetStartFrame(self):
        return self._start

    def GetEndFrame(self):
        return self._end

    def GetTrackCount(self, t):
        return self._tracks.get(t, 0)


class MediaPoolStub:
    def __init__(self, root, current=None):
        self._root, self._current = root, current

    def GetRootFolder(self):
        return self._root

    def GetCurrentFolder(self):
        return self._current


class ProjectStub:
    def __init__(self, *, name="P", uid="pid", timelines=None,
                 current_timeline_idx=1, mp=None, settings=None):
        self._name, self._uid = name, uid
        self._timelines = timelines or []
        self._current_idx = current_timeline_idx
        self._mp = mp
        self._settings = settings or {
            "timelineFrameRate": "30",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }

    def GetName(self):
        return self._name

    def GetUniqueId(self):
        return self._uid

    def GetTimelineCount(self):
        return len(self._timelines)

    def GetTimelineByIndex(self, i):
        if 1 <= i <= len(self._timelines):
            return self._timelines[i - 1]
        return None

    def GetCurrentTimeline(self):
        idx = self._current_idx
        if idx and 1 <= idx <= len(self._timelines):
            return self._timelines[idx - 1]
        return None

    def GetMediaPool(self):
        return self._mp

    def GetSetting(self, k):
        return self._settings.get(k, "")


class ResolveStub:
    def GetProductName(self):
        return "DaVinci Resolve Studio"

    def GetVersion(self):
        return "20.3.2.9"

    def GetVersionString(self):
        return "20.3.2.9"

    def GetCurrentPage(self):
        return "edit"


class ProjectSummaryTest(unittest.TestCase):
    def setUp(self):
        self.original_check = compound._check
        self.original_get_resolve = compound.get_resolve
        # Default scenario: one project, two timelines, small bin tree
        self.tl_a = TimelineStub("Timeline 1", "tl-a")
        self.tl_b = TimelineStub("Edit B", "tl-b", start=86400, end=90000,
                                 tracks={"video": 2, "audio": 4, "subtitle": 1})
        sub = FolderStub("Subbin", "sub-1", clips=[ClipStub("nested.mov", "c3")])
        root = FolderStub("Master", "root", clips=[ClipStub("a.mov", "c1"), ClipStub("b.mov", "c2")],
                          subfolders=[sub])
        self.mp = MediaPoolStub(root, current=root)
        self.project = ProjectStub(
            timelines=[self.tl_a, self.tl_b],
            current_timeline_idx=2,
            mp=self.mp,
        )
        self.resolve = ResolveStub()
        compound._check = lambda: (None, self.project, None)
        compound.get_resolve = lambda: self.resolve

    def tearDown(self):
        compound._check = self.original_check
        compound.get_resolve = self.original_get_resolve

    def test_default_call_returns_overview_without_clip_lists(self):
        out = compound.resolve_control("project_summary")
        self.assertNotIn("error", out)
        self.assertTrue(out["success"])
        self.assertEqual(out["product"], "DaVinci Resolve Studio")
        self.assertEqual(out["page"], "edit")
        self.assertEqual(out["project"]["name"], "P")
        self.assertEqual(out["project"]["frame_rate"], "30")
        self.assertEqual(out["project"]["resolution_width"], 1920)
        self.assertEqual(out["project"]["resolution_height"], 1080)
        self.assertEqual(len(out["timelines"]), 2)
        self.assertFalse(out["truncated"])
        # Default: no per-folder 'clips' key
        self.assertNotIn("clips", out["media_pool"]["root_folder"])
        self.assertNotIn("clips", out["media_pool"]["root_folder"]["subfolders"][0])

    def test_is_current_flag_marks_active_timeline(self):
        out = compound.resolve_control("project_summary")
        flags = [(t["name"], t["is_current"]) for t in out["timelines"]]
        self.assertEqual(flags, [("Timeline 1", False), ("Edit B", True)])

    def test_track_counts_propagate(self):
        out = compound.resolve_control("project_summary")
        tl_b = next(t for t in out["timelines"] if t["name"] == "Edit B")
        self.assertEqual(tl_b["track_counts"], {"video": 2, "audio": 4, "subtitle": 1})

    def test_root_folder_counts(self):
        out = compound.resolve_control("project_summary")
        root = out["media_pool"]["root_folder"]
        self.assertEqual(root["name"], "Master")
        self.assertEqual(root["clip_count"], 2)
        self.assertEqual(root["subfolder_count"], 1)
        self.assertEqual(len(root["subfolders"]), 1)
        sub = root["subfolders"][0]
        self.assertEqual(sub["name"], "Subbin")
        self.assertEqual(sub["clip_count"], 1)

    def test_include_clips_returns_clip_lists(self):
        out = compound.resolve_control("project_summary", {"include_clips": True})
        root = out["media_pool"]["root_folder"]
        self.assertEqual([c["name"] for c in root["clips"]], ["a.mov", "b.mov"])
        self.assertEqual([c["name"] for c in root["subfolders"][0]["clips"]], ["nested.mov"])
        self.assertFalse(out["truncated"])

    def test_clip_limit_truncates_across_whole_tree(self):
        out = compound.resolve_control(
            "project_summary", {"include_clips": True, "clip_limit": 2},
        )
        root = out["media_pool"]["root_folder"]
        self.assertEqual([c["name"] for c in root["clips"]], ["a.mov", "b.mov"])
        # Budget was exhausted at the root — nested clip is dropped
        self.assertEqual(root["subfolders"][0]["clips"], [])
        self.assertTrue(out["truncated"])

    def test_clip_limit_one_keeps_first_only(self):
        out = compound.resolve_control(
            "project_summary", {"include_clips": True, "clip_limit": 1},
        )
        root = out["media_pool"]["root_folder"]
        self.assertEqual([c["name"] for c in root["clips"]], ["a.mov"])
        self.assertTrue(out["truncated"])

    def test_no_project_returns_error(self):
        compound._check = lambda: (None, None, compound._err("No project open"))
        out = compound.resolve_control("project_summary")
        self.assertIn("error", out)

    def test_no_media_pool_returns_null_media_pool(self):
        self.project._mp = None
        out = compound.resolve_control("project_summary")
        self.assertIsNone(out["media_pool"])
        self.assertEqual(len(out["timelines"]), 2)

    def test_no_timelines_returns_empty_list(self):
        self.project._timelines = []
        self.project._current_idx = None
        out = compound.resolve_control("project_summary")
        self.assertEqual(out["timelines"], [])

    def test_unknown_action_lists_project_summary(self):
        out = compound.resolve_control("not_an_action")
        self.assertIn("error", out)
        self.assertIn("project_summary", out["error"])


if __name__ == "__main__":
    unittest.main()
