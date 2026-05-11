"""Hermetic tests for src/utils/frame_extraction.py and the `frames` tool's
input-shape and dispatch logic. These tests do NOT require a running Resolve.

Live coverage for the actual ffmpeg invocation lives in
tests/live_frames_validation.py (uses ffmpeg-generated synthetic media).
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils import frame_extraction as fe


class TestFormatExtension(unittest.TestCase):
    def test_jpg_aliases(self):
        self.assertEqual(fe._format_to_extension("jpg"), "jpg")
        self.assertEqual(fe._format_to_extension("JPEG"), "jpg")
        self.assertEqual(fe._format_to_extension(".jpg"), "jpg")

    def test_png_and_webp(self):
        self.assertEqual(fe._format_to_extension("png"), "png")
        self.assertEqual(fe._format_to_extension("webp"), "webp")

    def test_default_when_blank(self):
        self.assertEqual(fe._format_to_extension(""), "jpg")

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            fe._format_to_extension("tiff")
        with self.assertRaises(ValueError):
            fe._format_to_extension("dpx")


class TestScaleFilter(unittest.TestCase):
    def test_none_when_no_dimension(self):
        self.assertIsNone(fe._scale_filter(None))
        self.assertIsNone(fe._scale_filter(0))
        self.assertIsNone(fe._scale_filter(-100))

    def test_scale_expression_includes_max_dimension(self):
        flt = fe._scale_filter(512)
        self.assertIn("512", flt)
        # Aspect-preserving scale should reference both iw and ih
        self.assertIn("iw", flt)
        self.assertIn("ih", flt)


class TestEvenlySpacedTimestamps(unittest.TestCase):
    def test_zero_returns_empty(self):
        self.assertEqual(fe.evenly_spaced_timestamps(10.0, 0), [])
        self.assertEqual(fe.evenly_spaced_timestamps(0.0, 5), [])

    def test_single_returns_midpoint(self):
        self.assertEqual(fe.evenly_spaced_timestamps(10.0, 1), [5.0])

    def test_eight_evenly_spaced(self):
        ts = fe.evenly_spaced_timestamps(9.0, 8)
        self.assertEqual(len(ts), 8)
        # Each entry spaced by duration / (count + 1)
        self.assertAlmostEqual(ts[0], 1.0)
        self.assertAlmostEqual(ts[-1], 8.0)
        # All entries strictly inside (0, duration)
        for t in ts:
            self.assertGreater(t, 0)
            self.assertLess(t, 9.0)


class TestNormalizeFrameSelection(unittest.TestCase):
    def test_default_count_when_nothing_specified(self):
        ts, err = fe.normalize_frame_selection(10.0, 24.0)
        self.assertIsNone(err)
        self.assertEqual(len(ts), 8)

    def test_explicit_count(self):
        ts, err = fe.normalize_frame_selection(10.0, 24.0, count=4)
        self.assertIsNone(err)
        self.assertEqual(len(ts), 4)

    def test_explicit_timestamps_passthrough(self):
        ts, err = fe.normalize_frame_selection(10.0, 24.0, timestamps_seconds=[1.0, 5.0])
        self.assertIsNone(err)
        self.assertEqual(ts, [1.0, 5.0])

    def test_frame_numbers_use_fps(self):
        ts, err = fe.normalize_frame_selection(10.0, 24.0, frame_numbers=[24, 48])
        self.assertIsNone(err)
        self.assertEqual(ts, [1.0, 2.0])

    def test_frame_numbers_without_fps_errors(self):
        ts, err = fe.normalize_frame_selection(10.0, None, frame_numbers=[24])
        self.assertIsNotNone(err)
        self.assertEqual(ts, [])

    def test_interval_seconds(self):
        ts, err = fe.normalize_frame_selection(10.0, 24.0, interval_seconds=2.0)
        self.assertIsNone(err)
        # 2s interval, midpoint-biased: 1, 3, 5, 7, 9
        self.assertEqual(ts, [1.0, 3.0, 5.0, 7.0, 9.0])

    def test_interval_zero_errors(self):
        _, err = fe.normalize_frame_selection(10.0, 24.0, interval_seconds=0)
        self.assertIsNotNone(err)

    def test_two_modes_specified_errors(self):
        _, err = fe.normalize_frame_selection(10.0, 24.0, count=4, interval_seconds=2.0)
        self.assertIsNotNone(err)

    def test_max_count_applied(self):
        ts, err = fe.normalize_frame_selection(100.0, 24.0, count=50, max_count=10)
        self.assertIsNone(err)
        self.assertEqual(len(ts), 10)

    def test_timestamps_clamped_within_duration(self):
        ts, err = fe.normalize_frame_selection(5.0, 24.0, timestamps_seconds=[10.0])
        self.assertIsNone(err)
        # Should clamp to just under duration
        self.assertLess(ts[0], 5.0)
        self.assertGreaterEqual(ts[0], 4.0)


class TestParseResolveFps(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(fe.parse_resolve_fps(24), 24.0)
        self.assertEqual(fe.parse_resolve_fps(23.976), 23.976)

    def test_string_with_decimals(self):
        self.assertEqual(fe.parse_resolve_fps("23.976"), 23.976)

    def test_drop_frame_string(self):
        self.assertEqual(fe.parse_resolve_fps("29.97 DF"), 29.97)

    def test_invalid_returns_none(self):
        self.assertIsNone(fe.parse_resolve_fps(None))
        self.assertIsNone(fe.parse_resolve_fps(""))
        self.assertIsNone(fe.parse_resolve_fps("not a number"))
        self.assertIsNone(fe.parse_resolve_fps(0))


class TestParseResolveDuration(unittest.TestCase):
    def test_frames_preferred(self):
        # Frames + fps takes priority over Duration string
        d = fe.parse_resolve_duration_seconds("00:00:01:00", 24.0, frames=240)
        self.assertEqual(d, 10.0)

    def test_timecode_fallback(self):
        d = fe.parse_resolve_duration_seconds("00:00:10:00", 24.0)
        self.assertAlmostEqual(d, 10.0)

    def test_drop_frame_separator(self):
        d = fe.parse_resolve_duration_seconds("00:00:10;00", 24.0)
        self.assertAlmostEqual(d, 10.0)

    def test_no_fps_returns_none(self):
        self.assertIsNone(fe.parse_resolve_duration_seconds("00:00:10:00", None))

    def test_malformed_returns_none(self):
        self.assertIsNone(fe.parse_resolve_duration_seconds("bogus", 24.0))


class TestCheckFfmpeg(unittest.TestCase):
    def test_missing_returns_install_hint(self):
        with mock.patch("shutil.which", return_value=None):
            result = fe.check_ffmpeg()
        self.assertFalse(result["available"])
        self.assertIn("brew install ffmpeg", result.get("error", ""))

    def test_present_returns_version(self):
        fake = mock.Mock(returncode=0, stdout="ffmpeg version 7.0.1 Copyright ...\n", stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=fake):
            result = fe.check_ffmpeg()
        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "7.0.1")
        self.assertEqual(result["path"], "/usr/bin/ffmpeg")


class TestExtractFrameMocked(unittest.TestCase):
    """Verify ffmpeg invocation shape without needing ffmpeg installed."""

    def test_command_includes_seek_input_and_output(self):
        captured = {}
        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            # Simulate success
            Path(cmd[-1]).write_bytes(b"\xff\xd8\xff\xd9")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", side_effect=fake_run):
            tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
            out = tmpdir / "test_extract_frame_out.jpg"
            ok, err = fe.extract_frame(
                "/dev/null/source.mov", 1.234, str(out),
                max_dimension=256,
            )

        try:
            self.assertTrue(ok, msg=err)
            cmd = captured["cmd"]
            self.assertEqual(cmd[0], "/usr/bin/ffmpeg")
            self.assertIn("-ss", cmd)
            ss_idx = cmd.index("-ss")
            self.assertEqual(cmd[ss_idx + 1], "1.234")
            self.assertIn("-i", cmd)
            self.assertEqual(cmd[-1], str(out))
            self.assertIn("-vf", cmd)
            self.assertIn("-q:v", cmd)
        finally:
            try:
                out.unlink()
            except FileNotFoundError:
                pass

    def test_png_output_skips_qv(self):
        captured = {}
        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", side_effect=fake_run):
            tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
            out = tmpdir / "test_extract_frame_out.png"
            ok, err = fe.extract_frame("/dev/null/x.mov", 0.0, str(out))

        try:
            self.assertTrue(ok, msg=err)
            self.assertNotIn("-q:v", captured["cmd"])
        finally:
            try:
                out.unlink()
            except FileNotFoundError:
                pass

    def test_nonzero_exit_returns_error(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run", return_value=mock.Mock(
                 returncode=1, stdout="", stderr="moov atom not found")):
            ok, err = fe.extract_frame("/dev/null/x.mov", 1.0, "/tmp/x.jpg")
        self.assertFalse(ok)
        self.assertIn("moov atom not found", err)


class TestFramesToolDispatch(unittest.TestCase):
    """Verify the `frames` compound tool's input validation without Resolve."""

    @classmethod
    def setUpClass(cls):
        # Importing src.server runs path setup but does not require Resolve until
        # a tool is actually called (lazy connect).
        os.environ.setdefault("RESOLVE_SCRIPT_API", "/nonexistent")
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", "/nonexistent")
        # Ensure DaVinciResolveScript import won't crash module import
        sys.modules.setdefault("DaVinciResolveScript", mock.Mock())
        from src import server as srv  # noqa: E402
        cls.srv = srv

    def test_unknown_action_returns_known_actions(self):
        result = self.srv.frames("nonsense", {})
        self.assertIn("error", result)
        self.assertIn("extract_from_clip", result["error"])

    def test_extract_from_clip_requires_clip_id(self):
        with mock.patch.object(self.srv._frames_helper, "check_ffmpeg",
                               return_value={"available": True}):
            result = self.srv.frames("extract_from_clip", {})
        self.assertEqual(result, {"error": "extract_from_clip requires 'clip_id'."})

    def test_extract_from_clip_ffmpeg_missing(self):
        with mock.patch.object(self.srv._frames_helper, "check_ffmpeg",
                               return_value={"available": False, "error": "no ffmpeg"}):
            result = self.srv.frames("extract_from_clip", {"clip_id": "abc"})
        self.assertEqual(result, {"error": "no ffmpeg"})

    def test_extract_thumbnails_requires_clip_ids(self):
        with mock.patch.object(self.srv._frames_helper, "check_ffmpeg",
                               return_value={"available": True}):
            result = self.srv.frames("extract_thumbnails", {})
        self.assertEqual(result, {"error": "extract_thumbnails requires 'clip_ids' (non-empty list)."})

    def test_check_ffmpeg_round_trip(self):
        sentinel = {"available": True, "path": "/usr/bin/ffmpeg", "version": "7.0"}
        with mock.patch.object(self.srv._frames_helper, "check_ffmpeg",
                               return_value=sentinel):
            self.assertEqual(self.srv.frames("check_ffmpeg", {}), sentinel)


class TestFrameIdToTimecode(unittest.TestCase):
    """Verify the new _frame_id_to_timecode helper used by extract_from_timeline."""

    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("DaVinciResolveScript", mock.Mock())
        from src import server as srv  # noqa: E402
        cls.srv = srv

    def test_zero_frame(self):
        tc, err = self.srv._frame_id_to_timecode(0, 24.0)
        self.assertIsNone(err)
        self.assertEqual(tc, "00:00:00:00")

    def test_one_second_at_24fps(self):
        tc, err = self.srv._frame_id_to_timecode(24, 24.0)
        self.assertIsNone(err)
        self.assertEqual(tc, "00:00:01:00")

    def test_one_hour_at_24fps(self):
        tc, err = self.srv._frame_id_to_timecode(86400, 24.0)
        self.assertIsNone(err)
        self.assertEqual(tc, "01:00:00:00")

    def test_negative_frame_errors(self):
        _, err = self.srv._frame_id_to_timecode(-1, 24.0)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
