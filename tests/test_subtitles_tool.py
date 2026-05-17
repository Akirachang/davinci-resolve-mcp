"""Hermetic tests for the `subtitles` compound tool's input-validation,
dispatch, and SRT-import probe logic. Does not require a running Resolve
or whisperx; both are mocked.

Live coverage of the actual whisperx + render pipeline lives in
tests/live_whisperx_validation.py.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestSubtitlesToolDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("RESOLVE_SCRIPT_API", "/nonexistent")
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", "/nonexistent")
        sys.modules.setdefault("DaVinciResolveScript", mock.Mock())
        from src import server as srv  # noqa: E402
        cls.srv = srv

    def test_unknown_action_lists_known(self):
        result = self.srv.subtitles("nonsense", {})
        self.assertIn("error", result)
        for known in ("check_engine", "render_audio", "align",
                      "import_srt", "generate"):
            self.assertIn(known, result["error"])

    def test_check_engine_round_trip(self):
        sentinel = {"available": True, "path": "/x/whisperx", "version": "3.1.1"}
        with mock.patch.object(self.srv._whisperx_helper, "check_whisperx",
                               return_value=sentinel):
            self.assertEqual(self.srv.subtitles("check_engine"), sentinel)

    def test_align_requires_audio_path(self):
        result = self.srv.subtitles("align", {})
        self.assertEqual(result, {"error": "align requires 'audio_path'."})

    def test_align_rejects_missing_audio_file(self):
        result = self.srv.subtitles("align", {"audio_path": "/nope/audio.wav"})
        self.assertIn("error", result)
        self.assertIn("does not exist", result["error"])

    def test_align_engine_missing_returns_install_hint(self):
        # File exists, but whisperx is not available.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"RIFF....")
            audio = fh.name
        try:
            with mock.patch.object(self.srv._whisperx_helper, "check_whisperx",
                                   return_value={"available": False,
                                                 "error": "pip install whisperx"}):
                result = self.srv.subtitles("align", {"audio_path": audio})
            self.assertIn("error", result)
            self.assertIn("pip install whisperx", result["error"])
        finally:
            os.unlink(audio)

    def test_align_rejects_non_list_extra_args(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            audio = fh.name
        try:
            with mock.patch.object(self.srv._whisperx_helper, "check_whisperx",
                                   return_value={"available": True, "path": "/x", "version": "3"}):
                result = self.srv.subtitles("align",
                                            {"audio_path": audio, "extra_args": "no"})
            self.assertEqual(result, {"error": "extra_args must be a list of strings"})
        finally:
            os.unlink(audio)

    def test_import_srt_requires_path(self):
        result = self.srv.subtitles("import_srt", {})
        self.assertEqual(result, {"error": "import_srt requires 'srt_path'."})

    def test_import_srt_rejects_missing_file(self):
        result = self.srv.subtitles("import_srt", {"srt_path": "/nope/x.srt"})
        self.assertIn("error", result)
        self.assertIn("does not exist", result["error"])


class TestProbeImportSrt(unittest.TestCase):
    """Direct tests for _subs_probe_import_srt — the undocumented-API best-effort."""

    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("DaVinciResolveScript", mock.Mock())
        from src import server as srv  # noqa: E402
        cls.srv = srv

    def test_empty_import_falls_back(self):
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(return_value=[])
        result = self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=True, track_index=1,
            create_track=True, tl=mock.Mock(),
        )
        self.assertFalse(result["imported"])
        self.assertTrue(result["manual_import_required"])
        self.assertEqual(result["srt_path"], "/tmp/x.srt")
        self.assertIn("/tmp/x.srt", result["instructions"])

    def test_exception_falls_back(self):
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(side_effect=RuntimeError("boom"))
        result = self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=True, track_index=1,
            create_track=True, tl=mock.Mock(),
        )
        self.assertFalse(result["imported"])
        self.assertTrue(result["manual_import_required"])
        self.assertIn("ImportMedia raised", result["instructions"])

    def test_import_and_append_success(self):
        fake_clip = mock.Mock()
        fake_appended = mock.Mock()
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(return_value=[fake_clip])
        mp.AppendToTimeline = mock.Mock(return_value=[fake_appended])
        tl = mock.Mock()
        tl.GetTrackCount = mock.Mock(return_value=1)
        result = self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=True, track_index=1,
            create_track=True, tl=tl,
        )
        self.assertTrue(result["imported"])
        self.assertTrue(result["appended_to_timeline"])
        self.assertEqual(result["appended_item_count"], 1)
        self.assertNotIn("manual_import_required", result)

    def test_import_succeeds_but_append_fails(self):
        fake_clip = mock.Mock()
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(return_value=[fake_clip])
        mp.AppendToTimeline = mock.Mock(return_value=None)
        tl = mock.Mock()
        tl.GetTrackCount = mock.Mock(return_value=1)
        result = self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=True, track_index=1,
            create_track=True, tl=tl,
        )
        self.assertTrue(result["imported"])
        self.assertFalse(result["appended_to_timeline"])
        self.assertTrue(result["manual_import_required"])
        self.assertIn("Drag the SRT clip", result["instructions"])

    def test_create_track_when_none_exists(self):
        fake_clip = mock.Mock()
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(return_value=[fake_clip])
        mp.AppendToTimeline = mock.Mock(return_value=[mock.Mock()])
        tl = mock.Mock()
        tl.GetTrackCount = mock.Mock(return_value=0)
        tl.AddTrack = mock.Mock(return_value=True)
        self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=True, track_index=1,
            create_track=True, tl=tl,
        )
        tl.AddTrack.assert_called_once_with("subtitle")

    def test_append_false_skips_timeline_call(self):
        fake_clip = mock.Mock()
        mp = mock.Mock()
        mp.ImportMedia = mock.Mock(return_value=[fake_clip])
        mp.AppendToTimeline = mock.Mock()
        result = self.srv._subs_probe_import_srt(
            mp, "/tmp/x.srt", append=False, track_index=1,
            create_track=False, tl=mock.Mock(),
        )
        self.assertTrue(result["imported"])
        self.assertFalse(result["appended_to_timeline"])
        mp.AppendToTimeline.assert_not_called()


class TestSubsHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("DaVinciResolveScript", mock.Mock())
        from src import server as srv  # noqa: E402
        cls.srv = srv

    def test_safe_filename_strips_special_chars(self):
        self.assertEqual(self.srv._subs_safe_filename("My Timeline #1!"),
                         "My_Timeline__1_")

    def test_safe_filename_defaults_when_blank(self):
        self.assertEqual(self.srv._subs_safe_filename(""), "timeline")
        self.assertEqual(self.srv._subs_safe_filename(None), "timeline")

    def test_safe_filename_truncated(self):
        long_name = "a" * 200
        self.assertEqual(len(self.srv._subs_safe_filename(long_name)), 48)


if __name__ == "__main__":
    unittest.main()
